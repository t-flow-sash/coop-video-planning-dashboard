import type {
  AuditEvent,
  Contact,
  Coop,
  CoopStatus,
  Organization,
} from "@/features/coops/domain";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "/api/v1";

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

export interface CoopRepository {
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
  async list() {
    const summaries = items(
      await request<Page<ApiCoop>>("/coops?scope=all&limit=100"),
    );
    // M1 list responses intentionally stay compact; enrich them through the typed detail endpoint.
    return Promise.all(
      summaries.map(async (summary) => {
        if (
          summary.brand ||
          summary.brand_organization ||
          (summary.contacts?.length ?? 0) > 0
        )
          return normalize(summary);
        try {
          return normalize(
            await request<ApiCoop>(`/coops/${encodeURIComponent(summary.id)}`),
          );
        } catch {
          return normalize(summary);
        }
      }),
    );
  },
  async detail(id) {
    return normalize(
      await request<ApiCoop>(`/coops/${encodeURIComponent(id)}`),
    );
  },
  async audit(id) {
    return items(
      await request<Page<AuditEvent>>(
        `/coops/${encodeURIComponent(id)}/audit-events?limit=100`,
      ),
    );
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
