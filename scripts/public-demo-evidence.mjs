import { chromium } from "@playwright/test";

const baseURL =
  process.env.DEMO_BASE_URL ??
  "http://127.0.0.1:4173/coop-video-planning-dashboard/";
const browser = await chromium.launch({ channel: "chrome", headless: true });
const errors = [];
const failedRequests = [];
const crossOriginRequests = [];

async function observe(page, label) {
  page.on("console", (message) => {
    if (message.type() === "error")
      errors.push(`${label} console: ${message.text()}`);
  });
  page.on("pageerror", (error) =>
    errors.push(`${label} pageerror: ${error.message}`),
  );
  page.on("requestfailed", (request) =>
    failedRequests.push(`${request.method()} ${request.url()}`),
  );
  page.on("request", (request) => {
    const target = new URL(request.url());
    const expected = new URL(baseURL);
    if (target.origin !== expected.origin)
      crossOriginRequests.push(request.url());
  });
}

const desktop = await browser.newPage({
  viewport: { width: 1440, height: 1000 },
});
await observe(desktop, "desktop");
await desktop.goto(baseURL, { waitUntil: "networkidle" });
await desktop.evaluate(() => localStorage.clear());
await desktop.reload({ waitUntil: "networkidle" });
await desktop.getByRole("table", { name: "Offene Kooperationen" }).waitFor();
await desktop.getByRole("button", { name: "Herbstküche Kurzvideo" }).click();
await desktop
  .getByRole("complementary", { name: "Herbstküche Kurzvideo" })
  .waitFor();
await desktop.screenshot({
  path: "artifacts/m1-coops-desktop.png",
});
await desktop.keyboard.press("Escape");
await desktop
  .getByPlaceholder("Marke, Coop, Kontakt oder Agentur suchen")
  .fill("Mosaik Studio");
await desktop.getByText(/2 offene Kooperationen für/).waitFor();
await desktop
  .getByPlaceholder("Marke, Coop, Kontakt oder Agentur suchen")
  .fill("");
const firstStatus = desktop
  .getByLabel("Status für Herbstküche Kurzvideo ändern")
  .first();
await firstStatus.selectOption("QUALIFYING");
await desktop
  .getByRole("status")
  .filter({ hasText: "Coop gespeichert" })
  .waitFor();
await desktop.reload({ waitUntil: "networkidle" });
if (
  (await desktop
    .getByLabel("Status für Herbstküche Kurzvideo ändern")
    .first()
    .inputValue()) !== "QUALIFYING"
) {
  throw new Error("Statusänderung wurde nicht in localStorage persistiert");
}
await desktop.getByRole("button", { name: "Demo zurücksetzen" }).click();

const mobile = await browser.newPage({
  viewport: { width: 390, height: 844 },
  isMobile: true,
  hasTouch: true,
});
await observe(mobile, "mobile");
await mobile.goto(baseURL, { waitUntil: "networkidle" });
await mobile
  .getByRole("button", { name: "Herbstküche Kurzvideo", exact: true })
  .click();
await mobile
  .getByRole("complementary", { name: "Herbstküche Kurzvideo" })
  .waitFor();
await mobile.screenshot({
  path: "artifacts/m1-coops-mobile.png",
});
await mobile.keyboard.press("Escape");

await browser.close();

if (errors.length || failedRequests.length || crossOriginRequests.length) {
  throw new Error(
    JSON.stringify({ errors, failedRequests, crossOriginRequests }, null, 2),
  );
}
console.log(
  JSON.stringify(
    {
      desktop: "1440x1000 split detail",
      mobile: "390x844 full-screen detail",
      consoleErrors: errors.length,
      failedRequests: failedRequests.length,
      crossOriginRequests: crossOriginRequests.length,
      interactions: [
        "search",
        "detail open/escape",
        "status localStorage/reload",
        "reset",
      ],
    },
    null,
    2,
  ),
);
