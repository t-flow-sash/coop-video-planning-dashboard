import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CoopWorkspace } from "@/features/coops/coop-workspace";

const base = {
  list: vi.fn(async () => []),
  detail: vi.fn(),
  audit: vi.fn(),
  transition: vi.fn(),
};

describe("operativer Importstatus", () => {
  it("zeigt verifizierte Zähler und kennzeichnet fehlende Coverage explizit", async () => {
    render(
      <CoopWorkspace
        repository={{
          ...base,
          semanticStatus: async () => ({
            as_of: "2026-10-21T11:00:00Z",
            domain_records: 21,
            tracks_total: 21,
            tracks_routed: 0,
            tracks_awaiting_route: 21,
            coverage_verified: false,
            coverage_note: "Message revisions are not business tracks",
            sources: [
              {
                enabled: true,
                discovered: 898,
                processed: 10,
                created: 2,
                updated: 3,
                unchanged: 1,
                review: 4,
                retry: 0,
                backlog: 888,
                oldest_backlog_at: "2026-10-20T11:00:00Z",
                latest_evidence_at: "2026-10-21T10:00:00Z",
                last_semantic_activity: "2026-10-21T10:30:00Z",
                collector_age_seconds: 3600,
                last_collector_run: {
                  mode: "DELTA",
                  status: "SUCCEEDED",
                  started_at: "2026-10-21T09:59:00Z",
                  finished_at: "2026-10-21T10:00:00Z",
                },
                threads_discovered: 900,
                threads_routed: 12,
                threads_awaiting_route: 888,
                oldest_awaiting_route_at: "2026-10-12T11:00:00Z",
              },
            ],
          }),
        }}
      />,
    );
    const panel = await screen.findByRole("region", {
      name: "Import und Coverage",
    });
    expect(panel).toHaveTextContent("898");
    expect(panel).toHaveTextContent("888");
    expect(panel).toHaveTextContent("CREATE 2");
    expect(panel).toHaveTextContent("UPDATE 3");
    expect(panel).toHaveTextContent("Review 4");
    expect(panel).toHaveTextContent("Threads entdeckt 900");
    expect(panel).toHaveTextContent("mit exakter Route 12");
    expect(panel).toHaveTextContent("ohne Route 888");
    expect(panel).toHaveTextContent(
      "Portfolio-Abdeckung: 0 von 21 Kooperationen mit exakter Route",
    );
    expect(panel).toHaveTextContent("ohne Route 21");
    expect(panel).toHaveTextContent(
      "Eine exakte Route ist keine inhaltliche Übereinstimmung",
    );
    expect(panel).toHaveTextContent("Coverage nicht verifiziert");
    expect(panel).toHaveTextContent(
      "Message revisions are not business tracks",
    );
    expect(panel).toHaveTextContent(
      "Collector-Erfolg ist keine semantische Verarbeitung",
    );
  });

  it("zeigt einen separaten unbekannten Zustand statt irreführender Nullen bei API-Ausfall", async () => {
    render(
      <CoopWorkspace
        repository={{
          ...base,
          semanticStatus: async () => {
            throw new Error("offline");
          },
        }}
      />,
    );
    expect(
      await screen.findByRole("region", { name: "Import und Coverage" }),
    ).toHaveTextContent("Status nicht verfügbar");
  });
});
