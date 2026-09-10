#!/usr/bin/env node

import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import { mkdtempSync, realpathSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createRequire } from "node:module";
import { once } from "node:events";
import { fileURLToPath } from "node:url";

const directory = fileURLToPath(new URL(".", import.meta.url));
const projectRoot = fileURLToPath(new URL("../../..", import.meta.url));
const dogosSource = process.env.DOGOS_MEMORY_SOURCE || projectRoot;
const requireFromProject = createRequire(
  new URL("../../../package.json", import.meta.url),
);
const { Client } = requireFromProject("@modelcontextprotocol/sdk/client/index.js");
const { StreamableHTTPClientTransport } = requireFromProject(
  "@modelcontextprotocol/sdk/client/streamableHttp.js",
);

function endpoint(child) {
  return new Promise((resolve, reject) => {
    let output = "";
    let errors = "";
    const timer = setTimeout(() => reject(new Error(`startup timeout: ${errors}`)), 6000);
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk) => { errors += chunk; });
    child.stdout.on("data", (chunk) => {
      output += chunk;
      const match = output.match(/DogOS MCP listening at (http:\/\/127\.0\.0\.1:\d+\/mcp)/);
      if (match) {
        clearTimeout(timer);
        resolve(match[1]);
      }
    });
    child.once("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`server exited ${code}: ${errors}`));
    });
  });
}

function start(owner, peer, ownerDb, peerDb, relayDb) {
  return spawn("./start.sh", [], {
    cwd: directory,
    env: {
      ...process.env,
      DOGOS_DATABASE: ownerDb,
      DOGOS_MEMORY_SOURCE: dogosSource,
      DOGOS_MCP_PORT: "0",
      DOGOS_OWNER_ID: owner,
      DOGOS_PEER_DATABASE: peerDb,
      DOGOS_PEER_ID: peer,
      DOGOS_RELAY_DATABASE: relayDb,
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
}

async function connect(url, name) {
  const client = new Client({ name, version: "0.1.0" });
  await client.connect(new StreamableHTTPClientTransport(new URL(url)));
  return client;
}

function resultJson(result) {
  assert.notEqual(result.isError, true, JSON.stringify(result));
  assert.equal(result.content.length, 1);
  return JSON.parse(result.content[0].text);
}

async function stop(child) {
  if (child.exitCode !== null) return;
  const exited = once(child, "exit");
  child.kill("SIGTERM");
  await exited;
}

const data = realpathSync(mkdtempSync(join(tmpdir(), "dogos-mcp-")));
const dogADb = join(data, "dog_a.db");
const dogBDb = join(data, "dog_b.db");
const relayDb = join(data, "relay.db");
execFileSync(
  process.env.DOGOS_PYTHON || "python3",
  [join(directory, "bootstrap_pair.py"), "--dogos-source", dogosSource, "--directory", data],
  { stdio: "ignore" },
);

const serverA = start("dog_a", "dog_b", dogADb, dogBDb, relayDb);
const serverB = start("dog_b", "dog_a", dogBDb, dogADb, relayDb);
let clientA;
let clientB;
try {
  const [urlA, urlB] = await Promise.all([endpoint(serverA), endpoint(serverB)]);
  [clientA, clientB] = await Promise.all([
    connect(urlA, "dog-a-smoke"),
    connect(urlB, "dog-b-smoke"),
  ]);
  const tools = await clientA.listTools();
  assert.deepEqual(tools.tools.map((item) => item.name), [
    "dogos_recall",
    "dogos_send_message",
    "dogos_inbox",
    "dogos_ack_message",
  ]);

  const before = resultJson(await clientA.callTool({ name: "dogos_recall", arguments: { limit: 3 } }));
  assert.equal(before.owner_id, "dog_a");
  assert.deepEqual(before.facts, []);

  const sent = resultJson(await clientA.callTool({
    name: "dogos_send_message",
    arguments: { message_id: "smoke-1", content: "你好，我是 A。" },
  }));
  assert.equal(sent.state, "queued");
  assert.equal(sent.inserted, true);
  const replay = resultJson(await clientA.callTool({
    name: "dogos_send_message",
    arguments: { message_id: "smoke-1", content: "你好，我是 A。" },
  }));
  assert.equal(replay.inserted, false);

  const inbox = resultJson(await clientB.callTool({ name: "dogos_inbox", arguments: { limit: 10 } }));
  assert.equal(inbox.trust, "untrusted_peer_message");
  assert.equal(inbox.messages[0].message_id, "smoke-1");
  assert.equal(inbox.messages[0].acknowledged, false);

  const wrongSide = await clientA.callTool({
    name: "dogos_ack_message",
    arguments: { message_id: "smoke-1" },
  });
  assert.equal(wrongSide.isError, true);
  const acknowledged = resultJson(await clientB.callTool({
    name: "dogos_ack_message",
    arguments: { message_id: "smoke-1" },
  }));
  assert.equal(acknowledged.state, "acknowledged");

  const [afterA, afterB] = await Promise.all([
    clientA.callTool({ name: "dogos_recall", arguments: { limit: 3 } }),
    clientB.callTool({ name: "dogos_recall", arguments: { limit: 3 } }),
  ]).then((items) => items.map(resultJson));
  assert.equal(afterA.facts[0].event_type, "agent_message_delivered");
  assert.equal(afterB.facts[0].event_type, "agent_message_received");
  assert.equal(afterA.facts[0].content, "你好，我是 A。");
  assert.equal(afterB.facts[0].content, "你好，我是 A。");
  console.log(JSON.stringify({ ok: true, endpoints: 2, exchanged: "smoke-1", persistedViews: 2 }));
} finally {
  await Promise.allSettled([clientA?.close(), clientB?.close()]);
  await Promise.allSettled([stop(serverA), stop(serverB)]);
  rmSync(data, { recursive: true, force: true });
}
