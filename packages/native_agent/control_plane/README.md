# Vbot local control plane

This is a retired development preview with a mobile-sized layout for creating
validated identity bundles from a browser on the same computer. It cannot
apply a bundle, accept a source or device path, execute a command, or connect
to a robot.

Run it with Python 3.10 or newer:

```sh
python -m native_agent.control_plane
```

The listener is restricted to IPv4 loopback and defaults to
`127.0.0.1:8769`. Wildcard and LAN binds are rejected by the server even when
it is called programmatically. Preview bundles are retained under
`~/.local/share/vbot-control-plane/previews`.

It is not part of the first-release product path. Do not expose this HTTP server to a LAN or proxy it to a physical phone. It has
no transport encryption or user authentication. Physical-phone access needs a
future HTTPS entry point with authentication or device pairing; the responsive
page can be reused behind that entry point.

The only JSON endpoints are:

- `GET /api/health`
- `POST /api/identity/preview`

The preview request has exactly this shape:

```json
{
  "dog_id": "dog_a",
  "files": {
    "soul.md": "# Soul\n",
    "user.md": "# User\n",
    "memory.md": "# Memory\n"
  }
}
```

`soul.md` is required. The other two files are optional. No other fields or
filenames are accepted. Browser POST requests require a same-origin `Origin`
header and an `application/json` content type.
