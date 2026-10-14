import { afterEach, describe, expect, it, vi } from "vitest";

async function loadConfig(demoMode) {
  vi.stubEnv("NEXT_PUBLIC_DEMO_MODE", demoMode ? "true" : "false");
  vi.resetModules();
  return (await import("../next.config.mjs")).default;
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

describe("Next-Ausgabemodus", () => {
  it("erzeugt im Produktionsmodus einen standalone Server", async () => {
    const config = await loadConfig(false);

    expect(config.output).toBe("standalone");
    expect(config.basePath).toBe("");
  });

  it("behält den statischen Export ausschließlich für die Demo", async () => {
    const config = await loadConfig(true);

    expect(config.output).toBe("export");
    expect(config.basePath).toBe("/coop-video-planning-dashboard");
  });
});
