# DogOS MCP pair

This binds the DogOS memory package to two separate
MCP endpoints, one per dog/Agent. Each endpoint fixes its owner, peer, DogOS
database, peer database, and relay database outside model input.

Tools:

- `dogos_recall`: read this dog's bounded memory of its fixed peer.
- `dogos_send_message`: queue an idempotent text message to the fixed peer.
- `dogos_inbox`: read peer messages as untrusted content.
- `dogos_ack_message`: acknowledge an incoming message, then record the same
  zero-affinity completed chat in both dogs' DogOS views.

No tool can select a robot address, execute shell/ROS, change affinity, or call
a body action. The server binds to `127.0.0.1` only.

Run the complete two-endpoint protocol and persistence test:

```bash
npm run test:dogos-mcp
```

Start persistent local endpoints:

```bash
./packages/native_agent/dogos_mcp/start_pair.sh
# dog_a: http://127.0.0.1:8768/mcp
# dog_b: http://127.0.0.1:8769/mcp
```

The persistent data defaults to the downloaded dependency's ignored
`data/mcp-pair` directory. Set `DOGOS_MODE=live`
only for a new, separately named data directory after both hardware identities
pass the upstream `connect-check`; simulation databases are not live evidence.
