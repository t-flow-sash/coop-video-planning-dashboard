import { afterEach, describe, expect, it, vi } from "vitest";

import { apiRepository } from "@/lib/api/coops";

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
    const jsonResponse = (body: object) =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
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
