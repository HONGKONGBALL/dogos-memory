# Vbot MCP registration probe

This is a local-only Streamable HTTP MCP server for checking whether an Agent
runtime can register and call an extension server. It exposes exactly one tool:

- `vbot_extension_ping` accepts an empty object and returns the text `pong`.

The input schema is strict (`additionalProperties: false`). The server has no
robot, ROS, file, network-fetch, or shell tools. It does not connect to a Vbot.

## Runtime

The probe uses the `@modelcontextprotocol/sdk` and `zod` dependencies declared by
the repository root. It does not vendor another SDK. Run `npm install` before
starting the probe.

Start it from the repository root:

```sh
./packages/native_agent/mcp_probe/start.sh
```

It listens only on `127.0.0.1` and defaults to this endpoint:

```text
http://127.0.0.1:8768/mcp
```

Select another local port with `MCP_PROBE_PORT`:

```sh
MCP_PROBE_PORT=9001 ./packages/native_agent/mcp_probe/start.sh
```

The transport is stateless and uses direct JSON responses. GET and DELETE are
not enabled. Requests are limited to 64 KiB, headers must arrive within five
seconds, and each request must finish within ten seconds. The server accepts at
most 16 concurrent requests by default; select a lower bound for constrained
hosts with `MCP_PROBE_MAX_CONCURRENT` (valid range: 1-64).

## Local protocol smoke test

The smoke test launches `start.sh` on an ephemeral loopback port, then uses the
same SDK's MCP client to perform initialization, list tools, call the ping tool,
and confirm that an extra input field is rejected. It also checks the body-size
and concurrency limits, then verifies clean shutdown while a request is still
active:

```sh
npm run test:mcp
```

The pytest wrapper runs the same end-to-end check:

```sh
uv run pytest -q packages/native_agent/tests/test_mcp_probe.py
```

A passing test proves the local MCP protocol and schema behavior. It does not
prove that a particular Vbot firmware exposes an MCP registration setting.
