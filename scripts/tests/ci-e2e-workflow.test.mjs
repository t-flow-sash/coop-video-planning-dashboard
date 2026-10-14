import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const workflowPath = new URL("../../.github/workflows/ci.yml", import.meta.url);

test("E2E-CI nutzt Standalone über das lokale Same-Origin-Gateway", async () => {
  const workflow = await readFile(workflowPath, "utf8");

  assert.match(workflow, /node apps\/web\/server\.js/);
  assert.match(workflow, /cp -R apps\/web\/\.next\/static/);
  assert.match(
    workflow,
    /backend test fixtures truncate[\s\S]*uv run --project apps\/api python -m app\.seed[\s\S]*uvicorn/,
  );
  assert.match(workflow, /node scripts\/e2e-gateway\.mjs/);
  assert.match(workflow, /E2E_BASE_URL=http:\/\/127\.0\.0\.1:3001/);
  assert.match(workflow, /127\.0\.0\.1:3001\/api\/v1\/health\/ready/);
});

test("E2E-CI beendet alle Server begrenzt und wartet ihre PIDs ab", async () => {
  const workflow = await readFile(workflowPath, "utf8");

  assert.match(workflow, /gateway_pid/);
  assert.match(workflow, /kill -TERM/);
  assert.match(workflow, /kill -KILL/);
  assert.match(workflow, /wait "\$pid"/);
});
