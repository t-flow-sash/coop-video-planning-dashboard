import type { AuditEvent, Coop, CoopStatus } from "@/features/coops/domain";
import type { CoopRepository } from "@/lib/api/coops";

export const DEMO_STORAGE_KEY = "coop-m1-public-demo-v1";

export const DEMO_COOPS: Coop[] = [
  {
    id: "demo-coop-01",
    title: "Herbstküche Kurzvideo",
    status: "NEGOTIATING",
    brand: { id: "demo-brand-01", name: "Fabelpfad Küche" },
    agency: { id: "demo-agency-01", name: "Mosaik Studio" },
    contacts: [
      {
        id: "demo-contact-01",
        display_name: "Mara Beispiel",
        email: "mara@example.invalid",
        role: "PRIMARY",
      },
    ],
    summary:
      "Synthetisches Demo-Briefing: Rezeptidee und ruhige Produktintegration.",
    deliverables: ["1 Kurzvideo", "2 Story-Sequenzen"],
    next_action: "Konditionen abstimmen",
    release_at: null,
    fee_amount_minor: null,
    fee_currency: null,
    version: 1,
    created_at: "2026-10-01T09:00:00Z",
    updated_at: "2026-10-14T08:30:00Z",
    source: "gmail_preview",
    source_reference: "Synthetische Vorschau · DEMO-01",
    sync_state: "conflict",
    updated_by: "Demo-Nutzer:in",
  },
  {
    id: "demo-coop-02",
    title: "Winterroutine Reel",
    status: "CONFIRMED",
    brand: { id: "demo-brand-02", name: "Nordfaden Pflege" },
    agency: { id: "demo-agency-01", name: "Mosaik Studio" },
    contacts: [
      {
        id: "demo-contact-01",
        display_name: "Mara Beispiel",
        email: "mara@example.invalid",
        role: "PRIMARY",
      },
    ],
    summary: "Synthetischer Datensatz für wiederkehrenden Kontakt und Agentur.",
    deliverables: ["1 Reel"],
    next_action: "Produktionsfenster bestätigen",
    release_at: "2026-10-24T12:00:00Z",
    fee_amount_minor: 145000,
    fee_currency: "EUR",
    version: 2,
    created_at: "2026-10-02T10:00:00Z",
    updated_at: "2026-10-13T15:15:00Z",
    source: "manual",
    source_reference: null,
    sync_state: "local",
    updated_by: "Demo-Nutzer:in",
  },
  {
    id: "demo-coop-03",
    title: "Werkstatt Einblick",
    status: "IN_PRODUCTION",
    brand: { id: "demo-brand-03", name: "Kiesel & Kante" },
    agency: { id: "demo-agency-02", name: "Papierboot Kollektiv" },
    contacts: [
      {
        id: "demo-contact-02",
        display_name: "Noa Muster",
        email: "noa@example.invalid",
        role: "PRIMARY",
      },
    ],
    summary: "Synthetische Produktionsnotiz ohne externe Quelle.",
    deliverables: ["1 Video", "3 Standbilder"],
    next_action: "Rohschnitt vorbereiten",
    release_at: "2026-11-08T09:00:00Z",
    fee_amount_minor: 210000,
    fee_currency: "EUR",
    version: 3,
    created_at: "2026-09-27T11:00:00Z",
    updated_at: "2026-10-12T11:45:00Z",
    source: "manual",
    source_reference: null,
    sync_state: "synced",
    updated_by: "Demo-Nutzer:in",
  },
  {
    id: "demo-coop-04",
    title: "Lichtideen Tutorial",
    status: "REVIEW",
    brand: { id: "demo-brand-04", name: "Sonnenseite Leuchten" },
    agency: { id: "demo-agency-02", name: "Papierboot Kollektiv" },
    contacts: [
      {
        id: "demo-contact-03",
        display_name: "Noa Muster",
        email: "noa.zwei@example.invalid",
        role: "PRIMARY",
      },
    ],
    summary: "Gleichnamiger, aber absichtlich separater synthetischer Kontakt.",
    deliverables: ["1 Tutorial"],
    next_action: "Feedback einarbeiten",
    release_at: "2026-10-10T10:00:00Z",
    fee_amount_minor: 175000,
    fee_currency: "EUR",
    version: 2,
    created_at: "2026-09-20T08:00:00Z",
    updated_at: "2026-10-11T14:00:00Z",
    source: "gmail_preview",
    source_reference: "Synthetische Vorschau · DEMO-04",
    sync_state: "pending",
    updated_by: "Demo-Nutzer:in",
  },
  {
    id: "demo-coop-05",
    title: "Reise-Checkliste",
    status: "SCHEDULED",
    brand: { id: "demo-brand-05", name: "Fernweh Papier" },
    agency: null,
    contacts: [
      {
        id: "demo-contact-04",
        display_name: "Toni Test",
        email: "toni@example.invalid",
        role: "PRIMARY",
      },
    ],
    summary: null,
    deliverables: ["1 Kurzvideo"],
    next_action: null,
    release_at: "2026-10-18T09:00:00Z",
    fee_amount_minor: 98000,
    fee_currency: "EUR",
    version: 1,
    created_at: "2026-10-05T08:00:00Z",
    updated_at: "2026-10-10T10:00:00Z",
    source: "manual",
    source_reference: null,
    sync_state: "local",
    updated_by: "Demo-Nutzer:in",
  },
  {
    id: "demo-coop-06",
    title: "Frühlingsserie Archiv",
    status: "COMPLETED",
    brand: { id: "demo-brand-06", name: "Wolkenwiese Textil" },
    agency: null,
    contacts: [
      {
        id: "demo-contact-05",
        display_name: "Kim Platzhalter",
        email: "kim@example.invalid",
        role: "PRIMARY",
      },
    ],
    summary: "Abgeschlossener synthetischer Datensatz für den Statusfilter.",
    deliverables: ["2 Kurzvideos"],
    next_action: null,
    release_at: "2026-04-10T09:00:00Z",
    fee_amount_minor: 160000,
    fee_currency: "EUR",
    version: 4,
    created_at: "2026-03-01T09:00:00Z",
    updated_at: "2026-04-12T09:00:00Z",
    source: "manual",
    source_reference: null,
    sync_state: "local",
    updated_by: "Demo-Nutzer:in",
  },
  {
    id: "demo-coop-07",
    title: "Sommerformat Entwurf",
    status: "CANCELLED",
    brand: { id: "demo-brand-07", name: "Buntglas Garten" },
    agency: null,
    contacts: [],
    summary: "Abgesagter synthetischer Datensatz für den Abschlussfilter.",
    deliverables: [],
    next_action: null,
    release_at: null,
    fee_amount_minor: null,
    fee_currency: null,
    version: 2,
    created_at: "2026-06-01T09:00:00Z",
    updated_at: "2026-06-03T09:00:00Z",
    source: "manual",
    source_reference: null,
    sync_state: "local",
    updated_by: "Demo-Nutzer:in",
  },
];

const cloneFixtures = () => DEMO_COOPS.map((coop) => structuredClone(coop));

function readStored(): Coop[] {
  try {
    const raw = window.localStorage.getItem(DEMO_STORAGE_KEY);
    if (!raw) return cloneFixtures();
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as Coop[]) : cloneFixtures();
  } catch {
    return cloneFixtures();
  }
}

function persist(coops: Coop[]) {
  try {
    window.localStorage.setItem(DEMO_STORAGE_KEY, JSON.stringify(coops));
  } catch {
    // Storage can be unavailable in privacy mode; in-memory changes still work.
  }
}

const auditByCoop = new Map<string, AuditEvent[]>();

export const demoRepository: CoopRepository & { reset(): Coop[] } = {
  async list() {
    return readStored();
  },
  async detail(id) {
    const coop = readStored().find((item) => item.id === id);
    if (!coop) throw new Error("Demo-Datensatz nicht gefunden");
    return coop;
  },
  async audit(id) {
    return auditByCoop.get(id) ?? [];
  },
  async transition(id, status: CoopStatus, expectedVersion) {
    const coops = readStored();
    const index = coops.findIndex((item) => item.id === id);
    if (index < 0) throw new Error("Demo-Datensatz nicht gefunden");
    const current = coops[index];
    if (current.version !== expectedVersion) {
      throw Object.assign(new Error("Veraltete Demo-Version"), {
        status: 409,
        code: "stale_version",
      });
    }
    const updated: Coop = {
      ...current,
      status,
      version: current.version + 1,
      updated_at: new Date().toISOString(),
      updated_by: "Demo-Nutzer:in",
      sync_state: "local",
    };
    coops[index] = updated;
    persist(coops);
    auditByCoop.set(id, [
      {
        id: `demo-audit-${id}-${updated.version}`,
        action: "STATUS_TRANSITIONED",
        actor_type: "DEMO",
        source: "BROWSER_LOCAL",
        changes: { status: { old: current.status, new: status } },
        created_at: updated.updated_at,
      },
      ...(auditByCoop.get(id) ?? []),
    ]);
    return updated;
  },
  reset() {
    const fixtures = cloneFixtures();
    persist(fixtures);
    auditByCoop.clear();
    return fixtures;
  },
};
