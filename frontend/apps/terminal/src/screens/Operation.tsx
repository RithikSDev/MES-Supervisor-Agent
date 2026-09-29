import { Badge, Group, Modal, Progress, SimpleGrid, Stack, Text } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { type OrderMaterials, type QueueOut, type ReasonCodes, type RunOut, coreRaw } from "@mes/api-client";
import {
  IconAlertTriangle,
  IconArrowLeft,
  IconCheck,
  IconLock,
  IconRuler2,
  IconPackageImport,
  IconPlayerPause,
  IconPlayerPlay,
  IconPlayerStop,
  IconSettings,
  IconTool,
  IconTallymarks,
} from "@tabler/icons-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Elapsed, Keypad, qty } from "../components/ui";
import type { ViewItem } from "../state/optimistic";
import { type ActionKind, type ActionMeta, actionQueue } from "../state/queue";
import { uuid } from "../state/storage";
import { DefectDialog, InspectionDialog } from "./Quality";

type Dialog = null | "report" | "complete" | "pause" | "stop" | "inspect" | "defect" | { issue: string };

const STATE_TEXT: Record<string, string> = { SETUP: "SETUP", RUNNING: "RUNNING", PAUSED: "PAUSED" };

export function Operation({
  item,
  queue,
  reasons,
  onBack,
  onHelp,
  defectSignal = 0,
}: {
  item: ViewItem;
  queue: QueueOut;
  reasons: ReasonCodes | undefined;
  onBack: () => void;
  onHelp?: (equipmentId?: string) => void;
  defectSignal?: number;
}) {
  const qc = useQueryClient();
  const run = item.active_run;
  const [dialog, setDialog] = useState<Dialog>(null);
  const [busy, setBusy] = useState(false);
  // "Get help -> quality problem" in the header opens this operation's defect report
  useEffect(() => {
    if (defectSignal) setDialog("defect");
  }, [defectSignal]);
  const busyMachines = new Set(queue.items.filter((i) => i.active_run && i.operation_id !== item.operation_id).map((i) => i.active_run!.equipment_id));
  const candidates = queue.machines.filter((m) => m.work_center_id === item.work_center_id);
  // preselect the planned machine only if it is free; otherwise the first free one in the work center
  const plannedFree = item.planned_equipment_id && !busyMachines.has(item.planned_equipment_id) ? item.planned_equipment_id : null;
  const [machine, setMachine] = useState<string | null>(
    run?.equipment_id ?? plannedFree ?? candidates.find((m) => !busyMachines.has(m.id))?.id ?? null,
  );

  const materials = useQuery({
    queryKey: ["materials", item.work_order_id],
    queryFn: () => coreRaw<OrderMaterials>(`/work-orders/${item.work_order_id}/materials`),
    refetchInterval: 15_000,
  });

  const act = async (kind: ActionKind, path: string, body: Record<string, unknown>, label: string, meta: Partial<ActionMeta> = {}) => {
    setBusy(true);
    try {
      const r = await actionQueue.submit<RunOut>(kind, path, body, label, {
        workOrderId: item.work_order_id, operationId: item.operation_id, runId: run?.id, ...meta,
      });
      if (r.queued) {
        notifications.show({ color: "amber", title: "Saved on this terminal", message: `${label}: will be sent when the connection is back.` });
      }
      setDialog(null);
      await qc.invalidateQueries({ queryKey: ["queue"] });
      await qc.invalidateQueries({ queryKey: ["materials", item.work_order_id] });
      return r;
    } catch (e) {
      notifications.show({ color: "red", title: "Not accepted", message: e instanceof Error ? e.message : String(e), autoClose: 8000 });
      return null;
    } finally {
      setBusy(false);
    }
  };

  const start = (phase: "SETUP" | "RUN") => {
    const runId = uuid();
    return act("start", `/execution/work-orders/${item.work_order_id}/operations/${item.operation_id}/runs`,
      { equipment_id: machine, phase, run_id: runId }, phase === "SETUP" ? "Start setup" : "Start production",
      { runId, equipmentId: machine ?? undefined, phase });
  };
  const runPath = (verb: string) => `/execution/runs/${run!.id}/${verb}`;
  const planned = Number(item.planned_qty);
  const done = Number(item.yield_qty);
  const remaining = Math.max(planned - done - Number(item.scrap_qty), 0);
  const state = run?.status;
  const onHold = item.wo_status === "ON_HOLD";
  const runMachine = run ? queue.machines.find((m) => m.id === run.equipment_id) : undefined;
  const machineDown = runMachine && runMachine.status !== "AVAILABLE";
  const insp = run?.inspection;
  const gated = Boolean(insp && insp.blocking && insp.status !== "PASSED");
  // the hold was released: open the next first-off inspection lot for this operation
  const repeatCheck = async () => {
    setBusy(true);
    try {
      await coreRaw(`/quality/work-orders/${item.work_order_id}/operations/${item.operation_id}/inspection`, { method: "POST" });
      await qc.invalidateQueries({ queryKey: ["queue"] });
      setDialog("inspect");
    } catch (e) {
      notifications.show({ color: "red", title: "Not accepted", message: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };
  const report = async (good: number, scrap: number, reason: string | null) => {
    const final = dialog === "complete";
    const r = await act("report", runPath("report"), { yield_qty: String(good), scrap_qty: String(scrap), scrap_reason: reason, final },
      final ? `Complete ${qty(good)} ${item.uom}` : `Report ${qty(good)} ${item.uom}`, { yieldQty: good, scrapQty: scrap, final });
    const serials = r?.result?.serials ?? [];
    if (item.is_last_operation && good > 0 && serials.length) {
      const fresh = serials.slice(-Math.min(good, serials.length));
      notifications.show({ color: "green", autoClose: 15000, title: `${fresh.length} serial number${fresh.length > 1 ? "s" : ""} assigned`,
        message: fresh.length > 6 ? `${fresh[0]} … ${fresh[fresh.length - 1]}` : fresh.join(", ") });
    }
  };

  return (
    <Stack gap="md">
      <Group justify="space-between" wrap="nowrap">
        <button className="t-btn ghost small" style={{ width: 150 }} onClick={onBack}><IconArrowLeft /> Back</button>
        <div style={{ flex: 1 }}>
          <Group gap="md" align="baseline">
            <span className="t-big" style={{ fontSize: 40 }}>{item.erp_order_number}</span>
            <span className="t-big" style={{ fontSize: 30, color: "var(--mes-text-dim)" }}>/ {item.op_no}</span>
            <span className="t-title" style={{ fontSize: 30 }}>{item.op_description}</span>
          </Group>
          <Text size="lg" c="dimmed"><span className="mono">{item.material_number}</span> {item.material_description}</Text>
        </div>
      </Group>

      <div style={{ display: "grid", gridTemplateColumns: "1.35fr 1fr", gap: 18 }}>
        <Stack gap="md">
          <div className="t-state" data-state={state ?? "IDLE"}>
            <div>
              <div className="t-title" style={{ fontSize: 40, color: state ? undefined : "var(--mes-text-dim)" }}>
                {state ? STATE_TEXT[state] : "NOT STARTED"}
                {run?.pending && <span className="t-blink" style={{ fontSize: 18, marginLeft: 12, color: "var(--mes-amber)" }}>not yet synced</span>}
              </div>
              {state === "PAUSED" && (
                <Text size="xl" c="amber.3">{reasons?.pause.find((r) => r.code === run?.pause_reason)?.label ?? run?.pause_reason}</Text>
              )}
              {run && !run.pending && <Text size="md" c="dimmed">operator {run.operator_name}</Text>}
            </div>
            {run && <div className="t-big" style={{ fontSize: 56 }}><Elapsed since={run.phase_started_at} /></div>}
          </div>

          {machineDown && (
            <div className="t-hold">
              <IconTool size={34} />
              <div style={{ flex: 1 }}>
                <div className="t-title" style={{ fontSize: 28 }}>{runMachine!.code} {runMachine!.status === "DOWN" ? "IS DOWN" : "IS IN MAINTENANCE"}</div>
                <Text size="lg">Maintenance has been called. Production resumes once the machine is released.</Text>
              </div>
              {runMachine!.status === "DOWN" && (
                <button className="t-btn small" style={{ width: 230 }} disabled={busy}
                  onClick={() => act("back-up", `/maintenance/equipment/${runMachine!.id}/back-up`, { note: "cleared by the operator" }, `${runMachine!.code} back up`)}>
                  Machine back up
                </button>
              )}
            </div>
          )}
          {onHold && (
            <div className="t-hold">
              <IconLock size={34} />
              <div>
                <div className="t-title" style={{ fontSize: 28 }}>ORDER ON QUALITY HOLD</div>
                <Text size="lg">Stop and wait: a supervisor must release the order before work continues.</Text>
              </div>
            </div>
          )}
          {insp && (
            <div className="t-insp" data-status={insp.status}>
              <IconRuler2 size={34} />
              <div style={{ flex: 1 }}>
                <div className="t-title" style={{ fontSize: 26 }}>
                  First-off inspection {insp.number}: {insp.status === "OPEN" ? "measure now" : insp.status.toLowerCase()}
                </div>
                <Text size="md" c="dimmed">
                  {insp.status === "OPEN" ? (insp.blocking ? "Quantities can be reported once it has passed." : "Informational check.")
                    : insp.status === "FAILED" ? "Out of tolerance: a nonconformance was opened." : "Passed: production released."}
                </Text>
              </div>
              {insp.status === "OPEN" && !onHold && (
                <button className="t-btn small primary" style={{ width: 230 }} onClick={() => setDialog("inspect")}>Enter results</button>
              )}
              {insp.status === "FAILED" && !onHold && (
                <button className="t-btn small" style={{ width: 230 }} disabled={busy} onClick={repeatCheck}>Repeat check</button>
              )}
            </div>
          )}

          <div className="panel" style={{ padding: 14 }}>
            <Group justify="space-between" mb={6}>
              <Text className="label" style={{ fontSize: 14 }}>Good / planned</Text>
              <Text className="t-big" size="xl">{qty(done)} / {qty(planned)} {item.uom}</Text>
            </Group>
            <Progress value={(done / planned) * 100} size={16} color="green" bg="dark.5" />
            {Number(item.scrap_qty) > 0 && <Text c="red.4" mt={6} size="lg" className="mono">scrap {qty(item.scrap_qty)}</Text>}
          </div>

          {!run ? (
            <>
              <Text className="label" style={{ fontSize: 14 }}>Machine</Text>
              <SimpleGrid cols={Math.min(candidates.length, 3)} spacing={10}>
                {candidates.map((m) => (
                  <button key={m.id} className={`t-choice${machine === m.id ? " selected" : ""}`} disabled={busyMachines.has(m.id)}
                    onClick={() => setMachine(m.id)} style={busyMachines.has(m.id) ? { opacity: 0.4 } : undefined}>
                    <div className="t-big" style={{ fontSize: 24 }}>{m.code}</div>
                    <Text size="sm" c="dimmed">{busyMachines.has(m.id) ? "busy" : m.id === item.planned_equipment_id ? "planned" : m.name}</Text>
                  </button>
                ))}
              </SimpleGrid>
              {item.previous_op_status && item.previous_op_status !== "CONFIRMED" && (
                <Text c="amber.4" size="lg">The previous operation is not finished yet.</Text>
              )}
              <SimpleGrid cols={2} spacing={12}>
                <button className="t-btn" disabled={!machine || busy} onClick={() => start("SETUP")}><IconSettings size={30} /> Start setup</button>
                <button className="t-btn primary" disabled={!machine || busy} onClick={() => start("RUN")}><IconPlayerPlay size={30} /> Start production</button>
              </SimpleGrid>
            </>
          ) : (
            <SimpleGrid cols={2} spacing={12}>
              {state === "SETUP" && (
                <button className="t-btn primary" disabled={busy || onHold} onClick={() => act("start-production", runPath("start-production"), {}, "Start production")}>
                  <IconPlayerPlay size={30} /> Start production
                </button>
              )}
              {state === "PAUSED" ? (
                <button className="t-btn primary" disabled={busy || onHold || Boolean(machineDown)} onClick={() => act("resume", runPath("resume"), {}, "Resume")}>
                  <IconPlayerPlay size={30} /> Resume
                </button>
              ) : (
                <button className="t-btn warn" disabled={busy} onClick={() => setDialog("pause")}><IconPlayerPause size={30} /> Pause</button>
              )}
              {state === "RUNNING" && (
                <>
                  <button className="t-btn" disabled={busy || gated} onClick={() => setDialog("report")}><IconTallymarks size={30} /> Report qty</button>
                  <button className="t-btn primary" disabled={busy || gated} onClick={() => setDialog("complete")}><IconCheck size={30} /> Complete</button>
                </>
              )}
              <button className="t-btn ghost" disabled={busy} onClick={() => setDialog("stop")}><IconPlayerStop size={30} /> Stop</button>
            </SimpleGrid>
          )}
        </Stack>

        <Stack gap="sm">
          <Text className="label" style={{ fontSize: 14 }}>Material</Text>
          {(materials.data?.components ?? []).filter((c) => !c.operation_id || c.operation_id === item.operation_id).map((c) => {
            const req = Number(c.required_qty);
            const iss = Number(c.issued_qty);
            return (
              <div key={c.component_id} className="panel" style={{ padding: 12 }}>
                <Group justify="space-between" wrap="nowrap">
                  <div>
                    <Text size="lg" className="mono">{c.material_number}</Text>
                    <Text size="sm" c="dimmed">{c.material_description}</Text>
                  </div>
                  {c.backflush ? (
                    <Badge size="lg" variant="outline" color="dark.2">auto (backflush)</Badge>
                  ) : (
                    <button className="t-btn small" style={{ width: 130 }} disabled={busy || c.available.length === 0} onClick={() => setDialog({ issue: c.component_id })}>
                      <IconPackageImport /> Issue
                    </button>
                  )}
                </Group>
                <Group gap="sm" mt={6} wrap="nowrap">
                  <Progress value={Math.min(iss / req, 1) * 100} size={8} style={{ flex: 1 }} color={iss >= req ? "green" : "blue"} bg="dark.5" />
                  <Text size="md" className="mono">{qty(iss)} / {qty(req)} {c.uom}</Text>
                </Group>
                {!c.backflush && c.available.length === 0 && iss < req && <Text c="red.4" size="sm" mt={4}>No stock available: call material handling.</Text>}
              </div>
            );
          })}
          {materials.isError && <Text c="dimmed">Material status unavailable offline.</Text>}
          <button className="t-btn warn small" style={{ marginTop: 8 }} disabled={busy} onClick={() => setDialog("defect")}>
            <IconAlertTriangle /> Report defect
          </button>
          {run && (run.serials ?? []).length > 0 && (
            <div className="panel" style={{ padding: 12 }}>
              <Text className="label" style={{ fontSize: 14 }}>Serial numbers (latest)</Text>
              <Text className="mono" size="md">{(run.serials ?? []).slice(-6).join("  ")}</Text>
            </div>
          )}
        </Stack>
      </div>

      <QtyDialog
        opened={dialog === "report" || dialog === "complete"}
        final={dialog === "complete"}
        defaultGood={dialog === "complete" ? remaining : 0}
        uom={item.uom}
        scrapReasons={reasons?.scrap ?? []}
        busy={busy}
        onClose={() => setDialog(null)}
        onConfirm={report}
      />
      {dialog === "inspect" && insp && (
        <InspectionDialog id={insp.id} onClose={() => setDialog(null)} onDone={async (d) => {
          setDialog(null);
          notifications.show(d.status === "FAILED"
            ? { color: "red", autoClose: 12000, title: `Inspection ${d.number} failed`, message: "The order is on quality hold. Pause the machine (quality check) and call your supervisor." }
            : { color: d.status === "PASSED" ? "green" : "cyan", title: `Inspection ${d.number}: ${d.status.toLowerCase()}`, message: d.status === "PASSED" ? "You can report quantities now." : "Results saved." });
          await qc.invalidateQueries({ queryKey: ["queue"] });
        }} />
      )}
      {dialog === "defect" && (
        <DefectDialog uom={item.uom} busy={busy} onClose={() => setDialog(null)}
          onConfirm={(code, label, severity, q) => {
            act("defect", "/quality/nonconformances", {
              work_order_id: item.work_order_id, operation_id: item.operation_id, defect_code: code, severity,
              description: `${label} (reported at the terminal, op ${item.op_no})`, quantity: String(q),
            }, `Defect: ${label}`).then((r) => {
              if (r && !r.queued) notifications.show({ color: severity === "CRITICAL" ? "red" : "amber", title: "Defect reported",
                message: severity === "CRITICAL" ? "The order is on quality hold." : "Quality has been notified." });
            });
          }} />
      )}
      <Modal opened={dialog === "pause"} onClose={() => setDialog(null)} size={760} centered title={<span className="t-title" style={{ fontSize: 30 }}>Why is the machine stopping?</span>}>
        <SimpleGrid cols={2} spacing={12}>
          {(reasons?.pause ?? []).map((r) => (
            <button key={r.code} className="t-choice" disabled={busy} onClick={async () => {
              await act("pause", runPath("pause"), { reason_code: r.code }, `Pause: ${r.label}`, { reason: r.code });
              if (r.code === "MACHINE_ISSUE" && run) onHelp?.(run.equipment_id);  // straight on to "call maintenance"
            }}>
              {r.label}
            </button>
          ))}
        </SimpleGrid>
      </Modal>
      <Modal opened={dialog === "stop"} onClose={() => setDialog(null)} size={620} centered title={<span className="t-title" style={{ fontSize: 30 }}>Stop without completing?</span>}>
        <Stack>
          <Text size="xl">The run ends (for example at shift end). The operation stays open for the next shift. Report good parts first.</Text>
          <SimpleGrid cols={2}>
            <button className="t-btn ghost" onClick={() => setDialog(null)}>Cancel</button>
            <button className="t-btn danger" disabled={busy} onClick={() => act("stop", runPath("stop"), { note: "stopped at terminal" }, "Stop run")}>Stop run</button>
          </SimpleGrid>
        </Stack>
      </Modal>
      {typeof dialog === "object" && dialog && materials.data && (
        <IssueDialog
          materials={materials.data}
          componentId={dialog.issue}
          busy={busy}
          onClose={() => setDialog(null)}
          onConfirm={(lotId, locationId, q) =>
            act("issue", `/work-orders/${item.work_order_id}/components/${dialog.issue}/issues`,
              { lot_id: lotId, from_location_id: locationId, quantity: String(q) }, `Issue ${qty(q)}`)
          }
        />
      )}
    </Stack>
  );
}

function QtyDialog({ opened, final, defaultGood, uom, scrapReasons, busy, onClose, onConfirm }: {
  opened: boolean;
  final: boolean;
  defaultGood: number;
  uom: string;
  scrapReasons: { code: string; label: string }[];
  busy: boolean;
  onClose: () => void;
  onConfirm: (good: number, scrap: number, reason: string | null) => void;
}) {
  const [good, setGood] = useState("");
  const [scrap, setScrap] = useState("");
  const [field, setField] = useState<"good" | "scrap">("good");
  const [reason, setReason] = useState<string | null>(null);
  const [wasOpen, setWasOpen] = useState(false);
  if (opened !== wasOpen) {
    setWasOpen(opened);
    if (opened) {
      setGood(defaultGood ? String(defaultGood) : "");
      setScrap("");
      setField("good");
      setReason(null);
    }
  }
  const g = Number(good || 0);
  const sc = Number(scrap || 0);
  const valid = (g > 0 || sc > 0 || final) && (sc === 0 || reason !== null);
  return (
    <Modal opened={opened} onClose={onClose} size={980} centered
      title={<span className="t-title" style={{ fontSize: 30 }}>{final ? "Complete operation" : "Report quantity"}</span>}>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 360px", gap: 20 }}>
        <Stack gap="sm">
          <Text className="label" style={{ fontSize: 14 }}>Good ({uom})</Text>
          <div className={`t-display${field === "good" ? " focused" : ""}`} onClick={() => setField("good")}>{good || "0"}</div>
          <Text className="label" style={{ fontSize: 14 }}>Scrap ({uom})</Text>
          <div className={`t-display${field === "scrap" ? " focused" : ""}`} onClick={() => setField("scrap")} style={{ color: sc ? "var(--mes-red)" : undefined }}>
            {scrap || "0"}
          </div>
          {sc > 0 && (
            <>
              <Text className="label" style={{ fontSize: 14 }}>Scrap reason</Text>
              <SimpleGrid cols={2} spacing={8}>
                {scrapReasons.map((r) => (
                  <button key={r.code} className={`t-choice${reason === r.code ? " selected" : ""}`} onClick={() => setReason(r.code)} style={{ minHeight: 56, fontSize: 17 }}>
                    {r.label}
                  </button>
                ))}
              </SimpleGrid>
            </>
          )}
          {final && <Text c="dimmed" size="md">Completing finishes this operation: no more quantities can be reported on it.</Text>}
        </Stack>
        <Stack gap="sm">
          <Keypad value={field === "good" ? good : scrap} onChange={field === "good" ? setGood : setScrap} />
          <button className={`t-btn ${final ? "primary" : ""}`} disabled={!valid || busy} onClick={() => onConfirm(g, sc, sc > 0 ? reason : null)}>
            <IconCheck size={30} /> {final ? "Complete" : "Report"}
          </button>
        </Stack>
      </div>
    </Modal>
  );
}

function IssueDialog({ materials, componentId, busy, onClose, onConfirm }: {
  materials: OrderMaterials;
  componentId: string;
  busy: boolean;
  onClose: () => void;
  onConfirm: (lotId: string, locationId: string, qty: number) => void;
}) {
  const comp = materials.components.find((c) => c.component_id === componentId)!;
  const [lotKey, setLotKey] = useState(comp.available[0] ? `${comp.available[0].lot_id}|${comp.available[0].location_id}` : "");
  const lot = comp.available.find((a) => `${a.lot_id}|${a.location_id}` === lotKey);
  const suggested = Math.min(Number(comp.remaining_qty), Number(lot?.quantity ?? 0));
  const [amount, setAmount] = useState(suggested ? String(suggested) : "");
  const n = Number(amount || 0);
  return (
    <Modal opened onClose={onClose} size={980} centered
      title={<span className="t-title" style={{ fontSize: 30 }}>Issue {comp.material_number} · still needed {qty(comp.remaining_qty)} {comp.uom}</span>}>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 360px", gap: 20 }}>
        <Stack gap={8}>
          <Text className="label" style={{ fontSize: 14 }}>Lot (first in, first out)</Text>
          {comp.available.map((a) => {
            const key = `${a.lot_id}|${a.location_id}`;
            return (
              <button key={key} className={`t-choice${lotKey === key ? " selected" : ""}`} onClick={() => setLotKey(key)}>
                <Group justify="space-between">
                  <span className="t-big" style={{ fontSize: 22 }}>{a.lot_number}</span>
                  <span className="mono">{qty(a.quantity)} {comp.uom}</span>
                </Group>
                <Text size="sm" c="dimmed">location {a.location_code} · {a.location_kind.toLowerCase()}</Text>
              </button>
            );
          })}
        </Stack>
        <Stack gap="sm">
          <div className="t-display focused">{amount || "0"}</div>
          <Keypad value={amount} onChange={setAmount} decimals />
          <button className="t-btn primary" disabled={!lot || n <= 0 || n > Number(lot.quantity) || busy} onClick={() => lot && onConfirm(lot.lot_id, lot.location_id, n)}>
            <IconPackageImport size={30} /> Issue {qty(n)} {comp.uom}
          </button>
        </Stack>
      </div>
    </Modal>
  );
}
