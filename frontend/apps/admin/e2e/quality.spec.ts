import { type APIRequestContext, type Page, expect, test } from "@playwright/test";
import { openModule } from "./nav";

// Phase 3 against the running compose stack: first-off inspection on the terminal (fail -> hold -> release ->
// pass), serial numbers at goods receipt, final inspection usage decision with e-signature, genealogy.
const TERMINAL = process.env.MES_TERMINAL_URL ?? "http://localhost:3081";
const CORE = process.env.MES_CORE_URL ?? "http://localhost:8000/api/v1";
const MOCK = process.env.MES_MOCK_SAP_URL ?? "http://localhost:8030/_mock";
const KEYCLOAK = "http://localhost:8080/realms/mes/protocol/openid-connect/token";
const shots = (name: string) => `test-results/screens/${name}.png`;

async function token(request: APIRequestContext, user: string): Promise<Record<string, string>> {
  const r = await request.post(KEYCLOAK, { form: { grant_type: "password", client_id: "mes-cli", username: user, password: "demo" } });
  return { Authorization: `Bearer ${(await r.json()).access_token}` };
}

/** A fresh FG-1001 order (pump housing: first-off check on op 0020, final inspection, serialised), dispatched. */
async function dispatchedPumpOrder(request: APIRequestContext): Promise<{ order: string; id: string }> {
  const sup = await token(request, "supervisor1");
  let order: string | undefined;
  while (!order) {
    const created: string[] = (await (await request.post(`${MOCK}/orders/generate`, { data: { count: 4, release_ratio: 1 } })).json()).created;
    const all: { order: string; material: string }[] = await (await request.get(`${MOCK}/orders`)).json();
    order = created.find((o) => all.find((x) => x.order === o)!.material === "FG-1001");
  }
  const plantId = (await (await request.get(`${CORE}/plants`, { headers: sup })).json())[0].id;
  let wo: { id: string } | undefined;
  await expect.poll(async () => {
    wo = (await (await request.get(`${CORE}/work-orders`, { params: { search: order! }, headers: sup })).json()).items[0];
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
  return { order, id: wo!.id };
}

async function keypad(page: Page, text: string) {
  const pad = page.locator(".mantine-Modal-content .t-keypad");
  for (const k of text) await pad.getByRole("button", { name: k, exact: true }).click();
}

async function measure(page: Page, flange: string) {
  await page.getByRole("button", { name: /Flange diameter/ }).click();
  await keypad(page, flange);
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await page.getByRole("button", { name: /^OK$/ }).click();
  await page.screenshot({ animations: "disabled", path: shots(`q-t-inspect-${flange}`) });
  await page.getByRole("button", { name: /Save inspection/ }).click();
}

async function signIn(page: Page, user: string) {
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.locator("#username").fill(user);
  await page.locator("#password").fill("demo");
  await page.locator("#kc-login").click();
}

async function sign(page: Page, reason: string) {
  const modal = page.locator(".mantine-Modal-content", { hasText: "Electronic signature" });
  await modal.getByLabel("Reason for signing").fill(reason);
  await modal.getByRole("checkbox").check({ force: true });
  await modal.getByRole("button", { name: /^Sign/ }).click();
}

test("first-off inspection, quality hold, serials, usage decision and genealogy", async ({ page, request }) => {
  test.setTimeout(240_000);
  const { order, id: woId } = await dispatchedPumpOrder(request);
  const admin = await token(request, "admin1");
  const plantId = (await (await request.get(`${CORE}/plants`, { headers: admin })).json())[0].id;
  const t1 = (await (await request.get(`${CORE}/terminals`, { params: { plant_id: plantId }, headers: admin })).json()).find((t: { code: string }) => t.code === "T-L1");
  const device = (await (await request.post(`${CORE}/terminals/${t1.id}/enroll`, { headers: admin })).json()).device_token;

  // ---- terminal: operator starts milling (op 0020): a first-off inspection opens and blocks reporting
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto(`${TERMINAL}/#enroll=${device}`);
  for (const k of ["1", "1", "1", "1", "OK"]) await page.getByRole("button", { name: k, exact: true }).click();
  const card = page.locator(".t-card", { hasText: order }).filter({ hasText: "0020" }).first();
  await expect(card).toBeVisible();
  await card.click();
  await page.getByRole("button", { name: /Start production/ }).click();
  await expect(page.locator(".t-insp[data-status='OPEN']")).toBeVisible();
  await expect(page.getByRole("button", { name: /Report qty/ })).toBeDisabled();
  await page.screenshot({ animations: "disabled", path: shots("q-t1-first-off-open") });

  // out of tolerance: failed, order on quality hold
  await page.getByRole("button", { name: "Enter results" }).click();
  await measure(page, "119.8");
  await expect(page.getByText(/Inspection Q\d+ failed/)).toBeVisible();
  await expect(page.locator(".t-hold")).toBeVisible({ timeout: 15_000 });
  await page.getByRole("button", { name: /Pause/ }).click();
  await page.getByRole("button", { name: "Quality check" }).click();
  await expect(page.getByRole("button", { name: /Resume/ })).toBeDisabled();
  await page.screenshot({ animations: "disabled", path: shots("q-t2-hold") });

  // supervisor releases the hold; resuming opens the repeat check, which passes
  const sup = await token(request, "supervisor1");
  const wo = await (await request.get(`${CORE}/work-orders/${woId}`, { headers: sup })).json();
  expect(wo.status).toBe("ON_HOLD");
  expect((await request.post(`${CORE}/work-orders/${woId}/resume`, { data: { row_version: wo.row_version, reason: "tool offset corrected" }, headers: sup })).status()).toBe(200);
  await expect(page.locator(".t-hold")).toHaveCount(0, { timeout: 15_000 });
  await page.getByRole("button", { name: /Resume/ }).click();
  await expect(page.locator(".t-insp[data-status='OPEN']")).toBeVisible();
  await page.getByRole("button", { name: "Enter results" }).click();
  await measure(page, "120.02");
  await expect(page.locator(".t-insp[data-status='PASSED']")).toBeVisible({ timeout: 15_000 });

  // complete milling, then deburr (last operation): goods receipt into quality inspection stock with serial numbers
  const complete = async () => {
    await page.getByRole("button", { name: /Complete/ }).click();
    const modal = page.locator(".mantine-Modal-content");
    await modal.locator(".t-display").first().click();
    await modal.getByRole("button", { name: "C", exact: true }).click();
    await keypad(page, "3");
    await modal.getByRole("button", { name: /^Complete$/ }).click();
  };
  await complete();
  const last = page.locator(".t-card", { hasText: order }).filter({ hasText: "0030" }).first();
  await expect(last).toBeVisible();
  await last.click();
  await page.getByRole("button", { name: /Start production/ }).click();
  await expect(page.locator(".t-state[data-state='RUNNING']")).toBeVisible();
  await complete();
  await expect(page.getByText(/3 serial numbers assigned/)).toBeVisible();
  await page.screenshot({ animations: "disabled", path: shots("q-t3-serials") });

  // ---- admin: quality engineer decides the final inspection with an e-signature
  await page.setViewportSize({ width: 1600, height: 950 });
  await signIn(page, "quality1");
  await openModule(page, "Quality");
  await page.getByText("Awaiting usage decision").click();
  const row = page.locator("tbody tr", { hasText: order }).first();
  await expect(row).toBeVisible();
  await page.screenshot({ animations: "disabled", path: shots("q-a1-quality") });
  await row.click();
  const drawer = page.locator(".mantine-Drawer-content");
  await expect(drawer.getByText("Overall length")).toBeVisible();
  await drawer.locator("label", { hasText: /^OK$/ }).click();
  await drawer.locator("input.mantine-NumberInput-input").first().fill("85.02");
  await drawer.getByRole("button", { name: "Save results" }).click();
  await expect(drawer.getByText("PASSED")).toBeVisible();
  await drawer.getByRole("button", { name: /Accept/ }).click();
  await page.screenshot({ animations: "disabled", path: shots("q-a2-signature") });
  await sign(page, "final inspection passed");
  await expect(drawer.getByText("UD accepted")).toBeVisible();
  await expect(drawer.getByText("signed by Quinn Quality")).toBeVisible();
  await page.screenshot({ animations: "disabled", path: shots("q-a3-decided") });
  await page.keyboard.press("Escape");

  // nonconformance of the failed first-off
  await page.getByRole("tab", { name: "Nonconformances" }).click();
  await page.locator("tbody tr", { hasText: order }).first().click();
  await expect(page.locator(".mantine-Drawer-content").getByText(/Inspection Q\d+ failed/)).toBeVisible();
  await page.screenshot({ animations: "disabled", path: shots("q-a4-nc") });
  await page.keyboard.press("Escape");

  // ---- genealogy: order backward (raw material, machines, operators), forward (lot with serials)
  await openModule(page, "Traceability");
  await page.getByPlaceholder("Lot, serial or order number").fill(order);
  await page.locator("button", { hasText: order }).first().click();
  await expect(page.locator(".trace-card.order").first()).toBeVisible();
  await page.screenshot({ animations: "disabled", path: shots("q-a5-trace-backward") });
  await page.locator("label", { hasText: "Forward (where to)" }).click();
  await expect(page.locator(".trace-card.lot").first()).toBeVisible();
  await page.locator(".trace-card.lot").first().getByText("details").click();
  await expect(page.getByText(/FG-1001-\d+/).first()).toBeVisible();
  await page.screenshot({ animations: "disabled", path: shots("q-a6-trace-forward") });
});

test.afterAll(async ({ playwright }) => {
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
