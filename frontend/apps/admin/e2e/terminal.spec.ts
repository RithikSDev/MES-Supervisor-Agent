import { type APIRequestContext, expect, test } from "@playwright/test";
import { openModule } from "./nav";

// Drives the operator terminal (:3081) against the running compose stack, then checks the admin views.
const TERMINAL = process.env.MES_TERMINAL_URL ?? "http://localhost:3081";
const CORE = process.env.MES_CORE_URL ?? "http://localhost:8000/api/v1";
const MOCK = process.env.MES_MOCK_SAP_URL ?? "http://localhost:8030/_mock";
const KEYCLOAK = "http://localhost:8080/realms/mes/protocol/openid-connect/token";
const shots = (name: string) => `test-results/screens/${name}.png`;

async function token(request: APIRequestContext, user: string): Promise<Record<string, string>> {
  const r = await request.post(KEYCLOAK, { form: { grant_type: "password", client_id: "mes-cli", username: user, password: "demo" } });
  return { Authorization: `Bearer ${(await r.json()).access_token}` };
}

/** A fresh machining-line order, scheduled and dispatched through the API. */
async function dispatchedL1Order(request: APIRequestContext): Promise<string> {
  const sup = await token(request, "supervisor1");
  let order: string | undefined;
  while (!order) {
    const created: string[] = (await (await request.post(`${MOCK}/orders/generate`, { data: { count: 3, release_ratio: 1 } })).json()).created;
    const all: { order: string; material: string }[] = await (await request.get(`${MOCK}/orders`)).json();
    order = created.find((o) => ["FG-1001", "FG-1002", "SF-1003"].includes(all.find((x) => x.order === o)!.material));
  }
  const plantId = (await (await request.get(`${CORE}/plants`, { headers: sup })).json())[0].id;
  let wo: { id: string; row_version: number; status: string } | undefined;
  await expect.poll(async () => {
    const items = (await (await request.get(`${CORE}/work-orders`, { params: { search: order! }, headers: sup })).json()).items;
    wo = items[0];
    return Boolean(wo);
  }, { timeout: 60_000 }).toBe(true);
  const sid = (await (await request.post(`${CORE}/schedules`, { data: { plant_id: plantId, horizon_days: 14 }, headers: sup })).json()).id;
  // the shared stack collects many test orders: put this one first so it always fits the horizon
  const fresh = await (await request.get(`${CORE}/work-orders/${wo!.id}`, { headers: sup })).json();
  await request.put(`${CORE}/work-orders/${wo!.id}/priority`, { data: { row_version: fresh.row_version, priority: 1 }, headers: sup });
  await request.post(`${CORE}/schedules/${sid}/auto-sequence`, { data: { reset_unlocked: true }, headers: sup });
  expect((await request.post(`${CORE}/schedules/${sid}/publish`, { data: { reason: "UI test" }, headers: sup })).status()).toBe(200);
  const detail = await (await request.get(`${CORE}/work-orders/${wo!.id}`, { headers: sup })).json();
  expect((await request.post(`${CORE}/work-orders/${wo!.id}/dispatch`, { data: { row_version: detail.row_version }, headers: sup })).status()).toBe(200);
  return order;
}

test("operator runs an operation on the terminal, including an outage", async ({ page, request }) => {
  test.setTimeout(180_000);
  const order = await dispatchedL1Order(request);
  const admin = await token(request, "admin1");
  const plantId = (await (await request.get(`${CORE}/plants`, { headers: admin })).json())[0].id;
  const terminals = await (await request.get(`${CORE}/terminals`, { params: { plant_id: plantId }, headers: admin })).json();
  const t1 = terminals.find((t: { code: string }) => t.code === "T-L1");
  const device = (await (await request.post(`${CORE}/terminals/${t1.id}/enroll`, { headers: admin })).json()).device_token;

  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto(`${TERMINAL}/#enroll=${device}`);
  await expect(page.getByText("Enter your PIN")).toBeVisible();
  await page.screenshot({ animations: "disabled", path: shots("t1-pin") });
  for (const k of ["1", "1", "1", "1", "OK"]) await page.getByRole("button", { name: k, exact: true }).click();

  await expect(page.getByText("Work queue · line L1")).toBeVisible();
  const card = page.locator(".t-card", { hasText: order }).first();
  await expect(card).toBeVisible();
  await page.screenshot({ animations: "disabled", path: shots("t2-queue") });

  await card.click();
  await expect(page.getByText("NOT STARTED")).toBeVisible();
  await page.getByRole("button", { name: /Start setup/ }).click();
  await expect(page.locator(".t-state[data-state='SETUP']")).toBeVisible();
  await page.getByRole("button", { name: /Start production/ }).click();
  await expect(page.locator(".t-state[data-state='RUNNING']")).toBeVisible();

  await page.getByRole("button", { name: /Report qty/ }).click();
  for (const k of ["5"]) await page.locator(".t-keypad").getByRole("button", { name: k, exact: true }).click();
  await page.getByRole("button", { name: /^Report$/ }).click();
  await expect(page.getByText(/5 \/ \d+ PC/).first()).toBeVisible();
  await page.screenshot({ animations: "disabled", path: shots("t3-running") });

  // network goes down: the pause is recorded on the terminal and synced afterwards
  await page.getByRole("button", { name: "terminal menu" }).click();
  await page.getByRole("switch", { name: "Simulate network outage" }).check({ force: true });
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: /Pause/ }).click();
  await page.getByRole("button", { name: "Material shortage" }).click();
  await expect(page.getByText("Saved on this terminal", { exact: true })).toBeVisible();
  await expect(page.locator(".t-pill.offline")).toContainText("1 to sync");
  await expect(page.locator(".t-state[data-state='PAUSED']")).toBeVisible();
  await page.screenshot({ animations: "disabled", path: shots("t4-offline") });

  await page.getByRole("button", { name: "terminal menu" }).click();
  await page.getByRole("switch", { name: "Simulate network outage" }).uncheck({ force: true });
  await page.keyboard.press("Escape");
  await expect(page.locator(".t-pill.online")).toBeVisible({ timeout: 15_000 });
  await expect(page.locator(".t-pill")).not.toContainText("to sync", { timeout: 15_000 });
  await expect(page.getByText("not yet synced")).toHaveCount(0, { timeout: 15_000 });
  await expect(page.locator(".t-state[data-state='PAUSED']")).toBeVisible();
});

test.afterAll(async ({ playwright }) => {
  // stop any run left on line L1, so the machines are free the next time this suite runs
  const request = await playwright.request.newContext();
  const admin = await token(request, "admin1");
  const plantId = (await (await request.get(`${CORE}/plants`, { headers: admin })).json())[0].id;
  const t1 = (await (await request.get(`${CORE}/terminals`, { params: { plant_id: plantId }, headers: admin })).json()).find((t: { code: string }) => t.code === "T-L1");
  const device = (await (await request.post(`${CORE}/terminals/${t1.id}/enroll`, { headers: admin })).json()).device_token;
  const login = await (await request.post(`${CORE}/terminal/login`, { data: { pin: "1111" }, headers: { "X-Terminal-Device": device } })).json();
  const op = { Authorization: `Bearer ${login.access_token}` };
  const queue = await (await request.get(`${CORE}/execution/queue`, { headers: op })).json();
  for (const item of queue.items) {
    if (item.active_run) await request.post(`${CORE}/execution/runs/${item.active_run.id}/stop`, { data: { note: "test cleanup" }, headers: op });
  }
  await request.dispose();
});

test("supervisor sees the line live, materials and terminals", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.locator("#username").fill("supervisor1");
  await page.locator("#password").fill("demo");
  await page.locator("#kc-login").click();

  await openModule(page, "Line status");
  await expect(page.getByText("live", { exact: true })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("PAUSED").first()).toBeVisible();
  await page.screenshot({ animations: "disabled", path: shots("a1-line-status") });

  await openModule(page, "Materials");
  await expect(page.locator("tbody tr").first()).toBeVisible();
  await page.screenshot({ animations: "disabled", path: shots("a2-materials") });
  await page.getByRole("tab", { name: /Shortages/ }).click();
  await page.screenshot({ animations: "disabled", path: shots("a3-shortages") });
  await page.getByRole("tab", { name: "Movements" }).click();
  await expect(page.getByText("Issue to order").first()).toBeVisible();
  await page.screenshot({ animations: "disabled", path: shots("a4-movements") });

  await openModule(page, "Terminals");
  await expect(page.getByText("T-L1").first()).toBeVisible();
  await page.screenshot({ animations: "disabled", path: shots("a5-terminals") });
});
