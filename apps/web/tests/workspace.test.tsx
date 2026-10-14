import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "vitest-axe";
import { describe, expect, it, vi } from "vitest";
import { CoopWorkspace } from "@/features/coops/coop-workspace";
import type { Coop } from "@/features/coops/domain";

const coops: Coop[] = [
  {
    id: "c1",
    title: "Launch Reel",
    status: "NEGOTIATING",
    brand: { id: "b1", name: "Nordlicht" },
    agency: { id: "a1", name: "Studio Nord" },
    contacts: [
      {
        id: "p1",
        display_name: "Lea Berg",
        email: "lea@example.invalid",
        role: "PRIMARY",
      },
    ],
    summary: "30 Sekunden",
    deliverables: ["Reel"],
    next_action: null,
    release_at: null,
    fee_amount_minor: null,
    fee_currency: null,
    version: 4,
    created_at: "2026-10-01T10:00:00Z",
    updated_at: "2026-10-10T10:00:00Z",
  },
  {
    id: "c2",
    title: "Winter Short",
    status: "CONFIRMED",
    brand: { id: "b2", name: "Fjord" },
    agency: { id: "a1", name: "Studio Nord" },
    contacts: [
      {
        id: "p1",
        display_name: "Lea Berg",
        email: "lea@example.invalid",
        role: "PRIMARY",
      },
    ],
    summary: null,
    deliverables: [],
    next_action: null,
    release_at: "2026-10-20T12:00:00Z",
    fee_amount_minor: 125000,
    fee_currency: "EUR",
    version: 2,
    created_at: "2026-10-02T10:00:00Z",
    updated_at: "2026-10-11T10:00:00Z",
  },
];

const repository = {
  list: vi.fn(async () => coops),
  detail: vi.fn(async (id: string) => coops.find((coop) => coop.id === id)!),
  audit: vi.fn(async () => []),
  transition: vi.fn(async (id: string, status: Coop["status"]) => ({
    ...coops.find((coop) => coop.id === id)!,
    status,
    version: 5,
  })),
};

describe("CoopWorkspace", () => {
  it("rendert eine zugängliche Arbeitsliste mit Beziehungssignalen", async () => {
    const { container } = render(
      <CoopWorkspace repository={repository} initialCoops={coops} />,
    );
    expect(
      await screen.findByRole("table", { name: "Offene Kooperationen" }),
    ).toBeInTheDocument();
    expect(screen.getAllByText("2 offene Coops").length).toBeGreaterThan(0);
    expect(
      (
        await axe(container, {
          rules: { "color-contrast": { enabled: false } },
        })
      ).violations,
    ).toHaveLength(0);
  });

  it("filtert bei Klick auf eine wiederkehrende Agentur und hält den Zustand in der URL", async () => {
    const user = userEvent.setup();
    render(<CoopWorkspace repository={repository} initialCoops={coops} />);
    await user.click(
      (
        await screen.findAllByRole("button", { name: /Agentur: Studio Nord/ })
      )[0],
    );
    expect(new URL(window.location.href).searchParams.get("agency")).toBe("a1");
    expect(
      screen.getByText("Agentur: Studio Nord", {
        selector: ".filter-chip-label",
      }),
    ).toBeInTheDocument();
  });

  it("unterscheidet Lade-, leeren und Fehlerzustand", async () => {
    const pending = new Promise<Coop[]>(() => undefined);
    const loadingRepository = { ...repository, list: vi.fn(() => pending) };
    const first = render(<CoopWorkspace repository={loadingRepository} />);
    expect(
      screen.getByText("Kooperationen werden geladen."),
    ).toBeInTheDocument();
    first.unmount();

    const emptyRepository = { ...repository, list: vi.fn(async () => []) };
    const second = render(<CoopWorkspace repository={emptyRepository} />);
    expect(
      await screen.findByRole("heading", { name: "Noch keine Kooperationen" }),
    ).toBeInTheDocument();
    second.unmount();

    const failedRepository = {
      ...repository,
      list: vi.fn(async () => {
        throw new Error("offline");
      }),
    };
    render(<CoopWorkspace repository={failedRepository} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Kooperationen konnten nicht geladen werden",
    );
  });

  it("verhindert Eingeplant ohne Release-Datum vor dem API-Aufruf", async () => {
    const user = userEvent.setup();
    repository.transition.mockClear();
    const productionCoops = coops.map((coop) =>
      coop.id === "c1" ? { ...coop, status: "IN_PRODUCTION" as const } : coop,
    );
    render(
      <CoopWorkspace repository={repository} initialCoops={productionCoops} />,
    );
    const table = await screen.findByRole("table", {
      name: "Offene Kooperationen",
    });
    const row = within(table).getByText("Launch Reel").closest("tr")!;
    await user.selectOptions(
      within(row).getByLabelText("Status für Launch Reel ändern"),
      "SCHEDULED",
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Release-Datum erforderlich",
    );
    expect(repository.transition).not.toHaveBeenCalled();
  });

  it("sendet Statusänderungen mit expected_version und zeigt Konflikte verständlich", async () => {
    const user = userEvent.setup();
    repository.transition.mockRejectedValueOnce(
      Object.assign(new Error("Conflict"), {
        status: 409,
        code: "stale_version",
      }),
    );
    render(<CoopWorkspace repository={repository} initialCoops={coops} />);
    const table = await screen.findByRole("table", {
      name: "Offene Kooperationen",
    });
    const row = within(table).getByText("Launch Reel").closest("tr")!;
    await user.selectOptions(
      within(row).getByLabelText("Status für Launch Reel ändern"),
      "QUALIFYING",
    );
    await waitFor(() =>
      expect(repository.transition).toHaveBeenCalledWith("c1", "QUALIFYING", 4),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "zwischenzeitlich geändert",
    );
  });
});
