import type {
  AuditEvent,
  Contact,
  Coop,
  CoopStatus,
  Organization,
} from "@/features/coops/domain";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "/api/v1";
const MAX_LIST_PAGES = 100;

function apiUrl(): string {
  return API_URL.replace(/\/$/, "");
}

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public code?: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${apiUrl()}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      ...init?.headers,
    },
  });
  if (!response.ok) {
    let problem: {
      title?: string;
      detail?: string;
      code?: string;
      error_code?: string;
    } = {};
    try {
      problem = await response.json();
    } catch {
      /* non-JSON server failure */
    }
    throw new ApiError(
      problem.detail ?? problem.title ?? `API-Fehler (${response.status})`,
      response.status,
      problem.code ?? problem.error_code,
    );
  }
  return response.json() as Promise<T>;
}

type Page<T> =
  | { items: T[]; next_cursor?: string | null; total?: number }
  | T[];
const items = <T>(value: Page<T>): T[] =>
  Array.isArray(value) ? value : value.items;

type ApiOrganization = Organization & { version?: number };
type ApiContactLink = { role: string; contact: Contact };
type ApiCoop = Omit<Coop, "brand" | "agency" | "contacts" | "deliverables"> & {
  brand?: Organization | null;
  agency?: Organization | null;
  contacts?: Contact[] | ApiContactLink[];
  deliverables: Coop["deliverables"] | null;
  brand_organization?: ApiOrganization | null;
  agency_organization?: ApiOrganization | null;
};

function normalize(raw: ApiCoop): Coop {
  const links = raw.contacts ?? [];
  return {
    ...raw,
    brand: raw.brand ?? raw.brand_organization ?? null,
    agency: raw.agency ?? raw.agency_organization ?? null,
    contacts: links.map((entry) =>
      "contact" in entry ? { ...entry.contact, role: entry.role } : entry,
    ),
    deliverables: raw.deliverables ?? [],
  };
}

export interface SemanticSourceStatus {
  enabled: boolean;
  discovered: number;
  processed: number;
  created: number;
  updated: number;
  unchanged: number;
  review: number;
  retry: number;
  backlog: number;
  oldest_backlog_at: string | null;
  latest_evidence_at: string | null;
  last_semantic_activity: string | null;
  collector_age_seconds: number | null;
  last_collector_run: {
    mode: string;
    status: string;
    started_at: string;
    finished_at: string | null;
  } | null;
}

export interface SemanticStatus {
  as_of: string;
  coverage_verified: boolean;
  coverage_note: string;
  domain_records: number;
  sources: SemanticSourceStatus[];
}

export interface CoopRepository {
  semanticStatus?(): Promise<SemanticStatus>;
  list(): Promise<Coop[]>;
  detail(id: string): Promise<Coop>;
  audit(id: string): Promise<AuditEvent[]>;
  transition(
    id: string,
    status: CoopStatus,
    expectedVersion: number,
  ): Promise<Coop>;
  reset?(): Coop[];
}

export const apiRepository: CoopRepository = {
  semanticStatus() {
    return request<SemanticStatus>("/semantic/status");
  },
  async list() {
    const coops: Coop[] = [];
    const seenCursors = new Set<string>();
    let pageCount = 0;
    let cursor: string | null = null;
    do {
      const path: string = `/coops?scope=all&limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`;
      const page: Page<ApiCoop> = await request<Page<ApiCoop>>(path);
      pageCount += 1;
      coops.push(...items(page).map(normalize));
      cursor = Array.isArray(page) ? null : (page.next_cursor ?? null);
      if (cursor && seenCursors.has(cursor)) {
        throw new Error(
          "Cursor-Schleife beim Laden der Kooperationen erkannt.",
        );
      }
      if (cursor && pageCount >= MAX_LIST_PAGES) {
        throw new Error(
          `Harte Obergrenze von ${MAX_LIST_PAGES} Seiten beim Laden der Kooperationen erreicht.`,
        );
      }
      if (cursor) seenCursors.add(cursor);
    } while (cursor);
    return coops;
  },
  async detail(id) {
    return normalize(
      await request<ApiCoop>(`/coops/${encodeURIComponent(id)}`),
    );
  },
  async audit(id) {
    const events: AuditEvent[] = [];
    const seenCursors = new Set<string>();
    let pageCount = 0;
    let cursor: string | null = null;
    do {
      const path: string = `/coops/${encodeURIComponent(id)}/audit-events?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`;
      const page: Page<AuditEvent> = await request<Page<AuditEvent>>(path);
      pageCount += 1;
      events.push(...items(page));
      cursor = Array.isArray(page) ? null : (page.next_cursor ?? null);
      if (cursor && seenCursors.has(cursor)) {
        throw new Error("Cursor-Schleife beim Laden des Audits erkannt.");
      }
      if (cursor && pageCount >= MAX_LIST_PAGES) {
        throw new Error(
          `Harte Obergrenze von ${MAX_LIST_PAGES} Seiten beim Laden des Audits erreicht.`,
        );
      }
      if (cursor) seenCursors.add(cursor);
    } while (cursor);
    return events;
  },
  async transition(id, status, expectedVersion) {
    return normalize(
      await request<ApiCoop>(`/coops/${encodeURIComponent(id)}/transitions`, {
        method: "POST",
        body: JSON.stringify({
          target_status: status,
          expected_version: expectedVersion,
        }),
      }),
    );
  },
};
