/**
 * Technician mode (PIN of a maintenance technician): the line's maintenance work. Open notifications become
 * orders; an order is started (machine -> MAINTENANCE), worked through its checklist with spare parts and labour,
 * and signed off, which releases the machine to production. Needs a connection (no offline queue here).
 */
import { Badge, Group, SimpleGrid, Stack, Text, TextInput } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { coreRaw, type Schemas } from "@mes/api-client";
import { IconArrowLeft, IconCheck, IconPackageImport, IconPlayerPlay, IconSignature, IconTool } from "@tabler/icons-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import dayjs from "dayjs";
import { useState } from "react";
import { Elapsed, Keypad, qty } from "../components/ui";

type Order = Schemas["MaintOrderDetail"];
type OrderRow = Schemas["MaintOrderOut"];
type Notif = Schemas["NotificationOut"];
type Part = Schemas["SparePart"];

export function MaintenanceScreen({ plantId, lineCode, lineId }: { plantId: string; lineCode: string; lineId: string }) {
  const [openId, setOpenId] = useState<string | null>(null);
  const qc = useQueryClient();
  const orders = useQuery({
    queryKey: ["m-orders", lineId],
    queryFn: () => coreRaw<OrderRow[]>(`/maintenance/orders?plant_id=${plantId}&line_id=${lineId}&status=CREATED&status=IN_PROGRESS`),
    refetchInterval: 5000,
  });
  const notifs = useQuery({
    queryKey: ["m-notifs"],
    queryFn: () => coreRaw<Notif[]>(`/maintenance/notifications?plant_id=${plantId}&status=OPEN`),
    refetchInterval: 5000,
  });
  const createOrder = async (n: Notif) => {
    try {
      const mo = await coreRaw<Order>(`/maintenance/notifications/${n.id}/order`, { method: "POST", body: "{}" });
      await qc.invalidateQueries({ queryKey: ["m-notifs"] });
      await qc.invalidateQueries({ queryKey: ["m-orders", lineId] });
      setOpenId(mo.id);
    } catch (e) {
      notifications.show({ color: "red", title: "Not accepted", message: e instanceof Error ? e.message : String(e) });
    }
  };
  if (openId) return <OrderScreen id={openId} plantId={plantId} onBack={() => setOpenId(null)} />;
  const mine = (notifs.data ?? []).filter((n) => n.line_code === lineCode);
  return (
    <Stack gap="lg">
      <div className="t-title" style={{ fontSize: 28 }}>Maintenance · line {lineCode}</div>
      {mine.length > 0 && (
        <Stack gap={10}>
          <Text className="label" style={{ fontSize: 14 }}>Reported problems without an order</Text>
          {mine.map((n) => (
            <div key={n.id} className="t-card" data-state={n.machine_down ? "DOWN" : undefined}>
              <div>
                <Group gap="md" align="baseline">
                  <span className="t-big" style={{ fontSize: 26 }}>{n.equipment_code}</span>
                  <Text size="xl" fw={500}>{n.description}</Text>
                </Group>
                <Text c="dimmed">notification {n.number} · {n.reported_by_name} · {dayjs(n.reported_at).format("HH:mm")} · priority {n.priority}</Text>
              </div>
              <button className="t-btn warn small" style={{ width: 220 }} onClick={() => createOrder(n)}><IconTool /> Take it</button>
            </div>
          ))}
        </Stack>
      )}
      <Text className="label" style={{ fontSize: 14 }}>Maintenance orders</Text>
      <Stack gap={10}>
        {(orders.data ?? []).map((o) => (
          <div key={o.id} className="t-card" data-state={o.status === "IN_PROGRESS" ? "SETUP" : undefined} role="button" onClick={() => setOpenId(o.id)}>
            <div>
              <Group gap="md" align="baseline">
                <span className="t-big" style={{ fontSize: 26 }}>{o.equipment_code}</span>
                <Text size="xl" fw={500}>{o.description}</Text>
              </Group>
              <Text c="dimmed">order {o.number} · {o.order_type === "PREVENTIVE" ? "preventive" : "breakdown repair"} · planned {dayjs(o.planned_start).format("ddd HH:mm")} · {o.planned_duration_min} min</Text>
            </div>
            <Stack gap={2} align="flex-end">
              <Text className="t-title" style={{ fontSize: 22, color: o.status === "IN_PROGRESS" ? "var(--mes-cyan)" : o.overdue ? "var(--mes-red)" : undefined }}>
                {o.status === "IN_PROGRESS" ? "IN WORK" : o.overdue ? "OVERDUE" : "OPEN"}
              </Text>
              {o.technician_name && <Text c="dimmed">{o.technician_name}</Text>}
            </Stack>
          </div>
        ))}
        {orders.data?.length === 0 && <Text size="xl" c="dimmed" ta="center" py={40}>No open maintenance orders on this line.</Text>}
      </Stack>
    </Stack>
  );
}

function OrderScreen({ id, plantId, onBack }: { id: string; plantId: string; onBack: () => void }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["m-order", id], queryFn: () => coreRaw<Order>(`/maintenance/orders/${id}`) });
  const parts = useQuery({ queryKey: ["spare-parts"], queryFn: () => coreRaw<Part[]>(`/maintenance/spare-parts?plant_id=${plantId}`) });
  const [busy, setBusy] = useState(false);
  const [part, setPart] = useState<string | null>(null);
  const [amount, setAmount] = useState("1");
  const [findings, setFindings] = useState("");
  const [signing, setSigning] = useState(false);
  const o = q.data;
  const call = async (path: string, method: string, body: unknown, ok?: string) => {
    setBusy(true);
    try {
      const r = await coreRaw<Order>(path, { method, body: JSON.stringify(body) });
      qc.setQueryData(["m-order", id], r);
      if (ok) notifications.show({ color: "green", title: ok, message: `${r.equipment_code} · order ${r.number}` });
      return r;
    } catch (e) {
      notifications.show({ color: "red", title: "Not accepted", message: e instanceof Error ? e.message : String(e), autoClose: 8000 });
      return null;
    } finally {
      setBusy(false);
    }
  };
  if (!o) return <Text size="xl">Loading…</Text>;
  const working = o.status === "IN_PROGRESS";
  const allDone = o.checklist.every((c) => c.done);
  const toggle = (i: number) => call(`/maintenance/orders/${o.id}/work`, "PUT",
    { checklist: o.checklist.map((c, j) => (j === i ? { ...c, done: !c.done } : c)) });

  return (
    <Stack gap="md">
      <Group justify="space-between" wrap="nowrap">
        <button className="t-btn ghost small" style={{ width: 150 }} onClick={onBack}><IconArrowLeft /> Back</button>
        <div style={{ flex: 1 }}>
          <Group gap="md" align="baseline">
            <span className="t-big" style={{ fontSize: 38 }}>{o.equipment_code}</span>
            <span className="t-title" style={{ fontSize: 28 }}>{o.description}</span>
          </Group>
          <Text c="dimmed" size="lg">order {o.number}{o.notification_number ? ` · notification ${o.notification_number}` : ""} · {o.order_type.toLowerCase()} · SAP {o.erp_sync_status === "POSTED" ? o.erp_system_status ?? "posted" : o.erp_sync_status.toLowerCase()}</Text>
        </div>
      </Group>
      <div style={{ display: "grid", gridTemplateColumns: "1.3fr 1fr", gap: 18 }}>
        <Stack gap="sm">
          <div className="t-state" data-state={working ? "SETUP" : o.status === "COMPLETED" ? "RUNNING" : "IDLE"}>
            <div className="t-title" style={{ fontSize: 34 }}>{working ? "MAINTENANCE IN PROGRESS" : o.status === "COMPLETED" ? "COMPLETED · MACHINE RELEASED" : "NOT STARTED"}</div>
            {working && o.started_at && <div className="t-big" style={{ fontSize: 44 }}><Elapsed since={o.started_at} /></div>}
          </div>
          {o.status === "CREATED" && (
            <button className="t-btn primary" disabled={busy} onClick={() => call(`/maintenance/orders/${o.id}/start`, "POST", {}, "Maintenance started")}>
              <IconPlayerPlay size={30} /> Start work (machine blocked for production)
            </button>
          )}
          <Text className="label" style={{ fontSize: 14 }}>Checklist</Text>
          {o.checklist.map((c, i) => (
            <button key={i} className={`t-choice${c.done ? " selected" : ""}`} disabled={!working || busy} onClick={() => toggle(i)}>
              <Group gap="sm" wrap="nowrap">{c.done ? <IconCheck color="var(--mes-green)" /> : <span style={{ width: 24 }} />}<span>{c.text}</span></Group>
            </button>
          ))}
          {working && (
            <>
              <TextInput size="lg" label="What did you find and do?" value={findings} onChange={(e) => setFindings(e.currentTarget.value)}
                placeholder="e.g. Drive fuse blown, replaced; spindle tested" />
              {!signing ? (
                <button className="t-btn primary" disabled={!allDone || !findings.trim() || busy} onClick={() => setSigning(true)}>
                  <IconSignature size={30} /> Sign off and release machine
                </button>
              ) : (
                <div className="t-sign">
                  <Text size="lg">Electronic signature · <b>RELEASED</b> · {dayjs().format("DD MMM HH:mm")}</Text>
                  <Text c="dimmed">I confirm the work is done and {o.equipment_code} is safe to run.</Text>
                  <SimpleGrid cols={2} mt="sm">
                    <button className="t-btn ghost small" onClick={() => setSigning(false)}>Cancel</button>
                    <button className="t-btn primary small" disabled={busy}
                      onClick={async () => { if (await call(`/maintenance/orders/${o.id}/complete`, "POST",
                        { findings, reason: "Work completed, machine tested and safe to run" }, "Machine released to production")) setSigning(false); }}>
                      <IconSignature /> Sign
                    </button>
                  </SimpleGrid>
                </div>
              )}
              {!allDone && <Text c="amber.4">Tick every checklist item to sign off.</Text>}
            </>
          )}
        </Stack>
        <Stack gap="sm">
          <Text className="label" style={{ fontSize: 14 }}>Spare parts used</Text>
          {o.parts.length === 0 && <Text c="dimmed">None yet.</Text>}
          {o.parts.map((p) => (
            <Group key={p.movement_id} justify="space-between"><Text size="lg" className="mono">{p.material_number}</Text>
              <Group gap={6}><Text size="lg" className="mono">{qty(p.quantity)} {p.uom}</Text>
                <Badge color={p.erp_sync_status === "POSTED" ? "green" : "amber"} variant="light">{p.erp_sync_status === "POSTED" ? "in SAP" : "sending"}</Badge></Group></Group>
          ))}
          {working && (
            <>
              <SimpleGrid cols={2} spacing={8}>
                {(parts.data ?? []).map((p) => (
                  <button key={p.material_id} className={`t-choice${part === p.material_id ? " selected" : ""}`} onClick={() => setPart(p.material_id)} style={{ minHeight: 60, fontSize: 16 }}>
                    <div className="mono">{p.material_number}</div><Text size="xs" c="dimmed">{p.description} · {qty(p.in_stores)} in stores</Text>
                  </button>
                ))}
              </SimpleGrid>
              <div className="t-display focused">{amount || "0"}</div>
              <Keypad value={amount} onChange={setAmount} />
              <button className="t-btn small" disabled={!part || !Number(amount) || busy}
                onClick={async () => { if (await call(`/maintenance/orders/${o.id}/parts`, "POST", { material_id: part, quantity: amount }, "Spare part booked")) { setPart(null); setAmount("1"); } }}>
                <IconPackageImport /> Book spare part
              </button>
            </>
          )}
        </Stack>
      </div>
    </Stack>
  );
}
