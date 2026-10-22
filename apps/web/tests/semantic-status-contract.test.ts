import { readFileSync } from "node:fs";
import { basename, resolve } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * The dashboard renders the operational semantic status from the authenticated
 * API, but the client types are hand-maintained. An untyped or stale API
 * contract silently allows leaked evidence identifiers and client drift, so the
 * checked-in OpenAPI contract and the workspace types must agree exactly.
 */
const webRoot = resolve(process.cwd());
if (basename(webRoot) !== "web") {
  throw new Error(`expected to run from apps/web, got ${webRoot}`);
}
const contractPath = resolve(webRoot, "../../packages/api-client/openapi.json");
const sourcePath = resolve(webRoot, "lib/api/coops.ts");

type JsonSchema = { properties?: Record<string, unknown>; required?: string[] };

function interfaceFields(source: string, name: string): Map<string, string> {
  const match = new RegExp(
    String.raw`export interface ${name} \{([\s\S]*?)\n\}`,
  ).exec(source);
  if (!match) {
    throw new Error(`interface ${name} is missing from lib/api/coops.ts`);
  }
  const fields = new Map<string, string>();
  for (const line of match[1].split("\n")) {
    const member = /^ {2}([a-z_][a-z0-9_]*)\??: (.+);$/.exec(line);
    if (member) fields.set(member[1], member[2]);
  }
  return fields;
}

function schemaFor(name: string): JsonSchema {
  const contract = JSON.parse(readFileSync(contractPath, "utf8")) as {
    components: { schemas: Record<string, JsonSchema> };
  };
  const schema = contract.components.schemas[name];
  if (!schema) throw new Error(`schema ${name} is missing from the contract`);
  return schema;
}

const cases: Array<[string, string]> = [
  ["SemanticStatus", "SemanticStatus"],
  ["SemanticSourceStatus", "SemanticSourceStatus"],
  ["SemanticCollectorRun", "SemanticCollectorRun"],
];

describe("semantischer API-Vertrag", () => {
  it.each(cases)(
    "deklariert für %s exakt die Felder des vertraglich geprüften Schemas",
    (interfaceName, schemaName) => {
      const source = readFileSync(sourcePath, "utf8");
      const fields = interfaceFields(source, interfaceName);
      const schema = schemaFor(schemaName);
      expect([...fields.keys()].sort()).toEqual(
        Object.keys(schema.properties ?? {}).sort(),
      );
      expect([...fields.keys()].sort()).toEqual(
        [...(schema.required ?? [])].sort(),
      );
    },
  );

  it("markiert unbekannte Frische- und Rückstandswerte als null statt als Zahl", () => {
    const source = readFileSync(sourcePath, "utf8");
    const fields = interfaceFields(source, "SemanticSourceStatus");
    for (const name of [
      "oldest_backlog_at",
      "latest_evidence_at",
      "last_semantic_activity",
      "collector_age_seconds",
      "last_collector_run",
    ]) {
      expect(fields.get(name), name).toContain("null");
    }
  });

  it("gibt keine internen Beleg-Identitäten an die Oberfläche weiter", () => {
    const contract = JSON.parse(readFileSync(contractPath, "utf8")) as {
      components: { schemas: Record<string, JsonSchema> };
    };
    const forbidden = [
      "external_id",
      "source_key",
      "record_key",
      "track_key",
      "thread_id",
      "message_id",
      "body",
      "provenance",
    ];
    for (const schemaName of [
      "SemanticStatus",
      "SemanticSourceStatus",
      "SemanticCollectorRun",
    ]) {
      const properties = Object.keys(
        contract.components.schemas[schemaName].properties ?? {},
      );
      expect(
        properties.filter((key) => forbidden.includes(key)),
        schemaName,
      ).toEqual([]);
    }
  });
});
