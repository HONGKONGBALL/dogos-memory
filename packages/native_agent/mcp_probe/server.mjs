#!/usr/bin/env node

import http from "node:http";
import { createRequire } from "node:module";
import { TextDecoder } from "node:util";

const HOST = "127.0.0.1";
const DEFAULT_PORT = 8768;
const MCP_PATH = "/mcp";
const MAX_REQUEST_BODY_BYTES = 64 * 1024;
const DEFAULT_MAX_CONCURRENT_REQUESTS = 16;
const MAX_CONFIGURED_CONCURRENT_REQUESTS = 64;
const REQUEST_TIMEOUT_MS = 10_000;
const HEADERS_TIMEOUT_MS = 5_000;
const KEEP_ALIVE_TIMEOUT_MS = 1_000;
const SHUTDOWN_TIMEOUT_MS = 1_000;

// Resolve from the repository package so the probe is self-contained.
const requireFromProject = createRequire(
  new URL("../../../package.json", import.meta.url),
);

let McpServer;
let StreamableHTTPServerTransport;
let z;

try {
  ({ McpServer } = requireFromProject(
    "@modelcontextprotocol/sdk/server/mcp.js",
  ));
  ({ StreamableHTTPServerTransport } = requireFromProject(
    "@modelcontextprotocol/sdk/server/streamableHttp.js",
  ));
  ({ z } = requireFromProject("zod"));
} catch (error) {
  throw new Error(
    "The DogOS MCP dependencies are unavailable. Run npm install before " +
      "starting this probe.",
    { cause: error },
  );
}

class RequestRejection extends Error {
  constructor(statusCode, rpcCode, publicMessage) {
    super(publicMessage);
    this.statusCode = statusCode;
    this.rpcCode = rpcCode;
    this.publicMessage = publicMessage;
  }
}

function parsePort(rawPort) {
  if (rawPort === undefined || rawPort === "") {
    return DEFAULT_PORT;
  }

  const port = Number(rawPort);
  if (!Number.isInteger(port) || port < 0 || port > 65535) {
    throw new Error("MCP_PROBE_PORT must be an integer from 0 through 65535");
  }
  return port;
}

function parseConcurrency(rawValue) {
  if (rawValue === undefined || rawValue === "") {
    return DEFAULT_MAX_CONCURRENT_REQUESTS;
  }

  const value = Number(rawValue);
  if (
    !Number.isInteger(value) ||
    value < 1 ||
    value > MAX_CONFIGURED_CONCURRENT_REQUESTS
  ) {
    throw new Error(
      `MCP_PROBE_MAX_CONCURRENT must be an integer from 1 through ${MAX_CONFIGURED_CONCURRENT_REQUESTS}`,
    );
  }
  return value;
}

function createProtocolServer() {
  const server = new McpServer({
    name: "vbot-extension-registration-probe",
    version: "0.1.0",
  });

  server.registerTool(
    "vbot_extension_ping",
    {
      description: "Return pong to verify that a client can register and call this MCP server.",
      inputSchema: z.object({}).strict(),
      annotations: {
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
      },
    },
    async () => ({
      content: [{ type: "text", text: "pong" }],
    }),
  );

  return server;
}

function sendJsonRpcError(response, statusCode, code, message) {
  if (response.destroyed || response.writableEnded) {
    return;
  }
  if (response.headersSent) {
    response.end();
    return;
  }

  const body = Buffer.from(
    JSON.stringify({
      jsonrpc: "2.0",
      error: { code, message },
      id: null,
    }),
  );
  response.shouldKeepAlive = false;
  response.writeHead(statusCode, {
    connection: "close",
    "content-length": body.byteLength,
    "content-type": "application/json; charset=utf-8",
  });
  response.end(body);
}

function rejectAndDrain(request, response, rejection) {
  sendJsonRpcError(
    response,
    rejection.statusCode,
    rejection.rpcCode,
    rejection.publicMessage,
  );
  if (!request.complete && !request.destroyed) {
    request.resume();
  }
}

function readBoundedJsonBody(request, timeoutMs) {
  return new Promise((resolve, reject) => {
    const declaredLength = request.headers["content-length"];
    if (Array.isArray(declaredLength)) {
      reject(new RequestRejection(400, -32700, "Invalid Content-Length"));
      return;
    }
    if (declaredLength !== undefined) {
      if (!/^(0|[1-9]\d*)$/.test(declaredLength)) {
        reject(new RequestRejection(400, -32700, "Invalid Content-Length"));
        return;
      }
      if (BigInt(declaredLength) > BigInt(MAX_REQUEST_BODY_BYTES)) {
        reject(new RequestRejection(413, -32000, "Request body is too large"));
        return;
      }
    }

    const chunks = [];
    let receivedBytes = 0;
    let settled = false;
    let timer;

    const cleanup = () => {
      clearTimeout(timer);
      request.off("data", onData);
      request.off("end", onEnd);
      request.off("aborted", onAborted);
      request.off("error", onError);
    };

    const finish = (error, value) => {
      if (settled) {
        return;
      }
      settled = true;
      cleanup();
      if (error) {
        request.pause();
        reject(error);
      } else {
        resolve(value);
      }
    };

    const onData = (chunk) => {
      const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      receivedBytes += buffer.byteLength;
      if (receivedBytes > MAX_REQUEST_BODY_BYTES) {
        finish(new RequestRejection(413, -32000, "Request body is too large"));
        return;
      }
      chunks.push(buffer);
    };

    const onEnd = () => {
      if (receivedBytes === 0) {
        finish(new RequestRejection(400, -32700, "Request body must be JSON"));
        return;
      }

      let text;
      try {
        text = new TextDecoder("utf-8", { fatal: true }).decode(
          Buffer.concat(chunks, receivedBytes),
        );
      } catch {
        finish(new RequestRejection(400, -32700, "Request body must be valid UTF-8"));
        return;
      }

      try {
        finish(undefined, JSON.parse(text));
      } catch {
        finish(new RequestRejection(400, -32700, "Request body must be valid JSON"));
      }
    };

    const onAborted = () => {
      finish(new RequestRejection(400, -32700, "Request body was aborted"));
    };
    const onError = () => {
      finish(new RequestRejection(400, -32700, "Request body could not be read"));
    };

    timer = setTimeout(() => {
      finish(new RequestRejection(408, -32001, "Request timed out"));
    }, timeoutMs);
    request.on("data", onData);
    request.once("end", onEnd);
    request.once("aborted", onAborted);
    request.once("error", onError);
  });
}

function withTimeout(promise, timeoutMs) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new RequestRejection(408, -32001, "Request timed out")),
      timeoutMs,
    );
    Promise.resolve(promise).then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}

async function settleWithin(promise, timeoutMs) {
  let timer;
  await Promise.race([
    Promise.resolve(promise),
    new Promise((resolve) => {
      timer = setTimeout(resolve, timeoutMs);
    }),
  ]);
  clearTimeout(timer);
}

const requestedPort = parsePort(process.env.MCP_PROBE_PORT);
const maxConcurrentRequests = parseConcurrency(
  process.env.MCP_PROBE_MAX_CONCURRENT,
);
const sockets = new Set();
const activeProtocolClosers = new Set();
let activeRequests = 0;
let stopping = false;
let stopPromise;

const httpServer = http.createServer(async (request, response) => {
  const requestStartedAt = Date.now();
  const pathname = new URL(request.url ?? "/", "http://127.0.0.1").pathname;

  if (stopping) {
    rejectAndDrain(
      request,
      response,
      new RequestRejection(503, -32002, "Server is shutting down"),
    );
    return;
  }

  if (pathname !== MCP_PATH) {
    rejectAndDrain(
      request,
      response,
      new RequestRejection(404, -32001, "Not found"),
    );
    return;
  }

  if (request.method !== "POST") {
    response.setHeader("allow", "POST");
    rejectAndDrain(
      request,
      response,
      new RequestRejection(405, -32000, "Method not allowed"),
    );
    return;
  }

  const address = httpServer.address();
  if (address === null || typeof address === "string") {
    rejectAndDrain(
      request,
      response,
      new RequestRejection(503, -32603, "Server is not ready"),
    );
    return;
  }

  if (activeRequests >= maxConcurrentRequests) {
    rejectAndDrain(
      request,
      response,
      new RequestRejection(503, -32002, "Too many concurrent requests"),
    );
    return;
  }

  activeRequests += 1;
  let closeRequest;
  try {
    const parsedBody = await readBoundedJsonBody(request, REQUEST_TIMEOUT_MS);
    const elapsedMs = Date.now() - requestStartedAt;
    const remainingMs = Math.max(1, REQUEST_TIMEOUT_MS - elapsedMs);

    const protocolServer = createProtocolServer();
    const transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: undefined,
      enableJsonResponse: true,
      enableDnsRebindingProtection: true,
      allowedHosts: [`127.0.0.1:${address.port}`, `localhost:${address.port}`],
    });

    let closePromise;
    closeRequest = () => {
      if (closePromise === undefined) {
        closePromise = Promise.allSettled([
          transport.close(),
          protocolServer.close(),
        ]).then(() => undefined);
      }
      return closePromise;
    };
    activeProtocolClosers.add(closeRequest);
    response.once("close", () => {
      void closeRequest();
    });

    await protocolServer.connect(transport);
    await withTimeout(
      transport.handleRequest(request, response, parsedBody),
      remainingMs,
    );
  } catch (error) {
    if (error instanceof RequestRejection) {
      rejectAndDrain(request, response, error);
    } else if (!stopping) {
      console.error("MCP request failed with an internal error");
      rejectAndDrain(
        request,
        response,
        new RequestRejection(500, -32603, "Internal server error"),
      );
    }
  } finally {
    if (closeRequest !== undefined) {
      await closeRequest();
      activeProtocolClosers.delete(closeRequest);
    }
    activeRequests -= 1;
  }
});

httpServer.headersTimeout = HEADERS_TIMEOUT_MS;
httpServer.requestTimeout = REQUEST_TIMEOUT_MS;
httpServer.keepAliveTimeout = KEEP_ALIVE_TIMEOUT_MS;
httpServer.maxHeadersCount = 64;
httpServer.setTimeout(REQUEST_TIMEOUT_MS, (socket) => {
  socket.destroy();
});

httpServer.on("connection", (socket) => {
  sockets.add(socket);
  socket.once("close", () => {
    sockets.delete(socket);
  });
});

httpServer.on("clientError", (_error, socket) => {
  socket.end("HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n");
});

httpServer.listen({ host: HOST, port: requestedPort, exclusive: true }, () => {
  const address = httpServer.address();
  if (address === null || typeof address === "string") {
    throw new Error("Unable to determine the MCP probe listening address");
  }
  console.log(`MCP probe listening at http://${HOST}:${address.port}${MCP_PATH}`);
});

async function stop() {
  if (stopPromise !== undefined) {
    return stopPromise;
  }
  stopping = true;
  stopPromise = (async () => {
    const serverClosed = new Promise((resolve) => {
      httpServer.close(() => resolve());
    });
    const protocolCloses = [...activeProtocolClosers].map((close) => close());

    httpServer.closeIdleConnections?.();
    for (const socket of sockets) {
      socket.destroy();
    }
    httpServer.closeAllConnections?.();

    await settleWithin(
      Promise.allSettled([serverClosed, ...protocolCloses]),
      SHUTDOWN_TIMEOUT_MS,
    );

    for (const socket of sockets) {
      socket.destroy();
    }
  })();
  return stopPromise;
}

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.once(signal, () => {
    void stop()
      .then(() => process.exit(0))
      .catch(() => {
        console.error("Failed to stop MCP probe");
        process.exit(1);
      });
  });
}
