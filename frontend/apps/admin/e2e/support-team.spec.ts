import { expect, test } from "@playwright/test";
import { openModule } from "./nav";

// Support team page: the engineers, the add dialog and (when ServiceNow is connected) the read-only readiness check.
test("support team page", async ({ page }) => {
  await page.setViewportSize({ width: 1500, height: 950 });
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.locator("#username").fill("admin1");
  await page.locator("#password").fill("demo");
  await page.locator("#kc-login").click();
  await openModule(page, "Support Team");
  await expect(page.getByRole("cell", { name: /Priya Nair/ })).toBeVisible();
  await page.getByRole("button", { name: "Add engineer" }).click();
  await page.getByLabel("E-mail").fill("new.person@example.com");
  await expect(page.getByLabel("User name")).toHaveValue("new.person");
  await page.getByRole("button", { name: "Cancel" }).click();
  await page.getByRole("button", { name: "Check" }).click();
  const rows = page.locator("tbody tr");
  await expect(rows.filter({ hasText: "not checked" })).toHaveCount(0, { timeout: 90_000 });
  await expect(rows.first()).toContainText(/ready|no ServiceNow user|no itil role|not in group/);
  await page.screenshot({ animations: "disabled", path: "test-results/screens/support-team.png" });
});
