#!/usr/bin/env node

import http from "node:http";
import { pathToFileURL } from "node:url";

const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "proxy-connection",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

function forwardedHeaders(request, target) {
  const headers = {};
  for (const [name, value] of Object.entries(request.headers)) {
    if (!HOP_BY_HOP_HEADERS.has(name) && value !== undefined) {
      headers[name] = value;
    }
  }

  const remoteAddress = request.socket.remoteAddress;
  headers.host = target.host;
  headers["x-forwarded-host"] = request.headers.host ?? "";
  headers["x-forwarded-proto"] = "http";
  if (remoteAddress) {
    const existing = request.headers["x-forwarded-for"];
    headers["x-forwarded-for"] = existing
      ? `${existing}, ${remoteAddress}`
      : remoteAddress;
  }
  return headers;
}

function responseHeaders(headers) {
  return Object.fromEntries(
    Object.entries(headers).filter(
      ([name, value]) => !HOP_BY_HOP_HEADERS.has(name) && value !== undefined,
    ),
  );
}

function proxy(request, response, origin) {
  const target = new URL(request.url ?? "/", origin);
  const upstreamRequest = http.request(
    target,
    {
      method: request.method,
      headers: forwardedHeaders(request, target),
    },
    (upstreamResponse) => {
      response.writeHead(
        upstreamResponse.statusCode ?? 502,
        upstreamResponse.statusMessage,
        responseHeaders(upstreamResponse.headers),
      );
      upstreamResponse.pipe(response);
    },
  );

  upstreamRequest.on("error", (error) => {
    if (response.headersSent) {
      response.destroy(error);
      return;
    }
    response.writeHead(502, { "content-type": "application/json" });
    response.end(
      JSON.stringify({
        error: "gateway_upstream_unavailable",
        upstream: origin.origin,
      }),
    );
  });
  request.on("aborted", () => upstreamRequest.destroy());
  request.pipe(upstreamRequest);
}

export function createGateway({ apiOrigin, webOrigin }) {
  const api = new URL(apiOrigin);
  const web = new URL(webOrigin);

  return http.createServer((request, response) => {
    const pathname = new URL(request.url ?? "/", "http://gateway.local")
      .pathname;
    if (pathname === "/_gateway/health") {
      response.writeHead(200, { "content-type": "application/json" });
      response.end('{"status":"ready"}');
      return;
    }

    const isApiPath = pathname === "/api/v1" || pathname.startsWith("/api/v1/");
    proxy(request, response, isApiPath ? api : web);
  });
}

function start() {
  const host = process.env.GATEWAY_HOST ?? "127.0.0.1";
  const port = Number.parseInt(process.env.GATEWAY_PORT ?? "3001", 10);
  const server = createGateway({
    apiOrigin: process.env.API_ORIGIN ?? "http://127.0.0.1:8000",
    webOrigin: process.env.WEB_ORIGIN ?? "http://127.0.0.1:3000",
  });

  const shutdown = () => {
    server.close((error) => {
      if (error) console.error(error);
      process.exitCode = error ? 1 : 0;
    });
  };
  process.once("SIGINT", shutdown);
  process.once("SIGTERM", shutdown);
  server.listen(port, host, () => {
    console.log(`E2E gateway ready at http://${host}:${port}`);
  });
}

if (
  process.argv[1] &&
  pathToFileURL(process.argv[1]).href === import.meta.url
) {
  start();
}
