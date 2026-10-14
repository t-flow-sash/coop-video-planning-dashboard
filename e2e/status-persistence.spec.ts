import {
  expect,
  test,
  type Locator,
  type Response as PlaywrightResponse,
} from "@playwright/test";

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

type ApiCoop = {
  id: string;
  title: string;
  status: string;
  version: number;
};

type ApiCoopPage = { items: ApiCoop[] };

async function successfulJson<T>(
  response: PlaywrightResponse,
  context: string,
): Promise<T> {
  const body = await response.text();
  expect(
    response.ok(),
    `${context}: ${response.request().method()} ${response.url()} -> ${response.status()} ${response.statusText()}\n${body}`,
  ).toBeTruthy();
  try {
    return JSON.parse(body) as T;
  } catch (error) {
    throw new Error(
      `${context}: ${response.request().method()} ${response.url()} lieferte kein JSON: ${body}`,
      { cause: error },
    );
  }
}

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

  const coopListResponsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      response.request().method() === "GET" && url.pathname === "/api/v1/coops"
    );
  });
  await page.goto(`${baseURL}/coops`);
  const initialPage = await successfulJson<ApiCoopPage>(
    await coopListResponsePromise,
    "Initiale Coop-Liste fehlgeschlagen",
  );

  const table = page.getByRole("table", { name: "Offene Kooperationen" });
  await expect(table).toBeVisible();

  const rows = table.locator("tbody tr");
  let target: {
    row: Locator;
    id: string;
    title: string;
    version: number;
    status: { value: string; label: string };
  } | null = null;
  for (let index = 0; index < (await rows.count()); index += 1) {
    const row = rows.nth(index);
    const select = row.getByRole("combobox", { name: /Status für/ });
    if (await select.isDisabled()) continue;
    const current = await select.inputValue();
    if (nextStatus[current]) {
      const title = (await row.locator(".title-link").innerText()).trim();
      const apiCoop = initialPage.items.find((coop) => coop.title === title);
      expect(
        apiCoop,
        `Coop „${title}“ fehlt in der initialen API-Antwort`,
      ).toBeDefined();
      target = {
        row,
        id: apiCoop!.id,
        title,
        version: apiCoop!.version,
        status: nextStatus[current],
      };
      break;
    }
  }
  expect(
    target,
    "Seed benötigt mindestens eine regulär änderbare offene Coop",
  ).not.toBeNull();

  const transitionResponsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      response.request().method() === "POST" &&
      url.pathname === `/api/v1/coops/${target!.id}/transitions`
    );
  });
  await target!.row
    .getByRole("combobox", { name: /Status für/ })
    .selectOption(target!.status.value);
  const transitioned = await successfulJson<ApiCoop>(
    await transitionResponsePromise,
    "Statusänderung fehlgeschlagen",
  );
  expect(transitioned.id).toBe(target!.id);
  expect(transitioned.status).toBe(target!.status.value);
  expect(transitioned.version).toBeGreaterThan(target!.version);
  await expect(page.getByRole("status")).toContainText("Coop gespeichert");

  const reloadedListResponsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      response.request().method() === "GET" && url.pathname === "/api/v1/coops"
    );
  });
  await page.reload();
  const reloadedPage = await successfulJson<ApiCoopPage>(
    await reloadedListResponsePromise,
    "Coop-Liste nach Reload fehlgeschlagen",
  );
  const persisted = reloadedPage.items.find((coop) => coop.id === target!.id);
  expect(
    persisted,
    `Coop ${target!.id} fehlt in der API-Antwort nach Reload`,
  ).toBeDefined();
  expect(persisted!.status).toBe(target!.status.value);
  expect(persisted!.version).toBe(transitioned.version);

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
