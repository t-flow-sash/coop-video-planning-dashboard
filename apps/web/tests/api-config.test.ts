import { afterEach, describe, expect, it, vi } from "vitest";

async function loadRepository(apiUrl?: string) {
  if (apiUrl === undefined) {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
  } else {
    vi.stubEnv("NEXT_PUBLIC_API_URL", apiUrl);
  }
  vi.resetModules();
  return (await import("@/lib/api/coops")).apiRepository;
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.resetModules();
});

describe("API-Basis", () => {
  it("verwendet ohne Konfiguration same-origin /api/v1", async () => {
    const fetchMock = vi.fn(async () =>
      Response.json({ items: [], next_cursor: null }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const repository = await loadRepository();

    await repository.list();

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/coops?scope=all&limit=100",
      expect.objectContaining({
        headers: expect.objectContaining({ Accept: "application/json" }),
      }),
    );
  });
});
