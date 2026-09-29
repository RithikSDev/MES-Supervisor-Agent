import { expect, test } from "@playwright/test";
import { openModule } from "./nav";

// Every list / summary / report card in the assistant has a CSV download. Uses a saved conversation with cards (e.g.
// from demo-scenarios.spec), so no model call is needed.
test("report cards download CSV", async ({ page }) => {
  await page.setViewportSize({ width: 1500, height: 950 });
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.locator("#username").fill("admin1");
  await page.locator("#password").fill("demo");
  await page.locator("#kc-login").click();
  await openModule(page, "AI support");
  const items = page.locator(".assistant-item-open");
  await expect(items.first()).toBeVisible({ timeout: 20_000 });
  const buttons = page.getByRole("button", { name: /Download .* as CSV/ });
  // open saved conversations (read only) until one shows report cards
  let found = false;
  for (let i = 0; i < Math.min(await items.count(), 8) && !found; i++) {
    await items.nth(i).click();
    await page.waitForTimeout(2500);
    found = (await buttons.count()) > 0;
  }
  test.skip(!found, "needs a saved conversation with report cards");
  const n = await buttons.count();
  expect(n).toBeGreaterThanOrEqual(1);
  for (let i = 0; i < n; i++) {
    const b = buttons.nth(i);
    await b.scrollIntoViewIfNeeded();
    await expect(b).toBeEnabled({ timeout: 30_000 });
    const [dl] = await Promise.all([page.waitForEvent("download"), b.click()]);
    const path = `test-results/csv/${dl.suggestedFilename()}`;
    await dl.saveAs(path);
    expect(dl.suggestedFilename()).toMatch(/\.csv$/);
  }
});
