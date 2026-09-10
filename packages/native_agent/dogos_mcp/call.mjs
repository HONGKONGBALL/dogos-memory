#!/usr/bin/env node

import { createRequire } from "node:module";

const requireFromProject = createRequire(
  new URL("../../../package.json", import.meta.url),
);
const { Client } = requireFromProject("@modelcontextprotocol/sdk/client/index.js");
const { StreamableHTTPClientTransport } = requireFromProject(
  "@modelcontextprotocol/sdk/client/streamableHttp.js",
);

if (process.argv.length !== 5) {
  throw new Error("Usage: call.mjs MCP_URL TOOL_NAME JSON_ARGUMENTS");
}
const [, , endpoint, toolName, rawArguments] = process.argv;
const client = new Client({ name: "dogos-pair-cli", version: "0.1.0" });
try {
  await client.connect(new StreamableHTTPClientTransport(new URL(endpoint)));
  const result = await client.callTool({
    name: toolName,
    arguments: JSON.parse(rawArguments),
  });
  process.stdout.write(`${JSON.stringify(result)}\n`);
  if (result.isError === true) process.exitCode = 1;
} finally {
  await client.close();
}
