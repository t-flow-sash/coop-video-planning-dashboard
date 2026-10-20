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
export type SortKey =
  | "priority"
  | "updated"
  | "deadline"
  | "status"
  | "brand"
  | "fee";
export type ReleaseFilter = "overdue" | "14d" | "later" | "missing" | null;
export type QualityFilter =
  | "missing-release"
  | "missing-fee"
  | "sync-issue"
  | "needs-review"
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
  record_key?: string;
  id: string;
  name: string;
}
export interface Contact {
  record_key?: string;
  id: string;
  display_name: string;
  email: string | null;
  role?: string;
}
export interface Coop {
  record_key?: string;
  id: string;
  title: string;
  status: CoopStatus;
  priority?: "P0" | "P1" | "P2";
  brand: Organization | null;
  agency: Organization | null;
  contacts: Contact[];
  summary: string | null;
  deliverables: Array<
    string | { type?: string; name?: string; kind?: string; quantity?: number }
  >;
  last_contact_date?: string | null;
  next_action: string | null;
  owner?: string | null;
  deadline?: string | null;
  urgency?: "OVERDUE" | "ACUTE" | "UPCOMING" | "NONE";
  release_at: string | null;
  fee_amount_minor: number | null;
  fee_currency: string | null;
  fee_status?:
    | "UNKNOWN"
    | "PROPOSED"
    | "AGREED"
    | "INVOICED"
    | "PAID"
    | "NOT_APPLICABLE";
  needs_review?: boolean;
  uncertainty?: string | null;
  version: number;
  created_at: string;
  updated_at: string;
  source?: string;
  source_reference?: string | null;
  sync_state?: "local" | "synced" | "pending" | "conflict" | "error";
  updated_by?: string;
}
export interface AuditEvent {
  id?: string;
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
export const coopKey = (coop: Coop) => coop.id;
export const entityKey = (entity: Organization | Contact) => entity.id;

export function relationshipIndex(coops: Coop[]) {
  const contacts = new Map<string, Coop[]>();
  const agencies = new Map<string, Coop[]>();
  for (const coop of coops.filter(isOpen)) {
    for (const contact of coop.contacts) {
      const key = entityKey(contact);
      contacts.set(key, [...(contacts.get(key) ?? []), coop]);
    }
    if (coop.agency) {
      const key = entityKey(coop.agency);
      agencies.set(key, [...(agencies.get(key) ?? []), coop]);
    }
  }
  return { contacts, agencies };
}
const day = (value: string) => value.slice(0, 10);
export function matchesRelease(
  coop: Coop,
  release: ReleaseFilter | undefined,
  today = new Date(),
) {
  if (!release) return true;
  if (release === "missing")
    return STATUSES.indexOf(coop.status) >= 3 && !coop.release_at;
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
  return coops
    .filter((coop) => {
      if (query.scope === "open" && !isOpen(coop)) return false;
      if (query.statuses.length && !query.statuses.includes(coop.status))
        return false;
      if (
        query.contact &&
        !coop.contacts.some((item) => entityKey(item) === query.contact)
      )
        return false;
      if (
        query.agency &&
        (!coop.agency || entityKey(coop.agency) !== query.agency)
      )
        return false;
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
      if (query.quality === "needs-review" && !coop.needs_review) return false;
      if (!needle) return true;
      return [
        coop.title,
        coop.brand?.name,
        coop.agency?.name,
        coop.next_action,
        coop.owner,
        ...coop.contacts.flatMap((item) => [item.display_name, item.email]),
      ]
        .filter(Boolean)
        .some((value) =>
          String(value).toLocaleLowerCase("de").includes(needle),
        );
    })
    .sort((a, b) => {
      const priority = (a.priority ?? "P2").localeCompare(b.priority ?? "P2");
      if (query.sort === "priority")
        return (
          priority || (a.deadline ?? "9999").localeCompare(b.deadline ?? "9999")
        );
      if (query.sort === "deadline")
        return (
          (a.deadline ?? "9999").localeCompare(b.deadline ?? "9999") || priority
        );
      if (query.sort === "status")
        return (
          STATUSES.indexOf(a.status) - STATUSES.indexOf(b.status) || priority
        );
      if (query.sort === "brand")
        return (
          (a.brand?.name ?? "").localeCompare(b.brand?.name ?? "", "de") ||
          priority
        );
      if (query.sort === "fee")
        return (
          (b.fee_amount_minor ?? -1) - (a.fee_amount_minor ?? -1) || priority
        );
      return priority || b.updated_at.localeCompare(a.updated_at);
    });
}
export function formatMoney(coop: Coop) {
  if (coop.fee_amount_minor === null || !coop.fee_currency)
    return "Honorar offen";
  return new Intl.NumberFormat("de-DE", {
    style: "currency",
    currency: coop.fee_currency,
  }).format(coop.fee_amount_minor / 100);
}
export function formatDate(
  value: string | null | undefined,
  fallback = "Noch nicht vereinbart",
) {
  return value
    ? new Intl.DateTimeFormat("de-DE", {
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
        timeZone: "UTC",
      }).format(new Date(`${value.slice(0, 10)}T12:00:00Z`))
    : fallback;
}
export const urgencyLabel = (coop: Coop) =>
  ({
    OVERDUE: "Überfällig",
    ACUTE: "Akut",
    UPCOMING: "Demnächst",
    NONE: "Ohne Frist",
  })[coop.urgency ?? "NONE"];
