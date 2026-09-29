/**
 * Apply queued (not yet confirmed by the server) actions to the last known work queue, so the operator sees
 * the effect of what they did even while offline. The server state replaces this as soon as the queue drains.
 */
import type { QueueItem, RunOut } from "@mes/api-client";
import type { QueuedAction } from "./queue";

export type ViewRun = RunOut & { pending?: boolean };
export type ViewItem = Omit<QueueItem, "active_run"> & { active_run: ViewRun | null; pendingActions: number };

function blankRun(item: QueueItem, a: QueuedAction): ViewRun {
  return {
    id: a.meta.runId ?? a.id,
    plant_id: "",
    work_order_id: item.work_order_id,
    erp_order_number: item.erp_order_number,
    material_number: item.material_number,
    material_description: item.material_description,
    operation_id: item.operation_id,
    op_no: item.op_no,
    op_description: item.op_description,
    op_status: "IN_PROGRESS",
    op_planned_qty: item.planned_qty,
    op_yield_qty: item.yield_qty,
    op_scrap_qty: item.scrap_qty,
    uom: item.uom,
    is_last_operation: item.is_last_operation,
    equipment_id: a.meta.equipmentId ?? "",
    equipment_code: null,
    line_id: null,
    terminal_id: null,
    operator_id: "",
    operator_name: "",
    status: a.meta.phase === "SETUP" ? "SETUP" : "RUNNING",
    paused_from: null,
    pause_reason: null,
    started_at: a.createdAt,
    phase_started_at: a.createdAt,
    ended_at: null,
    setup_s: 0,
    run_s: 0,
    paused_s: 0,
    yield_qty: "0",
    scrap_qty: "0",
    replayed: false,
    wo_status: item.wo_status,
    inspection: null,
    serials: [],
    pending: true,
  };
}

const add = (a: string | number, b: number | undefined) => String(Number(a) + (b ?? 0));

export function applyPending(items: QueueItem[], queue: QueuedAction[]): ViewItem[] {
  const view: ViewItem[] = items.map((i) => ({ ...i, active_run: i.active_run ? { ...i.active_run } : null, pendingActions: 0 }));
  for (const a of queue) {
    if (a.status !== "pending" || !a.meta.operationId) continue;
    const item = view.find((v) => v.operation_id === a.meta.operationId);
    if (!item) continue;
    item.pendingActions += 1;
    const run = item.active_run;
    switch (a.kind) {
      case "start":
        item.active_run = blankRun(item, a);
        item.op_status = "IN_PROGRESS";
        break;
      case "start-production":
        if (run) Object.assign(run, { status: "RUNNING", phase_started_at: a.createdAt, pending: true });
        break;
      case "pause":
        if (run) Object.assign(run, { paused_from: run.status, status: "PAUSED", pause_reason: a.meta.reason, phase_started_at: a.createdAt, pending: true });
        break;
      case "resume":
        if (run) Object.assign(run, { status: run.paused_from ?? "RUNNING", pause_reason: null, phase_started_at: a.createdAt, pending: true });
        break;
      case "report":
        item.yield_qty = add(item.yield_qty, a.meta.yieldQty);
        item.scrap_qty = add(item.scrap_qty, a.meta.scrapQty);
        if (run) {
          run.op_yield_qty = item.yield_qty;
          run.op_scrap_qty = item.scrap_qty;
          run.pending = true;
        }
        if (a.meta.final) {
          item.active_run = null;
          item.op_status = "CONFIRMED";
        }
        break;
      case "stop":
        item.active_run = null;
        break;
    }
  }
  return view.filter((v) => v.op_status !== "CONFIRMED");
}
