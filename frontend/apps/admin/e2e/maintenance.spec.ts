import { type APIRequestContext, type Page, expect, test } from "@playwright/test";
import { openModule } from "./nav";

// Phase 4 against the running compose stack: an operator gets help from the terminal (machine breakdown to
// maintenance, a P1 MES problem to support), a technician repairs and signs the machine off in technician mode,
// the supervisor escalates the ticket L1 -> L2 -> L3. The ServiceNow / Jira checks run only when the itsm-adapter
// is connected to real instances (.env); otherwise the ticket just stays queued.
const TERMINAL = process.env.MES_TERMINAL_URL ?? "http://localhost:3081";
const CORE = process.env.MES_CORE_URL ?? "http://localhost:8000/api/v1";
const ITSM = process.env.MES_ITSM_URL ?? "http://localhost:8070/api/v1/itsm";
const KEYCLOAK = "http://localhost:8080/realms/mes/protocol/openid-connect/token";
const shots = (name: string) => `test-results/screens/${name}.png`;

async function token(request: APIRequestContext, user: string): Promise<Record<string, string>> {
  const r = await request.post(KEYCLOAK, { form: { grant_type: "password", client_id: "mes-cli", username: user, password: "demo" } });
  return { Authorization: `Bearer ${(await r.json()).access_token}` };
}

async function itsmConnected(request: APIRequestContext): Promise<{ servicenow: boolean; jira: boolean }> {
  const r = await request.get(`${ITSM}/status`, { headers: await token(request, "supervisor1") });
  const conns: { system: string; reachable: boolean }[] = (await r.json()).connections;
  const up = (s: string) => conns.some((c) => c.system === s && c.reachable);
  return { servicenow: up("servicenow"), jira: up("jira") };
}

async function pin(page: Page, digits: string) {
  for (const k of [...digits, "OK"]) await page.getByRole("button", { name: k, exact: true }).click();
}

async function machine(request: APIRequestContext, code: string): Promise<{ id: string; status: string }> {
  const sup = await token(request, "supervisor1");
  const plantId = (await (await request.get(`${CORE}/plants`, { headers: sup })).json())[0].id;
  const eqs = await (await request.get(`${CORE}/maintenance/equipment`, { params: { plant_id: plantId }, headers: sup })).json();
  return eqs.find((e: { code: string }) => e.code === code);
}

test("breakdown and support ticket from the terminal, technician repair, escalation to L3", async ({ page, request }) => {
  test.setTimeout(240_000);
  const itsm = await itsmConnected(request);
  // start from an available TURN-01 (a previous run of this spec may have left it down)
  const before = await machine(request, "TURN-01");
  if (before.status === "DOWN") {
    await request.post(`${CORE}/maintenance/equipment/${before.id}/back-up`, { data: { note: "test reset" }, headers: await token(request, "supervisor1") });
  }
  const admin = await token(request, "admin1");
  const plantId = (await (await request.get(`${CORE}/plants`, { headers: admin })).json())[0].id;
  const t1 = (await (await request.get(`${CORE}/terminals`, { params: { plant_id: plantId }, headers: admin })).json()).find((t: { code: string }) => t.code === "T-L1");
  const device = (await (await request.post(`${CORE}/terminals/${t1.id}/enroll`, { headers: admin })).json()).device_token;

  // ---- operator: Get help -> machine problem (TURN-01 is down)
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto(`${TERMINAL}/#enroll=${device}`);
  await pin(page, "1111");
  await page.getByRole("button", { name: /Get help/ }).click();
  await page.screenshot({ animations: "disabled", path: shots("m-t1-help") });
  await page.getByRole("button", { name: /Machine problem/ }).click();
  const dlg = page.locator(".mantine-Modal-content");
  await dlg.getByRole("button", { name: /TURN-01/ }).click();
  await dlg.getByRole("button", { name: "Yes, it is down" }).click();
  await dlg.getByRole("button", { name: "Electrical failure" }).click();
  await dlg.getByRole("textbox").fill("Spindle drive alarm 2031");
  await page.screenshot({ animations: "disabled", path: shots("m-t2-breakdown") });
  await dlg.getByRole("button", { name: /Call maintenance/ }).click();
  await expect(page.getByText("Maintenance notified")).toBeVisible();
  await expect(page.locator(".t-machine[data-state='DOWN']", { hasText: "TURN-01" })).toBeVisible({ timeout: 10_000 });

  // ---- operator: Get help -> MES problem, line stopped (P1)
  await page.getByRole("button", { name: /Get help/ }).click();
  await page.getByRole("button", { name: /MES \/ system problem/ }).click();
  await dlg.getByRole("button", { name: /Scanner or label printer/ }).click();
  await dlg.getByRole("button", { name: /The line is stopped/ }).click();
  await dlg.getByRole("textbox").fill("Label printer at end of line prints blank labels (E2E)");
  await page.screenshot({ animations: "disabled", path: shots("m-t3-ticket") });
  await dlg.getByRole("button", { name: "Send to support" }).click();
  await expect(page.getByText("Support ticket raised")).toBeVisible();
  await page.getByRole("button", { name: /Log out/ }).click();

  // ---- technician (maint1, PIN 5555): take the notification, repair, sign off
  await pin(page, "5555");
  await expect(page.getByText("Maintenance · line L1")).toBeVisible();
  const notif = page.locator(".t-card", { hasText: "Spindle drive alarm 2031" }).first();
  await expect(notif).toBeVisible();
  await page.screenshot({ animations: "disabled", path: shots("m-t4-technician") });
  await notif.getByRole("button", { name: /Take it/ }).click();
  await page.getByRole("button", { name: /Start work/ }).click();
  await expect(page.getByText("MAINTENANCE IN PROGRESS")).toBeVisible();
  for (const task of ["Find the cause and repair", "Test the machine", "Clean up the work area"]) {
    await page.getByRole("button", { name: task }).click();
    await expect(page.locator(".t-choice.selected", { hasText: task })).toBeVisible();
  }
  await page.getByRole("button", { name: /SP-BEARING-6205/ }).click();
  await page.getByRole("button", { name: /Book spare part/ }).click();
  await expect(page.getByText("Spare part booked")).toBeVisible();
  await page.getByLabel("What did you find and do?").fill("Drive fuse blown and replaced; spindle tested at 3000 rpm");
  await page.getByRole("button", { name: /Sign off and release machine/ }).click();
  await page.screenshot({ animations: "disabled", path: shots("m-t5-signoff") });
  await page.getByRole("button", { name: "Sign", exact: true }).click();
  await expect(page.getByText("COMPLETED · MACHINE RELEASED")).toBeVisible();
  expect((await machine(request, "TURN-01")).status).toBe("AVAILABLE");
  await page.getByRole("button", { name: /Log out/ }).click();

  // ---- supervisor: triage the P1 ticket, escalate to L3 (Jira)
  await page.setViewportSize({ width: 1600, height: 950 });
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.locator("#username").fill("supervisor1");
  await page.locator("#password").fill("demo");
  await page.locator("#kc-login").click();
  await openModule(page, "Support tickets");
  const row = page.locator("tbody tr", { hasText: "blank labels (E2E)" }).first();
  await expect(row).toContainText("P1");
  if (itsm.servicenow) await expect(row).toContainText(/INC\d+/, { timeout: 30_000 });
  await page.screenshot({ animations: "disabled", path: shots("m-a1-tickets") });
  await row.click();
  const drawer = page.locator(".mantine-Drawer-content");
  await drawer.getByRole("button", { name: "Acknowledge" }).click();
  await expect(drawer.getByText("acknowledged", { exact: true })).toBeVisible();
  for (const [target, tier] of [["L2", "L2"], ["L3 (Jira)", "L3"]]) {
    await drawer.getByRole("button", { name: `Escalate to ${target}` }).click();
    const modal = page.locator(".mantine-Modal-content");
    await modal.getByRole("textbox").fill(tier === "L2" ? "printer driver crash, not a settings issue" : "needs a code fix in the label service");
    await modal.getByRole("button", { name: "Confirm" }).click();
    await expect(modal).toHaveCount(0);
    await expect(page.locator(".mantine-Drawer-title")).toContainText(tier);
  }
  if (itsm.servicenow) await expect(drawer.locator('a[href*="service-now.com"], a[href*="nav_to.do"]').first()).toBeVisible();
  if (itsm.servicenow && itsm.jira) await expect(drawer.locator('a[href*="/browse/"]')).toBeVisible({ timeout: 30_000 });  // Jira issue
  await page.screenshot({ animations: "disabled", path: shots("m-a2-ticket-jira") });
  await page.keyboard.press("Escape");

  await openModule(page, "Maintenance");
  await expect(page.locator(".machine-card", { hasText: "TURN-01" })).toContainText("AVAILABLE");
  await page.screenshot({ animations: "disabled", path: shots("m-a3-maintenance") });
  await page.getByRole("tab", { name: "Downtime log" }).click();
  await page.screenshot({ animations: "disabled", path: shots("m-a4-downtime") });
  await openModule(page, "Integrations");
  await page.getByRole("tab", { name: /ServiceNow \/ Jira/ }).click();
  await expect(page.getByTestId("itsm-conn-servicenow")).toContainText(itsm.servicenow ? "Table API" : "not configured");
  await expect(page.getByTestId("itsm-conn-jira")).toBeVisible();
  await page.screenshot({ animations: "disabled", path: shots("m-a5-itsm") });
});
