import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CoopWorkspace } from "@/features/coops/coop-workspace";
import type { Coop } from "@/features/coops/domain";

const base = {
  id: "00000000-0000-4000-8000-000000000001",
  record_key: "synthetic-p1",
  title: "Synthetic P1",
  status: "NEW",
  priority: "P1",
  last_contact_date: "2026-10-18",
  next_action: "Review synthetic brief",
  owner: "Test Owner",
  deadline: "2026-10-22",
  urgency: "UPCOMING",
  fee_status: "UNKNOWN",
  needs_review: false,
  uncertainty: null,
  brand: {
    id: "00000000-0000-4000-8000-000000000011",
    record_key: "brand-synthetic",
    name: "Synthetic Brand",
  },
  agency: {
    id: "00000000-0000-4000-8000-000000000012",
    record_key: "agency-synthetic",
    name: "Synthetic Agency",
  },
  contacts: [
    {
      record_key: "contact-synthetic",
      id: "00000000-0000-4000-8000-000000000013",
      display_name: "Synthetic Contact",
      email: "synthetic@example.invalid",
      role: "PRIMARY",
    },
  ],
  summary: null,
  deliverables: [],
  release_at: null,
  fee_amount_minor: null,
  fee_currency: null,
  version: 1,
  created_at: "2026-10-18T10:00:00Z",
  updated_at: "2026-10-18T10:00:00Z",
} as unknown as Coop;

const coops = [
  base,
  {
    ...base,
    id: "00000000-0000-4000-8000-000000000002",
    record_key: "synthetic-p0",
    title: "Synthetic urgent",
    priority: "P0",
    deadline: "2026-10-18",
    urgency: "OVERDUE",
    needs_review: true,
    uncertainty: "Synthetic uncertainty",
    updated_at: "2026-10-17T10:00:00Z",
  } as unknown as Coop,
];

const repository = {
  list: vi.fn(async () => coops),
  detail: vi.fn(
    async (recordKey: string) => coops.find((coop) => coop.id === recordKey)!,
  ),
  audit: vi.fn(async () => []),
  transition: vi.fn(async () => coops[0]),
};

describe("fachliche Koop-Übersicht", () => {
  it("zeigt den pausierten Stand und priorisiert P0 vor P1", async () => {
    render(<CoopWorkspace repository={repository} initialCoops={coops} />);

    expect(screen.getByText(/Stand 19\.10\.2026/)).toBeVisible();
    expect(screen.getByText(/Delta pausiert/)).toBeVisible();
    const rows = within(
      screen.getByRole("table", { name: "Offene Kooperationen" }),
    ).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("Synthetic urgent");
    expect(rows[1]).toHaveTextContent("P0");
    expect(rows[1]).toHaveTextContent(/Überfällig/);
    expect(rows[2]).toHaveTextContent("Synthetic P1");
  });

  it("zeigt Reviewstatus und Unsicherheit direkt in der Desktopliste", () => {
    render(<CoopWorkspace repository={repository} initialCoops={coops} />);

    const table = screen.getByRole("table", { name: "Offene Kooperationen" });
    expect(
      within(table).getByRole("columnheader", { name: "Review" }),
    ).toBeVisible();
    const urgentRow = within(table)
      .getByText("Synthetic urgent")
      .closest("tr")!;
    expect(urgentRow).toHaveTextContent("Prüfung erforderlich");
    expect(urgentRow).toHaveTextContent("Synthetic uncertainty");
  });

  it("zeigt operative Pflichtinformationen auf mobilen Karten", () => {
    const { container } = render(
      <CoopWorkspace repository={repository} initialCoops={coops} />,
    );

    const cards = container.querySelector<HTMLElement>(".mobile-cards")!;
    const urgentCard = within(cards)
      .getByText("Synthetic urgent")
      .closest("article")!;
    expect(urgentCard).toHaveTextContent("P0");
    expect(urgentCard).toHaveTextContent("Überfällig");
    expect(urgentCard).toHaveTextContent("18.10.2026");
    expect(urgentCard).toHaveTextContent("Review synthetic brief");
    expect(urgentCard).toHaveTextContent("Test Owner");
    expect(urgentCard).toHaveTextContent("18.10.2026");
    expect(urgentCard).toHaveTextContent("Prüfung erforderlich");
    expect(urgentCard).toHaveTextContent("Synthetic uncertainty");
  });

  it("zeigt Aktion, Owner, Kontakt, Agentur und Unsicherheit im Detail", async () => {
    render(<CoopWorkspace repository={repository} initialCoops={coops} />);
    within(screen.getByRole("table", { name: "Offene Kooperationen" }))
      .getByRole("button", { name: "Synthetic urgent" })
      .click();

    const detail = await screen.findByRole("dialog", {
      name: "Synthetic urgent",
    });
    expect(detail).toHaveTextContent("Review synthetic brief");
    expect(detail).toHaveTextContent("Test Owner");
    expect(detail).toHaveTextContent("Synthetic Contact");
    expect(detail).toHaveTextContent("Synthetic Agency");
    expect(detail).toHaveTextContent("Synthetic uncertainty");
  });
});
