#!/usr/bin/env node

import assert from "node:assert/strict";
import http from "node:http";
import { once } from "node:events";
import { createRequire } from "node:module";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const probeDirectory = fileURLToPath(new URL(".", import.meta.url));
const requireFromProject = createRequire(
  new URL("../../../package.json", import.meta.url),
);
const { Client } = requireFromProject(
  "@modelcontextprotocol/sdk/client/index.js",
);
const { StreamableHTTPClientTransport } = requireFromProject(
  "@modelcontextprotocol/sdk/client/streamableHttp.js",
);

function waitForEndpoint(child, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    let stdout = "";
    let stderr = "";

    const timer = setTimeout(() => {
      reject(
        new Error(
          `Timed out waiting for probe startup. stdout=${JSON.stringify(stdout)} ` +
            `stderr=${JSON.stringify(stderr)}`,
        ),
      );
    }, timeoutMs);

    const finish = (callback) => {
      clearTimeout(timer);
      callback();
    };

    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });

    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
      const match = stdout.match(
        /MCP probe listening at (http:\/\/127\.0\.0\.1:\d+\/mcp)/,
      );
      if (match) {
        finish(() => resolve(match[1]));
      }
    });

    child.once("exit", (code, signal) => {
      finish(() =>
        reject(
          new Error(
            `Probe exited before startup (code=${code}, signal=${signal}). ` +
              `stderr=${JSON.stringify(stderr)}`,
          ),
        ),
      );
    });
  });
}

async function stopChild(child) {
  if (child.exitCode !== null) {
    assert.equal(child.exitCode, 0, "Probe exited with a non-zero status");
    return;
  }
  if (child.signalCode !== null) {
    throw new Error(`Probe exited unexpectedly from ${child.signalCode}`);
  }

  const exited = once(child, "exit");
  child.kill("SIGTERM");
  const timeoutToken = Symbol("timeout");
  let timeout;
  const timedOut = new Promise((resolve) => {
    timeout = setTimeout(() => resolve(timeoutToken), 3000);
  });
  const outcome = await Promise.race([exited, timedOut]);
  clearTimeout(timeout);

  if (outcome === timeoutToken) {
    child.kill("SIGKILL");
    await exited;
    throw new Error("Probe did not stop within 3 seconds after SIGTERM");
  }

  const [code, signal] = outcome;
  assert.equal(signal, null, `Probe stopped from unexpected signal ${signal}`);
  assert.equal(code, 0, "Probe did not shut down cleanly");
}

async function openSlowRequest(endpoint) {
  const endpointUrl = new URL(endpoint);
  const request = http.request({
    hostname: endpointUrl.hostname,
    port: endpointUrl.port,
    path: endpointUrl.pathname,
    method: "POST",
    headers: {
      accept: "application/json, text/event-stream",
      "content-type": "application/json",
      "transfer-encoding": "chunked",
    },
  });
  request.on("error", () => {});
  request.on("response", (response) => response.resume());

  const socketReady = once(request, "socket").then(async ([socket]) => {
    if (socket.connecting) {
      await once(socket, "connect");
    }
  });
  request.write('{"jsonrpc":"2.0",');
  await socketReady;
  await new Promise((resolve) => setTimeout(resolve, 100));
  return request;
}

function sendChunkedRequest(endpoint, chunks) {
  return new Promise((resolve, reject) => {
    const endpointUrl = new URL(endpoint);
    const request = http.request(
      {
        hostname: endpointUrl.hostname,
        port: endpointUrl.port,
        path: endpointUrl.pathname,
        method: "POST",
        headers: {
          accept: "application/json, text/event-stream",
          "content-type": "application/json",
          "transfer-encoding": "chunked",
        },
      },
      (response) => {
        let body = "";
        response.setEncoding("utf8");
        response.on("data", (chunk) => {
          body += chunk;
        });
        response.once("end", () => {
          resolve({ body, status: response.statusCode });
        });
      },
    );
    request.once("error", reject);
    for (const chunk of chunks) {
      request.write(chunk);
    }
    request.end();
  });
}

const child = spawn("./start.sh", [], {
  cwd: probeDirectory,
  env: {
    ...process.env,
    MCP_PROBE_MAX_CONCURRENT: "1",
    MCP_PROBE_PORT: "0",
  },
  stdio: ["ignore", "pipe", "pipe"],
});

let client;
let endpoint;
let slowRequest;
let childStopped = false;
try {
  endpoint = await waitForEndpoint(child);
  const endpointUrl = new URL(endpoint);
  assert.equal(endpointUrl.hostname, "127.0.0.1");

  const transport = new StreamableHTTPClientTransport(endpointUrl);
  client = new Client({
    name: "vbot-extension-probe-smoke-test",
    version: "0.1.0",
  });
  await client.connect(transport);

  const listed = await client.listTools();
  assert.deepEqual(
    listed.tools.map((tool) => tool.name),
    ["vbot_extension_ping"],
  );

  const inputSchema = listed.tools[0].inputSchema;
  assert.equal(inputSchema.type, "object");
  assert.deepEqual(inputSchema.properties, {});
  assert.equal(inputSchema.additionalProperties, false);

  const result = await client.callTool({
    name: "vbot_extension_ping",
    arguments: {},
  });
  assert.notEqual(result.isError, true);
  assert.deepEqual(result.content, [{ type: "text", text: "pong" }]);

  const rejected = await client.callTool({
    name: "vbot_extension_ping",
    arguments: { unexpected: true },
  });
  assert.equal(rejected.isError, true);

  await client.close();
  client = undefined;

  const oversizedBody = JSON.stringify({
    jsonrpc: "2.0",
    id: 101,
    method: "tools/list",
    padding: "x".repeat(70 * 1024),
  });
  const oversizedResponse = await fetch(endpoint, {
    method: "POST",
    headers: {
      accept: "application/json, text/event-stream",
      "content-type": "application/json",
    },
    body: oversizedBody,
  });
  assert.equal(oversizedResponse.status, 413);
  const oversizedError = await oversizedResponse.json();
  assert.equal(oversizedError.error.message, "Request body is too large");

  const chunkedOversizedResponse = await sendChunkedRequest(endpoint, [
    '{"jsonrpc":"2.0","padding":"',
    "x".repeat(40 * 1024),
    "x".repeat(40 * 1024),
    '"}',
  ]);
  assert.equal(chunkedOversizedResponse.status, 413);
  assert.equal(
    JSON.parse(chunkedOversizedResponse.body).error.message,
    "Request body is too large",
  );

  slowRequest = await openSlowRequest(endpoint);
  const busyResponse = await fetch(endpoint, {
    method: "POST",
    headers: {
      accept: "application/json, text/event-stream",
      "content-type": "application/json",
    },
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: 102,
      method: "tools/list",
      params: {},
    }),
  });
  assert.equal(busyResponse.status, 503);
  const busyError = await busyResponse.json();
  assert.equal(busyError.error.message, "Too many concurrent requests");

  await stopChild(child);
  childStopped = true;

  console.log(
    JSON.stringify({
      ok: true,
      endpoint,
      tools: ["vbot_extension_ping"],
      result: "pong",
      extraFieldsRejected: true,
      oversizedBodyRejected: true,
      chunkedOversizedBodyRejected: true,
      concurrencyLimited: true,
      shutdownWithActiveRequest: true,
    }),
  );
} finally {
  if (client) {
    await client.close().catch(() => {});
  }
  if (slowRequest) {
    slowRequest.destroy();
  }
  if (!childStopped) {
    await stopChild(child);
  }
}
