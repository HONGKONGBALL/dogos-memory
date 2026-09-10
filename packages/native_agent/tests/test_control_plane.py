from __future__ import annotations

import http.client
import json
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from native_agent.control_plane import MAX_REQUEST_BYTES, create_server
from native_agent.control_plane.__main__ import _parser
from native_agent.identity import validate_bundle


class ControlPlaneBindingTests(unittest.TestCase):
    def test_non_loopback_bind_is_rejected_even_with_allowed_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "must-not-be-created"
            cases = (
                ("0.0.0.0", ["127.0.0.1"]),
                ("192.168.1.25", ["192.168.1.25"]),
                ("example.test", ["127.0.0.1"]),
                ("::1", ["localhost"]),
            )
            for bind_host, allowed_hosts in cases:
                with self.subTest(bind_host=bind_host):
                    with self.assertRaisesRegex(ValueError, "IPv4 loopback"):
                        create_server(
                            bind_host=bind_host,
                            port=0,
                            data_root=root,
                            allowed_hosts=allowed_hosts,
                        )
                    self.assertFalse(root.exists())

    def test_loopback_bind_cannot_allow_a_lan_host(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "IPv4 loopback"):
                create_server(
                    bind_host="127.0.0.1",
                    port=0,
                    data_root=Path(temporary) / "managed",
                    allowed_hosts=["192.168.1.25"],
                )

    def test_localhost_is_bound_as_a_literal_loopback_address(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            server = create_server(
                bind_host="localhost",
                port=0,
                data_root=Path(temporary) / "managed",
            )
            try:
                self.assertEqual(server.server_address[0], "127.0.0.1")
                self.assertEqual(
                    server.allowed_hostnames,
                    frozenset({"localhost", "127.0.0.1"}),
                )
            finally:
                server.server_close()

    def test_cli_has_no_bind_or_allowed_host_option(self) -> None:
        destinations = {action.dest for action in _parser()._actions}
        self.assertNotIn("bind", destinations)
        self.assertNotIn("allowed_host", destinations)


class ControlPlaneEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temporary.name) / "managed"
        self.server = create_server(port=0, data_root=self.data_root)
        self.port = int(self.server.server_address[1])
        self.origin = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        request_headers = dict(headers or {})
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        data = response.read()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        connection.close()
        return response.status, response_headers, data

    def post_json(
        self,
        value: object,
        *,
        origin: str | None = None,
        content_type: str = "application/json",
        host: str | None = None,
    ) -> tuple[int, dict[str, str], dict[str, object]]:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": content_type,
            "Origin": self.origin if origin is None else origin,
        }
        if host is not None:
            headers["Host"] = host
        status, response_headers, data = self.request(
            "POST", "/api/identity/preview", body=body, headers=headers
        )
        return status, response_headers, json.loads(data)

    def test_mobile_page_and_health_are_available(self) -> None:
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'name="viewport"', body)
        self.assertIn("不连接设备".encode(), body)
        self.assertIn("default-src 'none'", headers["content-security-policy"])
        self.assertEqual(headers["x-content-type-options"], "nosniff")

        status, _, body = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        health = json.loads(body)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["network_scope"], "loopback_only")
        self.assertTrue(health["capabilities"]["identity_preview"])
        self.assertFalse(health["capabilities"]["identity_apply"])
        self.assertFalse(health["capabilities"]["shell"])

    def test_preview_builds_valid_private_bundle_without_device_changes(self) -> None:
        status, headers, result = self.post_json(
            {
                "dog_id": "dog_a",
                "files": {
                    "soul.md": "# Soul\n\nName: Harbor\n",
                    "user.md": "# User\n\nCalls me Chen.\n",
                    "memory.md": "# Memory\n\nLikes quiet rooms.\n",
                },
            },
            content_type="application/json; charset=UTF-8",
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["operation"], "identity_preview")
        self.assertEqual(result["dry_run"]["status"], "validated")
        self.assertFalse(result["dry_run"]["device_changes"])
        self.assertFalse(result["dry_run"]["apply_performed"])
        self.assertEqual(
            {item["filename"] for item in result["files"]},
            {"AGENTS.md", "soul.md", "user.md", "memory.md"},
        )

        bundle = self.data_root / "previews" / "dog_a" / result["revision"]
        manifest = validate_bundle(bundle)
        self.assertEqual(manifest.revision, result["revision"])
        self.assertEqual((bundle / "payload" / "soul.md").read_text(), "# Soul\n\nName: Harbor\n")
        self.assertEqual(stat.S_IMODE((bundle / "manifest.json").stat().st_mode), 0o600)
        self.assertEqual(list((self.data_root / "scratch").iterdir()), [])

    def test_schema_is_exact_and_never_accepts_paths(self) -> None:
        cases = (
            (
                {"dog_id": "dog_a", "files": {}},
                "missing_file",
            ),
            (
                {"dog_id": "dog_a", "files": {"soul.md": "# Soul\n", "extra.md": "x"}},
                "unknown_identity_file",
            ),
            (
                {"dog_id": "dog_a", "files": {"soul.md": 3}},
                "invalid_file_content",
            ),
            (
                {
                    "dog_id": "dog_a",
                    "files": {"soul.md": "# Soul\n"},
                    "source_path": "/tmp/source",
                },
                "invalid_request_keys",
            ),
            (
                {
                    "dog_id": "dog_a",
                    "files": {"soul.md": "# Soul\n"},
                    "device_root": "/",
                },
                "invalid_request_keys",
            ),
        )
        for request, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                status, _, result = self.post_json(request)
                self.assertEqual(status, 400)
                self.assertEqual(result["error"]["code"], expected_code)
        self.assertEqual(list((self.data_root / "previews").iterdir()), [])
        self.assertEqual(list((self.data_root / "scratch").iterdir()), [])

    def test_duplicate_json_keys_and_partial_content_are_rejected(self) -> None:
        duplicate = b'{"dog_id":"dog_a","dog_id":"dog_b","files":{"soul.md":"# Soul\\n"}}'
        status, _, body = self.request(
            "POST",
            "/api/identity/preview",
            body=duplicate,
            headers={"Content-Type": "application/json", "Origin": self.origin},
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "duplicate_json_key")

        status, _, result = self.post_json(
            {"dog_id": "dog_a", "files": {"soul.md": ""}}
        )
        self.assertEqual(status, 400)
        self.assertEqual(result["error"]["code"], "empty_file")
        self.assertEqual(list((self.data_root / "scratch").iterdir()), [])

    def test_content_type_host_origin_and_size_are_enforced(self) -> None:
        valid = {"dog_id": "dog_a", "files": {"soul.md": "# Soul\n"}}

        status, _, result = self.post_json(valid, content_type="text/plain")
        self.assertEqual(status, 415)
        self.assertEqual(result["error"]["code"], "unsupported_media_type")

        body = json.dumps(valid).encode()
        status, _, raw = self.request(
            "POST",
            "/api/identity/preview",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(raw)["error"]["code"], "missing_header")

        status, _, result = self.post_json(valid, origin="http://attacker.invalid")
        self.assertEqual(status, 403)
        self.assertEqual(result["error"]["code"], "origin_rejected")

        status, _, result = self.post_json(valid, host="attacker.invalid")
        self.assertEqual(status, 403)
        self.assertEqual(result["error"]["code"], "host_rejected")

        # The server rejects from Content-Length without consuming an oversized
        # body. Send only the headers so the client cannot race the intentional
        # connection close while uploading bytes that the server will ignore.
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.putrequest("POST", "/api/identity/preview", skip_host=True)
            connection.putheader("Host", f"127.0.0.1:{self.port}")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Origin", self.origin)
            connection.putheader("Content-Length", str(MAX_REQUEST_BYTES + 1))
            connection.endheaders()
            response = connection.getresponse()
            raw = response.read()
            self.assertEqual(response.status, 413)
            self.assertEqual(json.loads(raw)["error"]["code"], "request_too_large")
        finally:
            connection.close()

    def test_apply_and_shell_routes_do_not_exist(self) -> None:
        body = b"{}"
        headers = {"Content-Type": "application/json", "Origin": self.origin}
        for route in ("/api/identity/apply", "/api/shell", "/api/device"):
            with self.subTest(route=route):
                status, _, raw = self.request("POST", route, body=body, headers=headers)
                self.assertEqual(status, 404)
                self.assertEqual(json.loads(raw)["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
