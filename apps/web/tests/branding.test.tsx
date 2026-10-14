import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

async function loadLayout(demoMode: boolean) {
  vi.stubEnv("NEXT_PUBLIC_DEMO_MODE", demoMode ? "true" : "false");
  vi.resetModules();
  return import("@/app/layout");
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

describe("Produktions-Branding", () => {
  it("entfernt M1-Demo-Texte aus Metadaten und Navigation", async () => {
    const { default: RootLayout, metadata } = await loadLayout(false);
    const html = renderToStaticMarkup(
      <RootLayout>
        <p>Inhalt</p>
      </RootLayout>,
    );

    expect(metadata.title).toBe("Coop-Planung");
    expect(metadata.description).toBe(
      "Arbeitsoberfläche für die Planung von Kooperationen und Veröffentlichungen",
    );
    expect(html).not.toContain("öffentliche M1-Demo");
    expect(html).not.toContain("M1 · Demo");
  });
});

describe("Demo-Branding", () => {
  it("kennzeichnet Metadaten und Navigation eindeutig als öffentliche Demo", async () => {
    const { default: RootLayout, metadata } = await loadLayout(true);
    const html = renderToStaticMarkup(
      <RootLayout>
        <p>Inhalt</p>
      </RootLayout>,
    );

    expect(metadata.title).toBe("Coop-Planung · öffentliche Demo");
    expect(metadata.description).toContain("synthetischen Beispieldaten");
    expect(html).toContain("Öffentliche Demo");
  });
});
