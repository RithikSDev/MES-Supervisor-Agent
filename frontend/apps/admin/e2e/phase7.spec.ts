import { expect, test } from "@playwright/test";
import { openModule } from "./nav";

// Phase 7 screens against the running stack: support analytics, notifications (alerts, rota, rules), the bell.
test("support analytics and notifications", async ({ page }) => {
  test.setTimeout(90_000);
  await page.setViewportSize({ width: 1600, height: 1500 });
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.locator("#username").fill("admin1");
  await page.locator("#password").fill("demo");
  await page.locator("#kc-login").click();
  await openModule(page, "Support analytics");
  await expect(page.getByText("Tickets raised and resolved per day")).toBeVisible();
  await expect(page.getByText("AI support engineers", { exact: true })).toBeVisible();
  await expect(page.locator(".recharts-surface").first()).toBeVisible();
  await page.screenshot({ animations: "disabled", path: "test-results/screens/p7-analytics.png", fullPage: true });
  await openModule(page, "Alerts");
  await expect(page.getByRole("tab", { name: "On-call rota" })).toBeVisible();
  await page.screenshot({ animations: "disabled", path: "test-results/screens/p7-alerts.png" });
  await page.getByRole("tab", { name: "On-call rota" }).click();
  await expect(page.getByRole("button", { name: "Send a test alert" })).toBeVisible();
  await page.getByRole("tab", { name: "Alert rules" }).click();
  await expect(page.getByText("An SLA target is at risk (75% of the time used)")).toBeVisible();
  await page.screenshot({ animations: "disabled", path: "test-results/screens/p7-rules.png" });
});
