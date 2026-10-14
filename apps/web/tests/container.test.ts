import { readFile } from "node:fs/promises";
import { describe, expect, it } from "vitest";

const dockerfilePath = new URL("file://" + process.cwd() + "/Dockerfile");
const dockerignorePath = new URL(
  "file://" + process.cwd() + "/Dockerfile.dockerignore",
);

describe("Web-Container", () => {
  it("baut Next standalone mehrstufig mit einer same-origin API-Basis", async () => {
    const dockerfile = await readFile(dockerfilePath, "utf8");

    expect(dockerfile.match(/^FROM /gm)).toHaveLength(3);
    expect(dockerfile).toContain("ARG NEXT_PUBLIC_API_URL=/api/v1");
    expect(dockerfile).toContain(
      "ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL",
    );
    expect(dockerfile).toContain("pnpm --filter web build");
    expect(dockerfile).toContain(".next/standalone");
  });

  it("läuft non-root und besitzt einen Healthcheck ohne Schreibbedarf im Root-Dateisystem", async () => {
    const dockerfile = await readFile(dockerfilePath, "utf8");

    expect(dockerfile).toMatch(/USER nextjs\s*$/m);
    expect(dockerfile).toContain("HEALTHCHECK");
    expect(dockerfile).toContain("http://127.0.0.1:3000/");
    expect(dockerfile).not.toMatch(/npm install -g|apk add.*curl/);
  });

  it("hält Build-Kontext, Secrets und lokale Artefakte klein", async () => {
    const dockerignore = await readFile(dockerignorePath, "utf8");

    for (const ignored of [
      ".git",
      "node_modules",
      ".next",
      ".env",
      "apps/api",
      "artifacts",
    ]) {
      expect(dockerignore).toContain(ignored);
    }
  });
});
