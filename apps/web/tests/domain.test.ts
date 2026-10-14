import { describe, expect, it } from "vitest";
import {
  allowedTransitionTargets,
  filterAndSortCoops,
  relationshipIndex,
  statusLabel,
  type Coop,
} from "@/features/coops/domain";

const base = (overrides: Partial<Coop>): Coop => ({
  id: "1",
  title: "Launch Reel",
  status: "NEGOTIATING",
  brand: { id: "brand-1", name: "Nordlicht" },
  agency: { id: "agency-1", name: "Studio Nord" },
  contacts: [
    {
      id: "contact-1",
      display_name: "Lea Berg",
      email: "lea@example.invalid",
      role: "PRIMARY",
    },
  ],
  summary: null,
  deliverables: [],
  next_action: null,
  release_at: null,
  fee_amount_minor: null,
  fee_currency: null,
  version: 1,
  created_at: "2026-10-01T10:00:00Z",
  updated_at: "2026-10-10T10:00:00Z",
  ...overrides,
});

const coops = [
  base({ id: "1" }),
  base({ id: "2", title: "Winter Short", status: "CONFIRMED" }),
  base({
    id: "3",
    title: "Archiv",
    status: "COMPLETED",
    contacts: [],
    agency: null,
  }),
];

describe("Coop-Domäne", () => {
  it("mappt die Architekturstatus auf verbindliche deutsche Labels", () => {
    expect(statusLabel("NEW")).toBe("Eingang");
    expect(statusLabel("PUBLISHED")).toBe("Veröffentlicht");
    expect(statusLabel("DECLINED")).toBe("Abgelehnt");
  });

  it("verwendet exakt die Transition-Matrix der Architektur", () => {
    expect(allowedTransitionTargets("NEW")).toEqual([
      "QUALIFYING",
      "NEGOTIATING",
      "DECLINED",
    ]);
    expect(allowedTransitionTargets("SCHEDULED")).toEqual([
      "REVIEW",
      "PUBLISHED",
      "CANCELLED",
    ]);
    expect(allowedTransitionTargets("COMPLETED")).toEqual([]);
  });

  it("zeigt standardmäßig nur offene Kooperationen und kombiniert Suche mit Filtern", () => {
    expect(
      filterAndSortCoops(coops, {
        q: "studio nord",
        scope: "open",
        statuses: [],
        sort: "updated",
      }),
    ).toHaveLength(2);
    expect(
      filterAndSortCoops(coops, {
        q: "archiv",
        scope: "open",
        statuses: [],
        sort: "updated",
      }),
    ).toHaveLength(0);
  });

  it("berechnet wiederkehrende Kontakte und Agenturen ausschließlich aus offenen Coops", () => {
    const index = relationshipIndex(coops);
    expect(index.contacts.get("contact-1")?.length).toBe(2);
    expect(index.agencies.get("agency-1")?.length).toBe(2);
  });
});
