import { type APIRequestContext, expect, test } from "@playwright/test";
import { openModule } from "./nav";

// Phase 5 screens against the running stack. The agent's answers need a valid ANTHROPIC_API_KEY; without one (or
// with the kill switch on) the terminal chat must fall back to the plain form, so both outcomes are accepted here.
const TERMINAL = process.env.MES_TERMINAL_URL ?? "http://localhost:3081";
const CORE = process.env.MES_CORE_URL ?? "http://localhost:8000/api/v1";
const KEYCLOAK = "http://localhost:8080/realms/mes/protocol/openid-connect/token";
const shots = (name: string) => `test-results/screens/${name}.png`;

async function token(request: APIRequestContext, user: string): Promise<Record<string, string>> {
  const r = await request.post(KEYCLOAK, { form: { grant_type: "password", client_id: "mes-cli", username: user, password: "demo" } });
  return { Authorization: `Bearer ${(await r.json()).access_token}` };
}

test("AI support page and the terminal assistant entry point", async ({ page, request }) => {
  test.setTimeout(120_000);
  await page.setViewportSize({ width: 1600, height: 950 });
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.locator("#username").fill("admin1");
  await page.locator("#password").fill("demo");
  await page.locator("#kc-login").click();
  await openModule(page, "AI support");
  // the assistant (engineer chat) opens first, with starter questions; the agent console is one click away
  await expect(page.getByText("How can I help")).toBeVisible();
  await expect(page.locator(".starter", { hasText: "Bring up all the open tickets" })).toBeVisible();
  await expect(page.getByRole("button", { name: "New conversation" })).toBeVisible();
  // full screen: conversation sidebar (search, delete with confirmation), fixed composer, no workspace header
  await expect(page.getByPlaceholder("Search conversations…")).toBeVisible();
  await expect(page.getByLabel("Message to the AI support assistant")).toBeVisible();
  await expect(page.locator(".shell-header")).toHaveCount(0);
  if (await page.locator(".assistant-item-delete").count()) {
    await page.locator(".assistant-item-delete").first().click();
    await expect(page.getByText("Delete conversation?")).toBeVisible();
    await page.getByRole("button", { name: "Cancel" }).click();
  }
  await page.screenshot({ animations: "disabled", path: shots("ai-0-assistant") });
  await page.getByText("Agent console").click();
  await expect(page.getByText("Tickets worked (24 h)")).toBeVisible();
  await expect(page.getByRole("tab", { name: /Approvals/ })).toBeVisible();
  await page.screenshot({ animations: "disabled", path: shots("ai-1-activity") });
  await page.getByRole("tab", { name: "Policy" }).click();
  await expect(page.getByText("Resolve the ticket")).toBeVisible();
  await expect(page.getByText("Kill switch")).toBeVisible();
  await page.screenshot({ animations: "disabled", path: shots("ai-2-policy") });

  // terminal: Get help offers the assistant first
  const admin = await token(request, "admin1");
  const plantId = (await (await request.get(`${CORE}/plants`, { headers: admin })).json())[0].id;
  const terminals = await (await request.get(`${CORE}/terminals`, { params: { plant_id: plantId }, headers: admin })).json();
  const t1 = terminals.find((t: { code: string }) => t.code === "T-L1");
  const device = (await (await request.post(`${CORE}/terminals/${t1.id}/enroll`, { headers: admin })).json()).device_token;
  const term = await page.context().newPage();
  await term.setViewportSize({ width: 1280, height: 800 });
  await term.goto(`${TERMINAL}/#enroll=${device}`);
  await expect(term.getByText("Enter your PIN")).toBeVisible();
  for (const k of ["1", "1", "1", "1", "OK"]) await term.getByRole("button", { name: k, exact: true }).click();
  await expect(term.getByText("Work queue · line L1")).toBeVisible();
  await term.getByRole("button", { name: /Get help/ }).click();
  await term.getByRole("button", { name: /Talk to the support assistant/ }).click();
  // either the assistant greets the operator (agent active) or the plain form is offered
  await expect(term.getByText(/support assistant for line L1|Use the form|not available/).first()).toBeVisible({ timeout: 15_000 });
  await term.screenshot({ animations: "disabled", path: shots("ai-3-terminal-assistant") });
});
