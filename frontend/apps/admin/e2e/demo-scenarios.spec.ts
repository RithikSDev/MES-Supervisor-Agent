import { type Page, expect, test } from "@playwright/test";
import { openModule } from "./nav";

// The demo scenarios of the AI Support Assistant, against the running stack with demo tickets loaded (admin menu ->
// Load demo tickets). Calls Claude (about $0.5-1), so it runs only with RUN_AI_DEMO=1. Screenshots in
// test-results/screens/demo-*.png.
const shots = (name: string) => `test-results/screens/${name}.png`;

async function ask(page: Page, text: string): Promise<void> {
  const box = page.getByLabel("Message to the AI support assistant");
  await box.fill(text);
  await box.press("Enter");
  const answers = page.locator(".assistant-thread .chat-card, .assistant-thread p");
  await page.waitForTimeout(1500);
  await expect(page.getByPlaceholder(/Ask about tickets/)).toBeVisible({ timeout: 180_000 });
  await expect(answers.first()).toBeVisible();
}

test("demo scenarios: forecast, SLA report, tickets of others, P1/P2 changes", async ({ page }) => {
  test.skip(!process.env.RUN_AI_DEMO, "calls Claude: set RUN_AI_DEMO=1");
  test.setTimeout(900_000);
  await page.setViewportSize({ width: 1500, height: 1000 });
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.locator("#username").fill("admin1");
  await page.locator("#password").fill("demo");
  await page.locator("#kc-login").click();
  await openModule(page, "AI support");
  await page.getByRole("button", { name: "New conversation" }).click();

  await ask(page, "Forecast the tickets for the next week and the next two weeks");
  await expect(page.locator(".chat-card", { hasText: "Ticket forecast" }).first()).toBeVisible();
  await expect(page.getByText("Expected tickets").first()).toBeVisible();
  await page.screenshot({ animations: "disabled", path: shots("demo-1-forecast"), fullPage: true });

  // the card shows the period asked for, not fixed 1 / 2 week options
  await ask(page, "Forecast the tickets for the next two months");
  const months = page.locator(".chat-card", { hasText: "next two months" }).last();
  await expect(months).toBeVisible();
  await expect(months.getByText("Busiest week")).toBeVisible();
  await months.scrollIntoViewIfNeeded();
  await page.screenshot({ animations: "disabled", path: shots("demo-1b-forecast-two-months") });

  await ask(page, "Give me the SLA breach and exit report for the last 7 days");
  const report = page.locator(".chat-card", { hasText: "SLA breach" }).last();
  await expect(report).toBeVisible();
  await expect(report.getByRole("button", { name: "CSV" })).toBeVisible();
  await report.scrollIntoViewIfNeeded();
  await page.screenshot({ animations: "disabled", path: shots("demo-2-sla-report") });

  await ask(page, "List all open tickets assigned to other engineers");
  const others = page.locator(".chat-card", { hasText: "assigned to others" }).last();
  await expect(others).toBeVisible();
  await others.scrollIntoViewIfNeeded();
  await page.screenshot({ animations: "disabled", path: shots("demo-3-assigned-to-others") });

  await ask(page, "Show the open P1 and P2 tickets. Raise the scanner problem on TURN-02 to P1, and assign it to Wei Chen.");
  const confirm = page.locator(".chat-card.confirm").last();
  await expect(confirm).toBeVisible();
  await confirm.scrollIntoViewIfNeeded();
  await page.screenshot({ animations: "disabled", path: shots("demo-4-p1-p2-changes") });
});
