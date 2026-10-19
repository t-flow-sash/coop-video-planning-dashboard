"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent, KeyboardEvent } from "react";
import {
  STATUSES,
  TERMINAL_STATUSES,
  allowedTransitionTargets,
  coopKey,
  entityKey,
  filterAndSortCoops,
  formatDate,
  formatMoney,
  isOpen,
  matchesRelease,
  relationshipIndex,
  statusLabel,
  urgencyLabel,
  type AuditEvent,
  type Coop,
  type CoopStatus,
  type QualityFilter,
  type QueryState,
  type ReleaseFilter,
  type SortKey,
} from "@/features/coops/domain";
import { type CoopRepository } from "@/lib/api/coops";

const SYNC_LABELS = {
  local: "Lokal",
  synced: "Synchron",
  pending: "Ausstehend",
  conflict: "Konflikt",
  error: "Fehler",
} as const;

const DEFAULT_QUERY: QueryState = {
  q: "",
  scope: "open",
  statuses: [],
  sort: "priority",
  release: null,
  quality: null,
  contact: null,
  agency: null,
};

function readQuery(): QueryState & { coop: string | null } {
  const params =
    typeof window === "undefined"
      ? new URLSearchParams()
      : new URLSearchParams(window.location.search);
  const statuses = (params.get("status")?.split(",") ?? []).filter(
    (value): value is CoopStatus => STATUSES.includes(value as CoopStatus),
  );
  const sort = params.get("sort") as SortKey | null;
  const releaseRaw = params.get("release");
  const release = (
    ["overdue", "14d", "later", "missing"].includes(releaseRaw ?? "")
      ? releaseRaw
      : null
  ) as ReleaseFilter;
  const qualityRaw = params.get("quality");
  const quality = (
    ["missing-release", "missing-fee", "sync-issue", "needs-review"].includes(
      qualityRaw ?? "",
    )
      ? qualityRaw
      : null
  ) as QualityFilter;
  return {
    q: params.get("q") ?? "",
    scope: params.get("scope") === "all" ? "all" : "open",
    statuses,
    sort: [
      "priority",
      "deadline",
      "updated",
      "status",
      "brand",
      "fee",
    ].includes(sort ?? "")
      ? sort!
      : "priority",
    release,
    quality,
    contact: params.get("contact"),
    agency: params.get("agency"),
    coop: params.get("coop"),
  };
}

function setUrl(
  query: QueryState,
  coop: string | null,
  mode: "push" | "replace" = "push",
) {
  const params = new URLSearchParams();
  if (query.q) params.set("q", query.q);
  if (query.statuses.length) params.set("status", query.statuses.join(","));
  if (query.release) params.set("release", query.release);
  if (query.contact) params.set("contact", query.contact);
  if (query.agency) params.set("agency", query.agency);
  if (query.quality) params.set("quality", query.quality);
  if (query.sort !== "priority") params.set("sort", query.sort);
  if (query.scope === "all") params.set("scope", "all");
  if (coop) params.set("coop", coop);
  const next = `${window.location.pathname}${params.size ? `?${params}` : ""}`;
  window.history[mode === "replace" ? "replaceState" : "pushState"](
    {},
    "",
    next,
  );
}

function primaryContact(coop: Coop) {
  return (
    coop.contacts.find((item) => item.role === "PRIMARY") ??
    coop.contacts[0] ??
    null
  );
}
function relative(value: string) {
  const days = Math.round(
    (new Date(value).getTime() - Date.now()) / 86_400_000,
  );
  if (Math.abs(days) < 1) return "heute";
  return new Intl.RelativeTimeFormat("de", { numeric: "auto" }).format(
    days,
    "day",
  );
}
function syncState(coop: Coop) {
  return coop.sync_state ?? "local";
}
function deliverableText(item: Coop["deliverables"][number]) {
  if (typeof item === "string") return item;
  return `${item.quantity && item.quantity > 1 ? `${item.quantity} × ` : ""}${item.name ?? item.type ?? "Deliverable"}`;
}

interface Props {
  repository: CoopRepository;
  initialCoops?: Coop[];
  demoMode?: boolean;
}
export function CoopWorkspace({
  repository,
  initialCoops,
  demoMode = false,
}: Props) {
  const [coops, setCoops] = useState<Coop[]>(initialCoops ?? []);
  const [query, setQuery] = useState<QueryState>(DEFAULT_QUERY);
  const [searchDraft, setSearchDraft] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<Coop | null>(null);
  const [audit, setAudit] = useState<AuditEvent[]>([]);
  const [loading, setLoading] = useState(!initialCoops);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [notice, setNotice] = useState<{
    tone: "success" | "error";
    text: string;
  } | null>(null);
  const [savingId, setSavingId] = useState<string | null>(null);
  const searchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const detailTrigger = useRef<HTMLElement | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setCoops(await repository.list());
    } catch {
      setError(
        "Kooperationen konnten nicht geladen werden. Bitte Verbindung zur lokalen API prüfen.",
      );
    } finally {
      setLoading(false);
    }
  }, [repository]);

  useEffect(() => {
    if (initialCoops) return;
    const timeout = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timeout);
  }, [initialCoops, load]);
  useEffect(() => {
    const timeout = window.setTimeout(() => {
      const state = readQuery();
      setQuery(state);
      setSearchDraft(state.q);
      setSelectedId(state.coop);
      setDetailLoading(Boolean(state.coop));
    }, 0);
    return () => window.clearTimeout(timeout);
  }, []);
  useEffect(() => {
    const onPopState = () => {
      const state = readQuery();
      setQuery(state);
      setSearchDraft(state.q);
      setDetail(null);
      setAudit([]);
      setDetailLoading(Boolean(state.coop));
      setSelectedId(state.coop);
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);
  useEffect(() => {
    if (!selectedId) return;
    let active = true;
    Promise.all([repository.detail(selectedId), repository.audit(selectedId)])
      .then(([nextDetail, nextAudit]) => {
        if (active) {
          setDetail(nextDetail);
          setAudit(nextAudit);
        }
      })
      .catch(() => {
        if (active) setDetailError("Details konnten nicht geladen werden.");
      })
      .finally(() => {
        if (active) setDetailLoading(false);
      });
    return () => {
      active = false;
    };
  }, [repository, selectedId]);

  const updateQuery = (patch: Partial<QueryState>) => {
    const next = { ...query, ...patch };
    setQuery(next);
    setUrl(next, selectedId);
  };
  const onSearch = (event: ChangeEvent<HTMLInputElement>) => {
    const value = event.target.value;
    setSearchDraft(value);
    if (searchTimer.current) clearTimeout(searchTimer.current);
    searchTimer.current = setTimeout(() => updateQuery({ q: value }), 250);
  };
  const openDetail = (id: string) => {
    detailTrigger.current = document.activeElement as HTMLElement | null;
    setDetail(null);
    setAudit([]);
    setDetailError(null);
    setDetailLoading(true);
    setSelectedId(id);
    setUrl(query, id);
  };
  const closeDetail = () => {
    setSelectedId(null);
    setDetail(null);
    setAudit([]);
    setDetailLoading(false);
    setUrl(query, null);
    window.setTimeout(() => detailTrigger.current?.focus(), 0);
  };
  useEffect(() => {
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape" && selectedId) closeDetail();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  });
  useEffect(() => {
    if (!selectedId) return;
    const shellNavigation = Array.from(
      document.querySelectorAll<HTMLElement>(".sidebar, .mobile-topbar"),
    );
    shellNavigation.forEach((element) => element.setAttribute("inert", ""));
    return () => {
      shellNavigation.forEach((element) => element.removeAttribute("inert"));
    };
  }, [selectedId]);
  const reset = () => {
    const next: QueryState = {
      q: "",
      scope: "open",
      statuses: [],
      sort: "priority",
      release: null,
      quality: null,
      contact: null,
      agency: null,
    };
    setSearchDraft("");
    setQuery(next);
    setUrl(next, selectedId);
  };

  const transition = async (coop: Coop, target: CoopStatus) => {
    if (target === coop.status) return;
    if (target === "SCHEDULED" && !coop.release_at) {
      setNotice({
        tone: "error",
        text: "Für „Eingeplant“ ist ein Release-Datum erforderlich.",
      });
      return;
    }
    if (
      (target === "COMPLETED" || target === "CANCELLED") &&
      !window.confirm(`Status wirklich auf „${statusLabel(target)}“ ändern?`)
    )
      return;
    const key = coopKey(coop);
    setSavingId(key);
    setNotice(null);
    try {
      const updated = await repository.transition(key, target, coop.version);
      setCoops((current) =>
        current.map((item) =>
          coopKey(item) === coopKey(updated) ? updated : item,
        ),
      );
      if (detail && coopKey(detail) === coopKey(updated)) setDetail(updated);
      setNotice({ tone: "success", text: "Coop gespeichert." });
      if (selectedId === coopKey(updated))
        setAudit(await repository.audit(coopKey(updated)));
    } catch (reason) {
      const stale =
        (reason as { status?: number; code?: string }).status === 409 &&
        (reason as { status?: number; code?: string }).code === "stale_version";
      setNotice({
        tone: "error",
        text: stale
          ? "Diese Coop wurde zwischenzeitlich geändert. Bitte die neuere Version laden und den Status erneut prüfen."
          : "Status konnte nicht gespeichert werden. Der bisherige Status bleibt erhalten.",
      });
    } finally {
      setSavingId(null);
    }
  };

  const result = useMemo(
    () => filterAndSortCoops(coops, query),
    [coops, query],
  );
  const relationships = useMemo(() => relationshipIndex(coops), [coops]);
  const selected =
    detail ?? coops.find((item) => coopKey(item) === selectedId) ?? null;
  const today = new Date();
  const observations = [
    {
      label: "Release überfällig",
      count: coops.filter(
        (c) => isOpen(c) && matchesRelease(c, "overdue", today),
      ).length,
      patch: { release: "overdue" as ReleaseFilter, quality: null },
    },
    {
      label: "Nächste 14 Tage",
      count: coops.filter((c) => isOpen(c) && matchesRelease(c, "14d", today))
        .length,
      patch: { release: "14d" as ReleaseFilter, quality: null },
    },
    {
      label: "Termin fehlt",
      count: coops.filter(
        (c) => isOpen(c) && matchesRelease(c, "missing", today),
      ).length,
      patch: { release: "missing" as ReleaseFilter, quality: null },
    },
    {
      label: "Sync prüfen",
      count: coops.filter(
        (c) => isOpen(c) && ["conflict", "error"].includes(syncState(c)),
      ).length,
      patch: { quality: "sync-issue" as QualityFilter, release: null },
    },
  ];
  const contactName = query.contact
    ? coops
        .flatMap((c) => c.contacts)
        .find((c) => entityKey(c) === query.contact)?.display_name
    : null;
  const agencyName = query.agency
    ? coops.find((c) => c.agency && entityKey(c.agency) === query.agency)
        ?.agency?.name
    : null;
  const activeCount =
    query.statuses.length +
    Number(Boolean(query.release)) +
    Number(Boolean(query.quality)) +
    Number(Boolean(query.contact)) +
    Number(Boolean(query.agency)) +
    Number(query.scope === "all");

  return (
    <div className={`workspace ${selectedId ? "has-detail" : ""}`}>
      <section
        className="list-pane"
        aria-label="Kooperationsarbeitsbereich"
        inert={selectedId ? true : undefined}
      >
        <header className="page-heading">
          <div>
            <h1>Kooperationen</h1>
            <p>Offene Kooperationen planen und bearbeiten.</p>
          </div>
          <button
            className="primary"
            type="button"
            onClick={() =>
              setNotice({
                tone: "error",
                text: "Das Anlegen folgt nach dem M1-Status-Slice.",
              })
            }
          >
            Coop anlegen
          </button>
        </header>

        <div className="environment-note" role="note">
          <span>
            <strong>
              {demoMode
                ? "Öffentliche Demo:"
                : "Stand 19.10.2026 · Delta pausiert:"}
            </strong>{" "}
            {demoMode
              ? "Ausschließlich synthetische Beispieldaten · keine API, Gmail- oder Agentenbearbeitung · Änderungen nur in diesem Browser."
              : "Fachlich geprüfter Stand vom 19.10.2026. Der automatische Delta-Import bleibt pausiert und unarmed."}
          </span>
          {demoMode && repository.reset && (
            <button
              type="button"
              onClick={() => {
                setCoops(repository.reset!());
                setSelectedId(null);
                setDetail(null);
                setAudit([]);
                setNotice({
                  tone: "success",
                  text: "Demo-Daten wurden zurückgesetzt.",
                });
              }}
            >
              Demo zurücksetzen
            </button>
          )}
        </div>

        <nav className="observations" aria-label="Beobachtungen">
          {observations.map((item) => (
            <button
              key={item.label}
              type="button"
              aria-pressed={Boolean(
                (item.patch.release && query.release === item.patch.release) ||
                  (item.patch.quality && query.quality === item.patch.quality),
              )}
              onClick={() => updateQuery(item.patch)}
            >
              {item.label} <span>· {item.count}</span>
            </button>
          ))}
        </nav>

        <div className="toolbar">
          <label className="search">
            <span className="sr-only">Kooperationen durchsuchen</span>
            <input
              value={searchDraft}
              onChange={onSearch}
              placeholder="Marke, Coop, Kontakt oder Agentur suchen"
            />
            {searchDraft && (
              <button
                type="button"
                aria-label="Suche leeren"
                onClick={() => {
                  setSearchDraft("");
                  updateQuery({ q: "" });
                }}
              >
                ×
              </button>
            )}
          </label>
          <details className="filter-menu">
            <summary>Filter{activeCount ? ` (${activeCount})` : ""}</summary>
            <div className="filter-panel">
              <fieldset>
                <legend>Status</legend>
                {STATUSES.map((status) => (
                  <label key={status}>
                    <input
                      type="checkbox"
                      checked={query.statuses.includes(status)}
                      onChange={() =>
                        updateQuery({
                          statuses: query.statuses.includes(status)
                            ? query.statuses.filter((value) => value !== status)
                            : [...query.statuses, status],
                        })
                      }
                    />
                    {statusLabel(status)}
                  </label>
                ))}
              </fieldset>
              <label>
                Datenqualität
                <select
                  value={query.quality ?? ""}
                  onChange={(e) =>
                    updateQuery({
                      quality: (e.target.value || null) as QualityFilter,
                    })
                  }
                >
                  <option value="">Alle</option>
                  <option value="missing-release">Release fehlt</option>
                  <option value="missing-fee">Honorar fehlt</option>
                  <option value="sync-issue">Sync prüfen</option>
                  <option value="needs-review">Unsicherheit prüfen</option>
                </select>
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={query.scope === "all"}
                  onChange={(e) =>
                    updateQuery({ scope: e.target.checked ? "all" : "open" })
                  }
                />{" "}
                Abgeschlossene einblenden
              </label>
            </div>
          </details>
          <label className="sort">
            Sortieren{" "}
            <select
              value={query.sort}
              onChange={(e) => updateQuery({ sort: e.target.value as SortKey })}
            >
              <option value="priority">Priorität P0 → P2</option>
              <option value="deadline">Deadline</option>
              <option value="updated">Zuletzt aktualisiert</option>
              <option value="status">Status im Workflow</option>
              <option value="brand">Marke A–Z</option>
              <option value="fee">Honorar (höchstes zuerst)</option>
            </select>
          </label>
        </div>

        {activeCount > 0 && (
          <div className="filter-chips" aria-label="Aktive Filter">
            {agencyName && (
              <FilterChip
                label={`Agentur: ${agencyName}`}
                onRemove={() => updateQuery({ agency: null })}
              />
            )}
            {contactName && (
              <FilterChip
                label={`Kontakt: ${contactName}`}
                onRemove={() => updateQuery({ contact: null })}
              />
            )}
            {query.release && (
              <FilterChip
                label={`Release: ${query.release}`}
                onRemove={() => updateQuery({ release: null })}
              />
            )}
            {query.quality && (
              <FilterChip
                label={`Qualität: ${query.quality}`}
                onRemove={() => updateQuery({ quality: null })}
              />
            )}
            {query.statuses.map((status) => (
              <FilterChip
                key={status}
                label={`Status: ${statusLabel(status)}`}
                onRemove={() =>
                  updateQuery({
                    statuses: query.statuses.filter((item) => item !== status),
                  })
                }
              />
            ))}
            {query.scope === "all" && (
              <FilterChip
                label="Abgeschlossene einblenden"
                onRemove={() => updateQuery({ scope: "open" })}
              />
            )}
            <button type="button" className="text-button" onClick={reset}>
              Alle zurücksetzen
            </button>
          </div>
        )}

        <div className="result-meta" aria-live="polite">
          {result.length} {query.scope === "open" ? "offene " : ""}Kooperationen
          {query.q ? ` für „${query.q}“` : ""}
        </div>
        <div className="results" aria-busy={loading}>
          {loading ? (
            <LoadingState />
          ) : error ? (
            <ErrorState message={error} retry={load} />
          ) : coops.length === 0 ? (
            <EmptyState />
          ) : result.length === 0 ? (
            <NoResults reset={reset} />
          ) : (
            <>
              <CoopTable
                coops={result}
                relationships={relationships}
                selectedId={selectedId}
                savingId={savingId}
                onOpen={openDetail}
                onTransition={transition}
                onEntity={(kind, id) =>
                  updateQuery(
                    kind === "contact" ? { contact: id } : { agency: id },
                  )
                }
              />
              <CoopCards
                coops={result}
                relationships={relationships}
                savingId={savingId}
                onOpen={openDetail}
                onEntity={(kind, id) =>
                  updateQuery(
                    kind === "contact" ? { contact: id } : { agency: id },
                  )
                }
              />
            </>
          )}
        </div>
      </section>
      {selectedId && (
        <DetailPanel
          coop={selected}
          loading={detailLoading}
          error={detailError}
          audit={audit}
          allCoops={coops}
          relationships={relationships}
          saving={savingId === selectedId}
          onClose={closeDetail}
          onRetry={() => {
            setSelectedId(null);
            setTimeout(() => setSelectedId(selectedId), 0);
          }}
          onOpen={openDetail}
          onTransition={transition}
          demoMode={demoMode}
        />
      )}
      {notice && (
        <div
          className={`toast ${notice.tone}`}
          role={notice.tone === "error" ? "alert" : "status"}
        >
          {notice.text}
          <button
            type="button"
            aria-label="Hinweis schließen"
            onClick={() => setNotice(null)}
          >
            ×
          </button>
        </div>
      )}
    </div>
  );
}

function FilterChip({
  label,
  onRemove,
}: {
  label: string;
  onRemove: () => void;
}) {
  return (
    <span className="filter-chip">
      <span className="filter-chip-label">{label}</span>
      <button
        type="button"
        aria-label={`${label} entfernen`}
        onClick={onRemove}
      >
        ×
      </button>
    </span>
  );
}
function LoadingState() {
  return (
    <div className="loading-state" role="status">
      <span className="sr-only">Kooperationen werden geladen.</span>
      {Array.from({ length: 6 }, (_, i) => (
        <div className="skeleton" key={i} />
      ))}
    </div>
  );
}
function ErrorState({
  message,
  retry,
}: {
  message: string;
  retry: () => void;
}) {
  return (
    <div className="state-block" role="alert">
      <h2>Kooperationen konnten nicht geladen werden.</h2>
      <p>{message}</p>
      <button type="button" onClick={retry}>
        Erneut versuchen
      </button>
    </div>
  );
}
function EmptyState() {
  return (
    <div className="state-block">
      <h2>Noch keine Kooperationen</h2>
      <p>
        Lege die erste Kooperation manuell an. Der Gmail-Import folgt in einem
        späteren Meilenstein.
      </p>
      <button className="primary" type="button">
        Coop anlegen
      </button>
    </div>
  );
}
function NoResults({ reset }: { reset: () => void }) {
  return (
    <div className="state-block">
      <h2>Keine Kooperationen für diese Auswahl</h2>
      <p>Suche und aktive Filter ergeben gemeinsam keine Treffer.</p>
      <button type="button" onClick={reset}>
        Filter zurücksetzen
      </button>
    </div>
  );
}

type RelationIndex = ReturnType<typeof relationshipIndex>;
interface ListProps {
  coops: Coop[];
  relationships: RelationIndex;
  savingId: string | null;
  onOpen: (id: string) => void;
  onEntity: (kind: "contact" | "agency", id: string) => void;
}
function RelationCells({
  coop,
  relationships,
  onEntity,
}: {
  coop: Coop;
  relationships: RelationIndex;
  onEntity: ListProps["onEntity"];
}) {
  const contact = primaryContact(coop);
  const contactCount = contact
    ? (relationships.contacts.get(entityKey(contact))?.length ?? 0)
    : 0;
  const agencyCount = coop.agency
    ? (relationships.agencies.get(entityKey(coop.agency))?.length ?? 0)
    : 0;
  return (
    <div className="entities">
      {contact && (
        <button
          type="button"
          className="entity-chip"
          aria-label={`Kontakt: ${contact.display_name}${contactCount > 1 ? `, ${contactCount} offene Coops` : ""}`}
          onClick={(e) => {
            e.stopPropagation();
            onEntity("contact", entityKey(contact));
          }}
        >
          {contact.display_name}
          {contactCount > 1 && <small>{contactCount} offene Coops</small>}
        </button>
      )}
      {coop.agency && (
        <button
          type="button"
          className="entity-chip"
          aria-label={`Agentur: ${coop.agency.name}${agencyCount > 1 ? `, ${agencyCount} offene Coops` : ""}`}
          onClick={(e) => {
            e.stopPropagation();
            onEntity("agency", entityKey(coop.agency!));
          }}
        >
          {coop.agency.name}
          {agencyCount > 1 && <small>{agencyCount} offene Coops</small>}
        </button>
      )}
    </div>
  );
}
function RelationshipNote({
  coop,
  relationships,
}: {
  coop: Coop;
  relationships: RelationIndex;
}) {
  const contact = primaryContact(coop);
  const contactCount = contact
    ? (relationships.contacts.get(entityKey(contact))?.length ?? 0)
    : 0;
  const agencyCount = coop.agency
    ? (relationships.agencies.get(entityKey(coop.agency))?.length ?? 0)
    : 0;
  if (contactCount < 2 && agencyCount < 2) return null;
  return (
    <span className="relationship-notes">
      {contactCount > 1 && (
        <small className="relationship-note">
          ↔ Kontakt auch in {contactCount - 1} weiteren offenen Coops
        </small>
      )}
      {agencyCount > 1 && (
        <small className="relationship-note">
          ↔ Agentur auch in {agencyCount - 1} weiteren offenen Coops
        </small>
      )}
    </span>
  );
}
function StatusSelect({
  coop,
  saving,
  onTransition,
}: {
  coop: Coop;
  saving: boolean;
  onTransition: (coop: Coop, target: CoopStatus) => void;
}) {
  return (
    <label className="status-select">
      <span className="sr-only">Status für {coop.title} ändern</span>
      <select
        aria-label={`Status für ${coop.title} ändern`}
        value={coop.status}
        disabled={saving || TERMINAL_STATUSES.has(coop.status)}
        onChange={(e) => void onTransition(coop, e.target.value as CoopStatus)}
      >
        {[coop.status, ...allowedTransitionTargets(coop.status)].map(
          (status) => (
            <option key={status} value={status}>
              {statusLabel(status)}
            </option>
          ),
        )}
      </select>
      {saving && <small>Speichert …</small>}
    </label>
  );
}
function Sync({ coop }: { coop: Coop }) {
  const state = syncState(coop);
  return (
    <span
      className={`sync sync-${state}`}
      aria-label={`Synchronisationsstatus: ${SYNC_LABELS[state]}`}
      title={`Synchronisationsstatus: ${SYNC_LABELS[state]}`}
    >
      {SYNC_LABELS[state]}
    </span>
  );
}

function CoopTable({
  coops,
  relationships,
  selectedId,
  savingId,
  onOpen,
  onTransition,
  onEntity,
}: ListProps & {
  selectedId: string | null;
  onTransition: (coop: Coop, target: CoopStatus) => void;
}) {
  const rowKey = (event: KeyboardEvent<HTMLTableRowElement>, id: string) => {
    if (event.key === "Enter" && event.target === event.currentTarget)
      onOpen(id);
  };
  return (
    <div className="desktop-table">
      <table>
        <caption className="sr-only">Offene Kooperationen</caption>
        <thead>
          <tr>
            <th scope="col">Prio</th>
            <th scope="col">Coop / nächste Aktion</th>
            <th scope="col">Status</th>
            <th scope="col">Deadline</th>
            <th scope="col">Honorar</th>
            <th scope="col">Kontakt / Agentur</th>
            <th scope="col">Owner</th>
            <th scope="col">Review</th>
          </tr>
        </thead>
        <tbody>
          {coops.map((coop) => {
            const key = coopKey(coop);
            return (
              <tr
                key={key}
                tabIndex={0}
                aria-selected={key === selectedId}
                onKeyDown={(e) => rowKey(e, key)}
              >
                <td>
                  <strong
                    className={`priority priority-${coop.priority ?? "P2"}`}
                  >
                    {coop.priority ?? "P2"}
                  </strong>
                </td>
                <td>
                  <button
                    className="title-link"
                    type="button"
                    onClick={() => onOpen(key)}
                  >
                    {coop.title}
                  </button>
                  <span>
                    {coop.next_action ??
                      coop.brand?.name ??
                      "Nächste Aktion offen"}
                  </span>
                  <RelationshipNote coop={coop} relationships={relationships} />
                </td>
                <td>
                  <StatusSelect
                    coop={coop}
                    saving={savingId === key}
                    onTransition={onTransition}
                  />
                </td>
                <td>
                  <strong
                    className={`urgency urgency-${coop.urgency ?? "NONE"}`}
                  >
                    {urgencyLabel(coop)}
                  </strong>
                  <span>{formatDate(coop.deadline)}</span>
                </td>
                <td>
                  {formatMoney(coop)}
                  <span>{coop.fee_status ?? "UNKNOWN"}</span>
                </td>
                <td>
                  <RelationCells
                    coop={coop}
                    relationships={relationships}
                    onEntity={onEntity}
                  />
                </td>
                <td>{coop.owner ?? "Nicht zugewiesen"}</td>
                <td>
                  {coop.needs_review ? (
                    <span className="review-warning">
                      Prüfung erforderlich
                      {coop.uncertainty && <small>{coop.uncertainty}</small>}
                    </span>
                  ) : (
                    <span>Kein Prüfhinweis</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
function CoopCards({
  coops,
  relationships,
  savingId,
  onOpen,
  onEntity,
}: ListProps) {
  return (
    <ul className="mobile-cards">
      {coops.map((coop) => (
        <li key={coop.id}>
          <article onClick={() => onOpen(coop.id)}>
            <div className="card-top">
              <span className="status-badge">{statusLabel(coop.status)}</span>
              <strong className={`priority priority-${coop.priority ?? "P2"}`}>
                {coop.priority ?? "P2"}
              </strong>
              <Sync coop={coop} />
            </div>
            <button
              type="button"
              className="card-title"
              onClick={() => onOpen(coop.id)}
            >
              {coop.title}
            </button>
            <p>{coop.brand?.name ?? "Marke nicht erfasst"}</p>
            <dl>
              <div>
                <dt>Deadline</dt>
                <dd>
                  <strong
                    className={`urgency urgency-${coop.urgency ?? "NONE"}`}
                  >
                    {urgencyLabel(coop)}
                  </strong>{" "}
                  {formatDate(coop.deadline)}
                </dd>
              </div>
              <div>
                <dt>Nächste Aktion</dt>
                <dd>{coop.next_action ?? "Noch nicht erfasst"}</dd>
              </div>
              <div>
                <dt>Owner</dt>
                <dd>{coop.owner ?? "Nicht zugewiesen"}</dd>
              </div>
              <div>
                <dt>Letzter Kontakt</dt>
                <dd>{formatDate(coop.last_contact_date)}</dd>
              </div>
              <div>
                <dt>Review</dt>
                <dd>
                  {coop.needs_review
                    ? `Prüfung erforderlich${coop.uncertainty ? `: ${coop.uncertainty}` : ""}`
                    : "Kein Prüfhinweis"}
                </dd>
              </div>
              <div>
                <dt>Release</dt>
                <dd>{formatDate(coop.release_at)}</dd>
              </div>
              <div>
                <dt>Honorar</dt>
                <dd>{formatMoney(coop)}</dd>
              </div>
            </dl>
            <RelationCells
              coop={coop}
              relationships={relationships}
              onEntity={onEntity}
            />
            <RelationshipNote coop={coop} relationships={relationships} />
            <footer>
              Aktualisiert {relative(coop.updated_at)}
              <button
                type="button"
                aria-label={`Aktionen für ${coop.title}`}
                onClick={(e) => e.stopPropagation()}
              >
                •••
              </button>
            </footer>
            {savingId === coop.id && <small>Speichert …</small>}
          </article>
        </li>
      ))}
    </ul>
  );
}

function DetailPanel({
  coop,
  loading,
  error,
  audit,
  allCoops,
  relationships,
  saving,
  onClose,
  onRetry,
  onOpen,
  onTransition,
  demoMode,
}: {
  coop: Coop | null;
  loading: boolean;
  error: string | null;
  audit: AuditEvent[];
  allCoops: Coop[];
  relationships: RelationIndex;
  saving: boolean;
  onClose: () => void;
  onRetry: () => void;
  onOpen: (id: string) => void;
  onTransition: (coop: Coop, target: CoopStatus) => void;
  demoMode: boolean;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  const trapFocus = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== "Tab") return;
    const focusable = Array.from(
      event.currentTarget.querySelectorAll<HTMLElement>(
        'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex="0"]',
      ),
    );
    if (focusable.length === 0) {
      event.preventDefault();
      event.currentTarget.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement;
    if (!focusable.includes(active as HTMLElement)) {
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
    } else if (event.shiftKey && active === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  };
  useEffect(() => {
    heading.current?.focus();
  }, [coop?.id]);
  if (loading && !coop)
    return (
      <aside
        className="detail-panel"
        role="dialog"
        aria-modal="true"
        aria-label="Coop-Details"
        aria-busy="true"
        tabIndex={-1}
        onKeyDown={trapFocus}
      >
        <p>Details werden geladen …</p>
      </aside>
    );
  if (!coop)
    return (
      <aside
        className="detail-panel"
        role="dialog"
        aria-modal="true"
        aria-label="Coop-Details"
        tabIndex={-1}
        onKeyDown={trapFocus}
      >
        <div className="state-block">
          <h2>{error ?? "Details konnten nicht geladen werden."}</h2>
          <button onClick={onRetry}>Erneut versuchen</button>
          <button onClick={onClose}>Schließen</button>
        </div>
      </aside>
    );
  const contact = primaryContact(coop);
  const related = allCoops.filter(
    (item) =>
      item.id !== coop.id &&
      isOpen(item) &&
      ((contact && item.contacts.some((c) => c.id === contact.id)) ||
        (coop.agency && item.agency?.id === coop.agency.id)),
  );
  return (
    <aside
      className="detail-panel"
      role="dialog"
      aria-modal="true"
      aria-labelledby="coop-detail-title"
      tabIndex={-1}
      onKeyDown={trapFocus}
    >
      {demoMode && (
        <p className="detail-demo-note" role="note">
          <strong>Öffentliche Demo:</strong> Synthetische Daten · keine API,
          Gmail- oder Agentenbearbeitung · Änderungen nur in diesem Browser.
        </p>
      )}
      <div className="detail-top">
        <div>
          <small>{coop.brand?.name ?? "Marke nicht erfasst"}</small>
          <h2 id="coop-detail-title" ref={heading} tabIndex={-1}>
            {coop.title}
          </h2>
        </div>
        <button type="button" aria-label="Details schließen" onClick={onClose}>
          ×
        </button>
      </div>
      {error && (
        <div role="alert" className="inline-error">
          {error} <button onClick={onRetry}>Erneut versuchen</button>
        </div>
      )}
      <p className="detail-meta">
        Aktualisiert {new Date(coop.updated_at).toLocaleString("de-DE")} ·
        Version {coop.version}
      </p>
      <section>
        <h3>Priorität &amp; nächste Aktion</h3>
        <dl>
          <div>
            <dt>Priorität</dt>
            <dd>{coop.priority ?? "P2"}</dd>
          </div>
          <div>
            <dt>Deadline</dt>
            <dd>{formatDate(coop.deadline)}</dd>
          </div>
          <div>
            <dt>Nächste Aktion</dt>
            <dd>{coop.next_action ?? "Noch nicht erfasst"}</dd>
          </div>
          <div>
            <dt>Owner</dt>
            <dd>{coop.owner ?? "Nicht zugewiesen"}</dd>
          </div>
          <div>
            <dt>Letzter Kontakt</dt>
            <dd>{formatDate(coop.last_contact_date)}</dd>
          </div>
          {coop.needs_review && (
            <div>
              <dt>Unsicherheit</dt>
              <dd>{coop.uncertainty ?? "Prüfung erforderlich"}</dd>
            </div>
          )}
        </dl>
      </section>
      <section>
        <h3>Planung</h3>
        <dl>
          <div>
            <dt>Release</dt>
            <dd>{formatDate(coop.release_at)}</dd>
          </div>
          <div>
            <dt>Status</dt>
            <dd>
              <StatusSelect
                coop={coop}
                saving={saving}
                onTransition={onTransition}
              />
            </dd>
          </div>
          <div>
            <dt>Deliverables</dt>
            <dd>
              {coop.deliverables.length
                ? coop.deliverables.map(deliverableText).join(", ")
                : "Noch nicht erfasst"}
            </dd>
          </div>
        </dl>
      </section>
      <section>
        <h3>Konditionen</h3>
        <p>{formatMoney(coop)}</p>
      </section>
      <section>
        <h3>Kontakt &amp; Agentur</h3>
        <p>
          {contact ? (
            <>
              {contact.display_name}
              {contact.email && (
                <>
                  {" "}
                  · <a href={`mailto:${contact.email}`}>{contact.email}</a>
                </>
              )}
            </>
          ) : (
            "Noch nicht erfasst"
          )}
        </p>
        <p>{coop.agency?.name ?? "Agentur nicht erfasst"}</p>
        <RelationshipNote coop={coop} relationships={relationships} />
        {related.length > 0 && (
          <>
            <h4>Weitere offene Coops mit diesem Kontakt oder dieser Agentur</h4>
            <ul className="related-list">
              {related.map((item) => (
                <li key={item.id}>
                  <button onClick={() => onOpen(item.id)}>
                    {item.title} — {statusLabel(item.status)}
                  </button>
                </li>
              ))}
            </ul>
          </>
        )}
      </section>
      <section>
        <h3>Notizen / Briefing</h3>
        <p>{coop.summary ?? "Noch nicht erfasst"}</p>
      </section>
      <section id="source">
        <h3>Herkunft &amp; Sync</h3>
        {["conflict", "error"].includes(syncState(coop)) && (
          <p className="sync-warning">
            Der lokale Stand benötigt Prüfung. Live-Synchronisierung ist in M1
            nicht aktiv.
          </p>
        )}
        <dl>
          <div>
            <dt>Quelle</dt>
            <dd>
              {coop.source === "gmail_preview"
                ? "Gmail-Vorschau"
                : "Manuell erfasst"}
            </dd>
          </div>
          <div>
            <dt>Syncstatus</dt>
            <dd>{SYNC_LABELS[syncState(coop)]}</dd>
          </div>
          <div>
            <dt>Quellenreferenz</dt>
            <dd>{coop.source_reference ?? "Nicht vorhanden"}</dd>
          </div>
        </dl>
        <p className="preview-copy">
          Gmail-Herkunft ist in Meilenstein 1 nur eine Vorschau; es findet keine
          Live-Synchronisierung statt.
        </p>
      </section>
      <section>
        <h3>Änderungshistorie</h3>
        {audit.length ? (
          <ol className="audit-list">
            {audit.map((event) => (
              <li key={event.id}>
                <strong>
                  {event.action === "STATUS_TRANSITIONED"
                    ? "Status geändert"
                    : event.action}
                </strong>
                <span>
                  {new Date(event.created_at).toLocaleString("de-DE")} ·{" "}
                  {event.actor_type} · {event.source}
                </span>
                {Object.entries(event.changes ?? {}).map(([field, values]) => (
                  <small key={field}>
                    {field}: {String(values.old ?? "—")} →{" "}
                    {String(values.new ?? "—")}
                  </small>
                ))}
              </li>
            ))}
          </ol>
        ) : (
          <p>Noch keine Audit-Ereignisse vorhanden.</p>
        )}
      </section>
      <section className="metadata">
        <h3>Metadaten</h3>
        <p>
          ID <code>{coop.id}</code>
          <br />
          Version {coop.version}
        </p>
      </section>
    </aside>
  );
}
