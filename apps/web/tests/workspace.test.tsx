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

  it("öffnet Details als modalen Dialog, inertisiert den Hintergrund und setzt den Fokus", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <CoopWorkspace repository={repository} initialCoops={coops} />,
    );
    await user.click(
      within(
        screen.getByRole("table", { name: "Offene Kooperationen" }),
      ).getByRole("button", { name: "Launch Reel" }),
    );

    const dialog = await screen.findByRole("dialog", { name: "Launch Reel" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(container.querySelector(".list-pane")).toHaveAttribute("inert");
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Launch Reel" }),
      ).toHaveFocus(),
    );
  });

  it("fokussiert schon den Ladedialog und inertisiert die globale Navigation", async () => {
    const user = userEvent.setup();
    const pending = new Promise<Coop>(() => undefined);
    const loadingRepository = { ...repository, detail: vi.fn(() => pending) };
    const { container } = render(
      <>
        <nav className="sidebar">Desktop navigation</nav>
        <nav className="mobile-topbar">Mobile navigation</nav>
        <CoopWorkspace repository={loadingRepository} initialCoops={coops} />
      </>,
    );
    await user.click(
      within(
        screen.getByRole("table", { name: "Offene Kooperationen" }),
      ).getByRole("button", { name: "Launch Reel" }),
    );

    const dialog = await screen.findByRole("dialog", { name: "Launch Reel" });
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Launch Reel" }),
      ).toHaveFocus(),
    );
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(container.querySelector(".sidebar")).toHaveAttribute("inert");
    expect(container.querySelector(".mobile-topbar")).toHaveAttribute("inert");
  });

  it("hält den Tastaturfokus im Detaildialog", async () => {
    const user = userEvent.setup();
    render(<CoopWorkspace repository={repository} initialCoops={coops} />);
    await user.click(
      within(
        screen.getByRole("table", { name: "Offene Kooperationen" }),
      ).getByRole("button", { name: "Launch Reel" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Launch Reel" });
    const focusable = Array.from(
      dialog.querySelectorAll<HTMLElement>(
        'button:not([disabled]), a[href], select:not([disabled]), [tabindex="0"]',
      ),
    );
    const first = focusable[0];
    const last = focusable.at(-1)!;

    expect(screen.getByRole("heading", { name: "Launch Reel" })).toHaveFocus();
    await user.tab({ shift: true });
    expect(last).toHaveFocus();

    last.focus();
    await user.tab();
    expect(first).toHaveFocus();
    first.focus();
    await user.tab({ shift: true });
    expect(last).toHaveFocus();
  });

  it("schließt per Escape und Schließen und stellt den Auslöserfokus wieder her", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <CoopWorkspace repository={repository} initialCoops={coops} />,
    );
    const trigger = within(
      screen.getByRole("table", { name: "Offene Kooperationen" }),
    ).getByRole("button", { name: "Launch Reel" });

    await user.click(trigger);
    await screen.findByRole("dialog", { name: "Launch Reel" });
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(container.querySelector(".list-pane")).not.toHaveAttribute("inert");

    await user.click(trigger);
    await user.click(
      await screen.findByRole("button", { name: "Details schließen" }),
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => expect(trigger).toHaveFocus());
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
