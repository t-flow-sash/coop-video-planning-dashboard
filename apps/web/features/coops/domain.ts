export const STATUSES = [
  "NEW",
  "QUALIFYING",
  "NEGOTIATING",
  "CONFIRMED",
  "IN_PRODUCTION",
  "REVIEW",
  "SCHEDULED",
  "PUBLISHED",
  "COMPLETED",
  "DECLINED",
  "CANCELLED",
] as const;

export type CoopStatus = (typeof STATUSES)[number];
export type SortKey = "updated" | "release" | "status" | "brand" | "fee";
export type ReleaseFilter = "overdue" | "14d" | "later" | "missing" | null;
export type QualityFilter =
  | "missing-release"
  | "missing-fee"
  | "sync-issue"
  | null;

export const STATUS_LABELS: Record<CoopStatus, string> = {
  NEW: "Eingang",
  QUALIFYING: "In Klärung",
  NEGOTIATING: "Verhandlung",
  CONFIRMED: "Vereinbart",
  IN_PRODUCTION: "In Produktion",
  REVIEW: "In Freigabe",
  SCHEDULED: "Eingeplant",
  PUBLISHED: "Veröffentlicht",
  COMPLETED: "Abgeschlossen",
  DECLINED: "Abgelehnt",
  CANCELLED: "Abgesagt",
};

export const TERMINAL_STATUSES = new Set<CoopStatus>([
  "COMPLETED",
  "DECLINED",
  "CANCELLED",
]);

const TRANSITIONS: Record<CoopStatus, CoopStatus[]> = {
  NEW: ["QUALIFYING", "NEGOTIATING", "DECLINED"],
  QUALIFYING: ["NEGOTIATING", "CONFIRMED", "DECLINED"],
  NEGOTIATING: ["QUALIFYING", "CONFIRMED", "DECLINED", "CANCELLED"],
  CONFIRMED: ["NEGOTIATING", "IN_PRODUCTION", "CANCELLED"],
  IN_PRODUCTION: ["CONFIRMED", "REVIEW", "SCHEDULED", "CANCELLED"],
  REVIEW: ["IN_PRODUCTION", "SCHEDULED", "CANCELLED"],
  SCHEDULED: ["REVIEW", "PUBLISHED", "CANCELLED"],
  PUBLISHED: ["REVIEW", "COMPLETED"],
  COMPLETED: [],
  DECLINED: [],
  CANCELLED: [],
};

export const allowedTransitionTargets = (status: CoopStatus) =>
  TRANSITIONS[status];
export const isOpen = (coop: Coop) => !TERMINAL_STATUSES.has(coop.status);
export const statusLabel = (status: CoopStatus) => STATUS_LABELS[status];

export interface Organization {
  id: string;
  name: string;
}
export interface Contact {
  id: string;
  display_name: string;
  email: string | null;
  role?: string;
}
export interface Coop {
  id: string;
  title: string;
  status: CoopStatus;
  brand: Organization | null;
  agency: Organization | null;
  contacts: Contact[];
  summary: string | null;
  deliverables: Array<
    string | { type?: string; name?: string; quantity?: number }
  >;
  next_action: string | null;
  release_at: string | null;
  fee_amount_minor: number | null;
  fee_currency: string | null;
  version: number;
  created_at: string;
  updated_at: string;
  source?: string;
  source_reference?: string | null;
  sync_state?: "local" | "synced" | "pending" | "conflict" | "error";
  updated_by?: string;
}

export interface AuditEvent {
  id: string;
  action: string;
  actor_type: string;
  source: string;
  changes: Record<string, { old: unknown; new: unknown }>;
  reason?: string | null;
  created_at: string;
}

export interface QueryState {
  q: string;
  scope: "open" | "all";
  statuses: CoopStatus[];
  sort: SortKey;
  release?: ReleaseFilter;
  quality?: QualityFilter;
  contact?: string | null;
  agency?: string | null;
}

export function relationshipIndex(coops: Coop[]) {
  const contacts = new Map<string, Coop[]>();
  const agencies = new Map<string, Coop[]>();
  for (const coop of coops.filter(isOpen)) {
    for (const contact of coop.contacts)
      contacts.set(contact.id, [...(contacts.get(contact.id) ?? []), coop]);
    if (coop.agency)
      agencies.set(coop.agency.id, [
        ...(agencies.get(coop.agency.id) ?? []),
        coop,
      ]);
  }
  return { contacts, agencies };
}

const day = (value: string) => value.slice(0, 10);
export function matchesRelease(
  coop: Coop,
  release: ReleaseFilter | undefined,
  today = new Date(),
): boolean {
  if (!release) return true;
  if (release === "missing")
    return (
      STATUSES.indexOf(coop.status) >= STATUSES.indexOf("CONFIRMED") &&
      !coop.release_at
    );
  if (!coop.release_at) return false;
  const current = day(today.toISOString());
  const horizon = new Date(today);
  horizon.setDate(horizon.getDate() + 14);
  const value = day(coop.release_at);
  if (release === "overdue") return value < current;
  if (release === "14d")
    return value >= current && value <= day(horizon.toISOString());
  return value > day(horizon.toISOString());
}

export function filterAndSortCoops(coops: Coop[], query: QueryState): Coop[] {
  const needle = query.q.trim().toLocaleLowerCase("de");
  const filtered = coops.filter((coop) => {
    if (query.scope === "open" && !isOpen(coop)) return false;
    if (query.statuses.length && !query.statuses.includes(coop.status))
      return false;
    if (
      query.contact &&
      !coop.contacts.some((item) => item.id === query.contact)
    )
      return false;
    if (query.agency && coop.agency?.id !== query.agency) return false;
    if (!matchesRelease(coop, query.release)) return false;
    if (
      query.quality === "missing-release" &&
      !(STATUSES.indexOf(coop.status) >= 3 && !coop.release_at)
    )
      return false;
    if (query.quality === "missing-fee" && coop.fee_amount_minor !== null)
      return false;
    if (
      query.quality === "sync-issue" &&
      !["conflict", "error"].includes(coop.sync_state ?? "local")
    )
      return false;
    if (!needle) return true;
    return [
      coop.title,
      coop.brand?.name,
      coop.agency?.name,
      ...coop.contacts.flatMap((item) => [item.display_name, item.email]),
    ]
      .filter(Boolean)
      .some((value) => String(value).toLocaleLowerCase("de").includes(needle));
  });
  return filtered.sort((a, b) => {
    if (query.sort === "release")
      return (a.release_at ?? "9999").localeCompare(b.release_at ?? "9999");
    if (query.sort === "status")
      return STATUSES.indexOf(a.status) - STATUSES.indexOf(b.status);
    if (query.sort === "brand")
      return (a.brand?.name ?? "").localeCompare(b.brand?.name ?? "", "de");
    if (query.sort === "fee")
      return (b.fee_amount_minor ?? -1) - (a.fee_amount_minor ?? -1);
    return b.updated_at.localeCompare(a.updated_at);
  });
}

export function formatMoney(coop: Coop): string {
  if (coop.fee_amount_minor === null || !coop.fee_currency)
    return "Honorar offen";
  return new Intl.NumberFormat("de-DE", {
    style: "currency",
    currency: coop.fee_currency,
  }).format(coop.fee_amount_minor / 100);
}

export function formatDate(
  value: string | null,
  fallback = "Noch nicht vereinbart",
) {
  return value
    ? new Intl.DateTimeFormat("de-DE", {
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
      }).format(new Date(value))
    : fallback;
}
