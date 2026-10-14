import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DemoWorkspace } from "@/features/coops/demo-workspace";
import { DEMO_STORAGE_KEY } from "@/features/coops/demo-data";

describe("öffentlicher Demo-Modus", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.history.replaceState({}, "", "/coops");
  });

  it("lädt ausschließlich lokale Fixtures und zeigt den dauerhaften Sicherheitshinweis", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    render(<DemoWorkspace />);

    expect(await screen.findByRole("note")).toHaveTextContent(
      "Öffentliche Demo",
    );
    expect(screen.getByRole("note")).toHaveTextContent(
      "Ausschließlich synthetische Beispieldaten",
    );
    expect(screen.getByRole("note")).toHaveTextContent(
      "keine API, Gmail- oder Agentenbearbeitung",
    );
    expect(
      await screen.findByRole("table", { name: "Offene Kooperationen" }),
    ).toBeVisible();
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });

  it("sucht Titel, Marke, Kontakt und Agentur clientseitig", async () => {
    const user = userEvent.setup();
    render(<DemoWorkspace />);
    const search = await screen.findByPlaceholderText(
      "Marke, Coop, Kontakt oder Agentur suchen",
    );

    for (const value of [
      "Herbstküche",
      "Fabelpfad",
      "Mara Beispiel",
      "Mosaik Studio",
    ]) {
      await user.clear(search);
      await user.type(search, value);
      await waitFor(() =>
        expect(
          screen.getAllByText("Herbstküche Kurzvideo").length,
        ).toBeGreaterThan(0),
      );
    }
  });

  it("persistiert eine lokale Statusänderung und setzt die Demo zurück", async () => {
    const user = userEvent.setup();
    render(<DemoWorkspace />);
    const table = await screen.findByRole("table", {
      name: "Offene Kooperationen",
    });
    const row = within(table).getByText("Herbstküche Kurzvideo").closest("tr")!;
    await user.selectOptions(
      within(row).getByLabelText(/Status für Herbstküche/),
      "QUALIFYING",
    );

    await waitFor(() =>
      expect(window.localStorage.getItem(DEMO_STORAGE_KEY)).toContain(
        "QUALIFYING",
      ),
    );
    await user.click(screen.getByRole("button", { name: "Demo zurücksetzen" }));
    expect(window.localStorage.getItem(DEMO_STORAGE_KEY)).toContain(
      "NEGOTIATING",
    );
  });

  it("öffnet das Detail per Tastatur, schließt mit Escape und gibt den Fokus zurück", async () => {
    render(<DemoWorkspace />);
    const table = await screen.findByRole("table", {
      name: "Offene Kooperationen",
    });
    const row = within(table).getByText("Herbstküche Kurzvideo").closest("tr")!;
    row.focus();
    fireEvent.keyDown(row, { key: "Enter" });
    expect(await screen.findByRole("complementary")).toBeVisible();
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() =>
      expect(screen.queryByRole("complementary")).not.toBeInTheDocument(),
    );
    await waitFor(() => expect(row).toHaveFocus());
  });
});
