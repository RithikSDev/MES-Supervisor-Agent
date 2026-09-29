import { expect, test } from "@playwright/test";
import { openModule } from "./nav";

const shots = (name: string) => `test-results/screens/${name}.png`;

test("supervisor can sign in and walk the planning screens", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();

  // Keycloak login (demo realm credentials)
  await page.locator("#username").fill("supervisor1");
  await page.locator("#password").fill("demo");
  await page.locator("#kc-login").click();

  await expect(page.getByText("Welcome back,")).toBeVisible();
  await expect(page.locator(".module-card").first()).toBeVisible();
  await page.screenshot({ animations: "disabled", path: shots("00-home") });
  await openModule(page, "Work orders");
  await expect(page.locator(".kpi").first()).toBeVisible();
  await expect(page.locator("tbody tr").first()).toBeVisible();
  await page.screenshot({ animations: "disabled", path: shots("01-work-orders") });

  await page.locator("tbody tr").first().click();
  await expect(page.getByText("Operations (", { exact: false })).toBeVisible();
  await expect(page.locator("tbody tr").first()).toBeVisible();
  await page.screenshot({ animations: "disabled", path: shots("02-work-order-detail") });

  await openModule(page, "Schedule board");
  await expect(page.locator(".vis-timeline")).toBeVisible();
  await expect(page.locator(".vis-item.op").first()).toBeVisible({ timeout: 20_000 });
  await page.screenshot({ animations: "disabled", path: shots("03-schedule-board") });

  await openModule(page, "Integrations");
  await expect(page.getByText("Posted to SAP")).toBeVisible();
  await expect(page.locator("tbody").getByText("POSTED").first()).toBeVisible({ timeout: 15_000 });
  await page.screenshot({ animations: "disabled", path: shots("04-integration") });

  await openModule(page, "Audit trail");
  await expect(page.getByText("INSERT").first()).toBeVisible();
  await page.screenshot({ animations: "disabled", path: shots("05-audit") });

  await openModule(page, "SAP simulator");
  await expect(page.getByText("Fault injection")).toBeVisible();
  await page.screenshot({ animations: "disabled", path: shots("06-sap-simulator") });
});
