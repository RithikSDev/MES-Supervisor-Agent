import { expect, test } from "@playwright/test";
import { openModule } from "./nav";

// ServiceNow / Jira connections are set up in the UI (admins only): the form with its test step opens
test("admin opens the ServiceNow connection form", async ({ page }) => {
  await page.setViewportSize({ width: 1500, height: 1000 });
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.locator("#username").fill("admin1");
  await page.locator("#password").fill("demo");
  await page.locator("#kc-login").click();
  await openModule(page, "Integrations");
  await page.getByRole("tab", { name: /ServiceNow/ }).click();
  await page.waitForTimeout(1500);
  await page.screenshot({ path: "test-results/screens/itsm-1-cards.png" });
  await page.getByRole("button", { name: /Set up|Configure/ }).first().click();
  await expect(page.getByText("Test connection")).toBeVisible();
  await expect(page.getByLabel(/Password|API token/).first()).toHaveAttribute("type", "password");
  await page.waitForTimeout(800);
  await page.screenshot({ path: "test-results/screens/itsm-2-form.png" });
});
