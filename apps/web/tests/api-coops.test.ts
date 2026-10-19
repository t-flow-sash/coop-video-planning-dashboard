import { afterEach, describe, expect, it, vi } from "vitest";

import { apiRepository } from "@/lib/api/coops";

const coop = (id: string) => ({
  id,
  title: id,
  status: "NEW",
  summary: null,
  deliverables: [],
  next_action: null,
  release_at: null,
  fee_amount_minor: null,
  fee_currency: null,
  version: 1,
  created_at: "2026-10-15T00:00:00Z",
  updated_at: "2026-10-15T00:00:00Z",
});

const jsonResponse = (body: object) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });

describe("apiRepository.list", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("loads every cursor page without per-coop detail requests", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({ items: [coop("coop-1")], next_cursor: "page 2" }),
      )
      .mockResolvedValueOnce(
        jsonResponse({ items: [coop("coop-2")], next_cursor: null }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await expect(apiRepository.list()).resolves.toMatchObject([
      { id: "coop-1" },
      { id: "coop-2" },
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/v1/coops?scope=all&limit=100",
      "/api/v1/coops?scope=all&limit=100&cursor=page%202",
    ]);
  });

  it("rejects a repeated cursor instead of returning a partial overview", async () => {
    const repeatedPage = jsonResponse({
      items: [coop("coop-1")],
      next_cursor: "same-cursor",
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(repeatedPage)
      .mockResolvedValueOnce(
        jsonResponse({ items: [coop("coop-2")], next_cursor: "same-cursor" }),
      )
      .mockRejectedValue(new Error("unexpected extra request"));
    vi.stubGlobal("fetch", fetchMock);

    await expect(apiRepository.list()).rejects.toThrow(/Cursor-Schleife/);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("rejects after the hard page limit instead of returning a partial overview", async () => {
    const fetchMock = vi.fn(async () => {
      const page = fetchMock.mock.calls.length;
      if (page > 100) throw new Error("unexpected extra request");
      return jsonResponse({
        items: [coop(`coop-${page}`)],
        next_cursor: `cursor-${page}`,
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(apiRepository.list()).rejects.toThrow(/Obergrenze/);
    expect(fetchMock).toHaveBeenCalledTimes(100);
  });
});

describe("apiRepository.audit", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("loads every audit cursor page", async () => {
    const event = (id: string) => ({
      id,
      coop_id: "coop-1",
      action: "COOP_UPDATED",
      actor_type: "USER",
      actor_id: null,
      source: "API",
      changes: {},
      reason: null,
      created_at: "2026-10-15T00:00:00Z",
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({ items: [event("event-1")], next_cursor: "audit 2" }),
      )
      .mockResolvedValueOnce(
        jsonResponse({ items: [event("event-2")], next_cursor: null }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await expect(apiRepository.audit("coop-1")).resolves.toMatchObject([
      { id: "event-1" },
      { id: "event-2" },
    ]);
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/v1/coops/coop-1/audit-events?limit=100",
      "/api/v1/coops/coop-1/audit-events?limit=100&cursor=audit%202",
    ]);
  });
});

describe("apiRepository.transition", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns the committed POST representation without a follow-up GET", async () => {
    const representation = {
      id: "coop-1",
      title: "Launch",
      status: "REVIEW",
      summary: null,
      deliverables: null,
      next_action: null,
      release_at: null,
      fee_amount_minor: null,
      fee_currency: null,
      completed_at: null,
      brand_organization_id: null,
      agency_organization_id: null,
      version: 2,
      created_at: "2026-10-15T00:00:00Z",
      updated_at: "2026-10-15T00:01:00Z",
    };

    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(representation))
      .mockResolvedValueOnce(
        jsonResponse({ ...representation, status: "SCHEDULED", version: 1 }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const updated = await apiRepository.transition("coop-1", "REVIEW", 1);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/coops/coop-1/transitions",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          target_status: "REVIEW",
          expected_version: 1,
        }),
      }),
    );
    expect(updated).toMatchObject({
      id: "coop-1",
      status: "REVIEW",
      version: 2,
      contacts: [],
      deliverables: [],
    });
  });
});
