import assert from "node:assert/strict";
import http from "node:http";
import { after, before, test } from "node:test";

import { createGateway } from "../e2e-gateway.mjs";

const listen = (server) =>
  new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.off("error", reject);
      resolve(server.address().port);
    });
  });
const close = (server) => new Promise((resolve) => server.close(resolve));

let api;
let web;
let gateway;
let gatewayUrl;

before(async () => {
  api = http.createServer((request, response) => {
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      response.writeHead(201, {
        "content-type": "application/json",
        "x-upstream": "api",
      });
      response.end(
        JSON.stringify({
          method: request.method,
          path: request.url,
          body: Buffer.concat(chunks).toString(),
          forwardedHost: request.headers["x-forwarded-host"],
        }),
      );
    });
  });
  web = http.createServer((_request, response) => {
    response.writeHead(200, {
      "content-type": "text/plain",
      "set-cookie": ["one=1", "two=2"],
      "x-upstream": "web",
    });
    response.write("streamed ");
    setImmediate(() => response.end("web"));
  });

  const apiPort = await listen(api);
  const webPort = await listen(web);
  gateway = createGateway({
    apiOrigin: `http://127.0.0.1:${apiPort}`,
    webOrigin: `http://127.0.0.1:${webPort}`,
  });
  const gatewayPort = await listen(gateway);
  gatewayUrl = `http://127.0.0.1:${gatewayPort}`;
});

after(async () => {
  await Promise.all([close(gateway), close(api), close(web)]);
});

test("routet /api/v1 unverändert und streamt Methode, Body und Header", async () => {
  const response = await fetch(`${gatewayUrl}/api/v1/coops?limit=1`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: '{"status":"NEW"}',
  });

  assert.equal(response.status, 201);
  assert.equal(response.headers.get("x-upstream"), "api");
  assert.deepEqual(await response.json(), {
    method: "POST",
    path: "/api/v1/coops?limit=1",
    body: '{"status":"NEW"}',
    forwardedHost: new URL(gatewayUrl).host,
  });
});

test("routet alle anderen Pfade zum Web-Server und erhält Multi-Value-Header", async () => {
  const response = await fetch(`${gatewayUrl}/coops`);

  assert.equal(response.status, 200);
  assert.equal(response.headers.get("x-upstream"), "web");
  assert.equal(response.headers.get("set-cookie"), "one=1, two=2");
  assert.equal(await response.text(), "streamed web");
});

test("behandelt nur das vollständige /api/v1-Segment als API-Präfix", async () => {
  const response = await fetch(`${gatewayUrl}/api/v10`);

  assert.equal(response.headers.get("x-upstream"), "web");
});

test("stellt einen lokalen Gateway-Healthcheck bereit", async () => {
  const response = await fetch(`${gatewayUrl}/_gateway/health`);

  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { status: "ready" });
});
