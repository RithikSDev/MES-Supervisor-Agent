import { expect, test } from "@playwright/test";

// Admin account menu -> Reset demo data. Opens the dialog and checks the safety step; RUN_RESET=1 also runs it
// (empties all data of the shared stack, so it is not part of the normal suite).
test("admin can reset the demo data from the account menu", async ({ page }) => {
  test.setTimeout(180_000);
  await page.setViewportSize({ width: 1500, height: 950 });
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.locator("#username").fill("admin1");
  await page.locator("#password").fill("demo");
  await page.locator("#kc-login").click();
  await page.getByRole("button", { name: "Account" }).click();
  await page.getByRole("menuitem", { name: "Reset demo data…" }).click();
  await expect(page.getByText("This can't be undone.")).toBeVisible();
  const go = page.getByRole("button", { name: "Reset all data" });
  await expect(go).toBeDisabled();
  await page.getByLabel("Type RESET to confirm").fill("RESET");
  await expect(go).toBeEnabled();
  await page.screenshot({ animations: "disabled", path: "test-results/screens/reset-1-confirm.png" });
  if (!process.env.RUN_RESET) {
    await page.getByRole("button", { name: "Cancel" }).click();
    return;
  }
  await go.click();
  await expect(page.getByRole("button", { name: "Start fresh" })).toBeVisible({ timeout: 120_000 });
  await page.screenshot({ animations: "disabled", path: "test-results/screens/reset-2-done.png" });
  await expect(page.getByText("Done. The app restarts")).toBeVisible();
  await page.getByRole("button", { name: "Start fresh" }).click();
  await expect(page.locator(".module-card").first()).toBeVisible({ timeout: 30_000 });
});
