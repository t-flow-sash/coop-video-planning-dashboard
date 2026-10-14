import { expect, test, type Locator } from "@playwright/test";

const baseURL = process.env.E2E_BASE_URL ?? "http://localhost:3000";
const nextStatus: Record<string, { value: string; label: string }> = {
  NEW: { value: "QUALIFYING", label: "In Klärung" },
  QUALIFYING: { value: "NEGOTIATING", label: "Verhandlung" },
  NEGOTIATING: { value: "QUALIFYING", label: "In Klärung" },
  CONFIRMED: { value: "IN_PRODUCTION", label: "In Produktion" },
  IN_PRODUCTION: { value: "REVIEW", label: "In Freigabe" },
  REVIEW: { value: "IN_PRODUCTION", label: "In Produktion" },
  SCHEDULED: { value: "REVIEW", label: "In Freigabe" },
  PUBLISHED: { value: "REVIEW", label: "In Freigabe" },
};

test("Statusänderung bleibt nach Reload erhalten und erscheint im Audit", async ({
  page,
}) => {
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      const location = message.location().url;
      consoleErrors.push(
        location ? `${message.text()} (${location})` : message.text(),
      );
    }
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await page.goto(`${baseURL}/coops`);
  const table = page.getByRole("table", { name: "Offene Kooperationen" });
  await expect(table).toBeVisible();

  const rows = table.locator("tbody tr");
  let target: {
    row: Locator;
    title: string;
    status: { value: string; label: string };
  } | null = null;
  for (let index = 0; index < (await rows.count()); index += 1) {
    const row = rows.nth(index);
    const select = row.getByRole("combobox", { name: /Status für/ });
    if (await select.isDisabled()) continue;
    const current = await select.inputValue();
    if (nextStatus[current]) {
      target = {
        row,
        title: (await row.locator(".title-link").innerText()).trim(),
        status: nextStatus[current],
      };
      break;
    }
  }
  expect(
    target,
    "Seed benötigt mindestens eine regulär änderbare offene Coop",
  ).not.toBeNull();

  await target!.row
    .getByRole("combobox", { name: /Status für/ })
    .selectOption(target!.status.value);
  await expect(page.getByRole("status")).toContainText("Coop gespeichert");

  await page.reload();
  const persistedRow = table
    .locator("tbody tr")
    .filter({ hasText: target!.title });
  await expect(
    persistedRow.getByRole("combobox", { name: /Status für/ }),
  ).toHaveValue(target!.status.value);
  await expect(persistedRow).toContainText(target!.status.label);

  await persistedRow.locator(".title-link").click();
  const details = page
    .getByRole("complementary", { name: /Coop-Details/ })
    .or(page.locator("aside.detail-panel"));
  await expect(
    details.getByRole("heading", { name: "Änderungshistorie" }),
  ).toBeVisible();
  await expect(details).toContainText(/Status geändert|STATUS_TRANSITIONED/);
  expect(consoleErrors, "Browserkonsole enthält Fehler").toEqual([]);
  expect(pageErrors, "Seite hat unbehandelte JavaScript-Fehler").toEqual([]);
});
