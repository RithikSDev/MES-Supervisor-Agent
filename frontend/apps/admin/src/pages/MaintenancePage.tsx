import {
  ActionIcon, Badge, Button, Checkbox, Drawer, Group, Loader, Modal, NumberInput, SegmentedControl, Select, SimpleGrid, Stack,
  Switch, Table, Tabs, Text, TextInput, Textarea, UnstyledButton,
} from "@mantine/core";
import {
  ApiError, type Downtime, type EquipmentHealth, type MaintNotification, type MaintOrder, type MaintOrderDetail,
  type MaintPlan, type MaintPlanIn, core, unwrap,
} from "@mes/api-client";
import {
  IconAlertTriangle, IconArrowsSort, IconBell, IconChevronDown, IconChevronUp, IconClockExclamation, IconPencil,
  IconPlayerPlay, IconPlus, IconSearch, IconTool, IconTrash, IconX, type Icon,
} from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router";
import { useCan } from "../auth";
import { ReasonModal, SignatureModal } from "../components/dialogs";
import { PageHeader } from "../components/Layout";
import { usePlant } from "../components/plant";
import { fmtDateTime, fromNow, minutes, num } from "../lib/format";

/** Generic sortable column header, shared by the notifications/orders/downtime tables below. */
function SortTh<K extends string>({
  id, sort, onSort, align, children,
}: { id: K; sort: { key: K; dir: 1 | -1 }; onSort: (k: K) => void; align?: "right"; children: React.ReactNode }) {
  const active = sort.key === id;
  return (
    <Table.Th style={align === "right" ? { textAlign: "right" } : undefined}>
      <UnstyledButton onClick={() => onSort(id)} className="sortable-th">
        <Group gap={4} wrap="nowrap" justify={align === "right" ? "flex-end" : "flex-start"}>
          <span>{children}</span>
          {active ? (sort.dir === 1 ? <IconChevronUp size={12} /> : <IconChevronDown size={12} />) : <IconArrowsSort size={12} opacity={0.35} />}
        </Group>
      </UnstyledButton>
    </Table.Th>
  );
}
function toggleSort<K extends string>(setSort: (fn: (s: { key: K; dir: 1 | -1 }) => { key: K; dir: 1 | -1 }) => void, key: K) {
  setSort((s) => (s.key === key ? { key, dir: s.dir === 1 ? -1 : 1 } : { key, dir: 1 }));
}
function sortBy<T, K extends string>(rows: T[], sort: { key: K; dir: 1 | -1 }, valueOf: Record<K, (r: T) => number | string>): T[] {
  const val = valueOf[sort.key];
  return [...rows].sort((a, b) => {
    const va = val(a);
    const vb = val(b);
    const cmp = typeof va === "number" && typeof vb === "number" ? va - vb : String(va).localeCompare(String(vb));
    return cmp * sort.dir;
  });
}

/** Clickable KPI tile: icon + colour-accented top border + hover lift (same look as Work Orders' bucket tiles). */
function MaintKpi({
  icon: Icon, label, value, color, hot, onClick,
}: { icon: Icon; label: string; value: number | string; color: string; hot?: boolean; onClick: () => void }) {
  return (
    <div className="panel kpi wo-kpi" style={{ borderTop: `3px solid ${color}` }} onClick={onClick} role="button" tabIndex={0}
      onKeyDown={(e) => e.key === "Enter" && onClick()}>
      <div className="wo-kpi-top"><span className="wo-kpi-icon" style={{ background: color }}><Icon size={13} stroke={2.2} /></span></div>
      <div className="label">{label}</div>
      <div className="value" style={{ color: hot ? color : undefined }}>{value}</div>
    </div>
  );
}

const STATUS_COLOR: Record<string, string> = { AVAILABLE: "green", DOWN: "red", MAINTENANCE: "amber" };

type NotifSortKey = "number" | "machine" | "priority" | "status" | "reported";
const NOTIF_SORT_VALUE: Record<NotifSortKey, (n: MaintNotification) => number | string> = {
  number: (n) => n.number,
  machine: (n) => n.equipment_code,
  priority: (n) => n.priority,
  status: (n) => n.status,
  reported: (n) => n.reported_at,
};

type OrderSortKey = "number" | "type" | "machine" | "status" | "planned" | "technician";
const ORDER_SORT_VALUE: Record<OrderSortKey, (o: MaintOrder) => number | string> = {
  number: (o) => o.number,
  type: (o) => o.order_type,
  machine: (o) => o.equipment_code,
  status: (o) => o.status,
  planned: (o) => o.planned_start,
  technician: (o) => o.technician_name ?? "",
};

type DownSortKey = "machine" | "from" | "duration" | "kind" | "reason";
const DOWN_SORT_VALUE: Record<DownSortKey, (d: Downtime) => number | string> = {
  machine: (d) => d.equipment_code,
  from: (d) => d.started_at,
  duration: (d) => d.duration_min,
  kind: (d) => (d.planned ? "planned" : "breakdown"),
  reason: (d) => d.reason_code,
};

export function MaintenancePage() {
  const { plant } = usePlant();
  const pid = plant?.id;
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "machines";
  const [reporting, setReporting] = useState<EquipmentHealth | null>(null);
  const qc = useQueryClient();
  const canReport = useCan("maintenance:report");
  const canExecute = useCan("maintenance:execute");
  const eq = useQuery({
    queryKey: ["equipment-health", pid], queryFn: () => unwrap(core.GET("/api/v1/maintenance/equipment", { params: { query: { plant_id: pid! } } })),
    enabled: Boolean(pid), refetchInterval: 5_000,
  });
  const notifs = useQuery({
    queryKey: ["m-notifs", pid], queryFn: () => unwrap(core.GET("/api/v1/maintenance/notifications", { params: { query: { plant_id: pid! } } })),
    enabled: Boolean(pid), refetchInterval: 5_000,
  });
  const orders = useQuery({
    queryKey: ["m-orders", pid], queryFn: () => unwrap(core.GET("/api/v1/maintenance/orders", { params: { query: { plant_id: pid! } } })),
    enabled: Boolean(pid), refetchInterval: 5_000,
  });
  const downtime = useQuery({
    queryKey: ["m-downtime", pid], queryFn: () => unwrap(core.GET("/api/v1/maintenance/downtime", { params: { query: { plant_id: pid!, days: 30 } } })),
    enabled: Boolean(pid), refetchInterval: 10_000,
  });
  const refresh = () => ["equipment-health", "m-notifs", "m-orders", "m-downtime", "m-order"].forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
  const makeOrder = useMutation({
    mutationFn: (id: string) => unwrap(core.POST("/api/v1/maintenance/notifications/{notification_id}/order", { params: { path: { notification_id: id } }, body: { duration_min: 120 } })),
    onSuccess: (mo) => { refresh(); setParams({ tab: "orders", order: mo.id }); },
  });
  const backUp = useMutation({
    mutationFn: ({ id, note }: { id: string; note: string }) => unwrap(core.POST("/api/v1/maintenance/equipment/{equipment_id}/back-up", { params: { path: { equipment_id: id } }, body: { note } })),
    onSuccess: refresh,
  });
  const machines = eq.data ?? [];
  const openOrders = (orders.data ?? []).filter((o) => ["CREATED", "IN_PROGRESS"].includes(o.status));
  const openNotifs = (notifs.data ?? []).filter((n) => n.status === "OPEN");
  const down = machines.filter((m) => m.status !== "AVAILABLE").length;
  const downtimeH = num(machines.reduce((a, m) => a + m.reliability.downtime_h, 0), 1);
  const [backUpFor, setBackUpFor] = useState<EquipmentHealth | null>(null);

  const [nSearch, setNSearch] = useState("");
  const [nSort, setNSort] = useState<{ key: NotifSortKey; dir: 1 | -1 }>({ key: "reported", dir: -1 });
  const notifRows = useMemo(() => {
    const s = nSearch.trim().toLowerCase();
    const filtered = (notifs.data ?? []).filter(
      (n) => !s || n.number.toLowerCase().includes(s) || n.equipment_code.toLowerCase().includes(s) || n.description.toLowerCase().includes(s) || (n.reported_by_name ?? "").toLowerCase().includes(s),
    );
    return sortBy(filtered, nSort, NOTIF_SORT_VALUE);
  }, [notifs.data, nSearch, nSort]);

  const [oSearch, setOSearch] = useState("");
  const [oSort, setOSort] = useState<{ key: OrderSortKey; dir: 1 | -1 }>({ key: "planned", dir: 1 });
  const orderRows = useMemo(() => {
    const s = oSearch.trim().toLowerCase();
    const filtered = (orders.data ?? []).filter(
      (o) => !s || o.number.toLowerCase().includes(s) || o.equipment_code.toLowerCase().includes(s) || o.description.toLowerCase().includes(s) || (o.technician_name ?? "").toLowerCase().includes(s),
    );
    return sortBy(filtered, oSort, ORDER_SORT_VALUE);
  }, [orders.data, oSearch, oSort]);

  const [dSearch, setDSearch] = useState("");
  const [dSort, setDSort] = useState<{ key: DownSortKey; dir: 1 | -1 }>({ key: "from", dir: -1 });
  const downtimeRows = useMemo(() => {
    const s = dSearch.trim().toLowerCase();
    const filtered = (downtime.data ?? []).filter(
      (d) => !s || d.equipment_code.toLowerCase().includes(s) || d.reason_code.toLowerCase().includes(s) || (d.description ?? "").toLowerCase().includes(s) || (d.reported_by_name ?? "").toLowerCase().includes(s),
    );
    return sortBy(filtered, dSort, DOWN_SORT_VALUE);
  }, [downtime.data, dSearch, dSort]);

  return (
    <>
      <PageHeader title="Equipment & maintenance" sub="Machine availability, breakdowns reported from the line, maintenance orders mirrored to SAP PM, preventive plans" />
      <SimpleGrid cols={4} mb="md" className="rise">
        <MaintKpi icon={IconAlertTriangle} label="Machines not available" value={down} color="var(--mes-red)" hot={down > 0} onClick={() => setParams({ tab: "machines" })} />
        <MaintKpi icon={IconBell} label="Open notifications" value={openNotifs.length} color="var(--mes-amber)" hot={openNotifs.length > 0} onClick={() => setParams({ tab: "notifications" })} />
        <MaintKpi icon={IconTool} label="Open maintenance orders" value={openOrders.length} color="var(--mes-blue)" onClick={() => setParams({ tab: "orders" })} />
        <MaintKpi icon={IconClockExclamation} label="Unplanned downtime (30 d)" value={`${downtimeH} h`} color="var(--mes-red)" onClick={() => setParams({ tab: "downtime" })} />
      </SimpleGrid>
      <Tabs value={tab} onChange={(v) => setParams({ tab: v ?? "machines" })}>
        <Tabs.List>
          <Tabs.Tab value="machines">Machines</Tabs.Tab>
          <Tabs.Tab value="notifications">Notifications</Tabs.Tab>
          <Tabs.Tab value="orders">Maintenance orders</Tabs.Tab>
          <Tabs.Tab value="downtime">Downtime log</Tabs.Tab>
          <Tabs.Tab value="plans">Preventive plans</Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="machines" pt="sm">
          <SimpleGrid cols={3}>
            {machines.map((m) => (
              <div key={m.id} className={`panel machine-card st-${m.status}`} style={{ padding: 12 }}>
                <Group justify="space-between" wrap="nowrap">
                  <div>
                    <Text className="mono" fw={700} size="lg">{m.code}</Text>
                    <Text size="xs" c="dimmed">{m.name} · {m.line_code} · {m.work_center_code}</Text>
                  </div>
                  <Badge color={STATUS_COLOR[m.status] ?? "gray"} variant={m.status === "AVAILABLE" ? "outline" : "filled"}>{m.status}</Badge>
                </Group>
                {m.open_downtime && (
                  <Text size="sm" c="red.7" mt={6}><IconAlertTriangle size={13} /> {m.open_downtime.planned ? "planned maintenance" : m.open_downtime.reason_code.toLowerCase()} since {fmtDateTime(m.open_downtime.started_at)} ({fromNow(m.open_downtime.started_at)})
                    {m.open_downtime.description ? ` · ${m.open_downtime.description}` : ""}</Text>
                )}
                <SimpleGrid cols={4} mt="sm" spacing={6}>
                  <div><div className="label">MTBF</div><Text className="mono" size="sm">{m.reliability.mtbf_h != null ? `${num(m.reliability.mtbf_h, 1)} h` : "—"}</Text></div>
                  <div><div className="label">MTTR</div><Text className="mono" size="sm">{m.reliability.mttr_h != null ? `${num(m.reliability.mttr_h * 60)} min` : "—"}</Text></div>
                  <div><div className="label">Failures</div><Text className="mono" size="sm">{m.reliability.failures}</Text></div>
                  <div><div className="label">Run h</div><Text className="mono" size="sm">{num(m.reliability.runtime_h, 1)}</Text></div>
                </SimpleGrid>
                <Group justify="space-between" mt="sm">
                  <Text size="xs" c="dimmed">{m.open_orders ? `${m.open_orders} open order(s)` : "no open orders"}{m.next_preventive ? ` · next PM ${fmtDateTime(m.next_preventive)}` : ""}</Text>
                  <Group gap={4}>
                    {canReport && m.status === "DOWN" && <Button size="compact-xs" variant="subtle" onClick={() => setBackUpFor(m)}>Back up</Button>}
                    {canReport && <Button size="compact-xs" variant="light" color="amber" leftSection={<IconTool size={12} />} onClick={() => setReporting(m)}>Report problem</Button>}
                  </Group>
                </Group>
              </div>
            ))}
          </SimpleGrid>
        </Tabs.Panel>

        <Tabs.Panel value="notifications" pt="sm">
          <TextInput size="xs" w={280} mb="xs" leftSection={<IconSearch size={14} />}
            rightSection={nSearch ? (
              <ActionIcon variant="subtle" color="gray" size="xs" onClick={() => setNSearch("")} aria-label="Clear search"><IconX size={12} /></ActionIcon>
            ) : undefined}
            placeholder="Number, machine, problem, reported by…" value={nSearch} onChange={(e) => setNSearch(e.currentTarget.value)} />
          <Table className="panel">
            <Table.Thead>
              <Table.Tr>
                <SortTh id="number" sort={nSort} onSort={(k) => toggleSort(setNSort, k)}>Notification</SortTh>
                <SortTh id="machine" sort={nSort} onSort={(k) => toggleSort(setNSort, k)}>Machine</SortTh>
                <Table.Th>Problem</Table.Th>
                <SortTh id="priority" align="right" sort={nSort} onSort={(k) => toggleSort(setNSort, k)}>Prio</SortTh>
                <SortTh id="status" sort={nSort} onSort={(k) => toggleSort(setNSort, k)}>Status</SortTh>
                <SortTh id="reported" sort={nSort} onSort={(k) => toggleSort(setNSort, k)}>Reported</SortTh>
                <Table.Th>SAP</Table.Th>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {notifRows.map((n) => (
                <Table.Tr key={n.id}>
                  <Table.Td className="mono">{n.number}<Text size="10px" c="dimmed">{n.notif_type === "BREAKDOWN" ? "M2 breakdown" : "M1 request"}</Text></Table.Td>
                  <Table.Td className="mono">{n.equipment_code}{n.machine_down && <Badge ml={6} size="xs" color="red">down</Badge>}</Table.Td>
                  <Table.Td><Text size="sm">{n.description}</Text><Text size="xs" c="dimmed">{n.reason_code.toLowerCase()}</Text></Table.Td>
                  <Table.Td className="mono" style={{ textAlign: "right" }}>{n.priority}</Table.Td>
                  <Table.Td><Badge size="sm" variant={n.status === "OPEN" ? "light" : "outline"} color={n.status === "OPEN" ? "amber" : n.status === "COMPLETED" ? "green" : "blue"}>{n.status.replace("_", " ")}</Badge></Table.Td>
                  <Table.Td><Text size="xs">{n.reported_by_name}</Text><Text size="10px" c="dimmed">{fmtDateTime(n.reported_at)}</Text></Table.Td>
                  <Table.Td><SapSync status={n.erp_sync_status} error={n.erp_error} /></Table.Td>
                  <Table.Td>
                    {n.maintenance_order_id ? <Button size="compact-xs" variant="subtle" onClick={() => setParams({ tab: "orders", order: n.maintenance_order_id! })}>order {n.maintenance_order_number}</Button>
                      : canExecute && n.status === "OPEN" && <Button size="compact-xs" loading={makeOrder.isPending} onClick={() => makeOrder.mutate(n.id)}>Create order</Button>}
                  </Table.Td>
                </Table.Tr>
              ))}
              {!notifRows.length && (
                <Table.Tr><Table.Td colSpan={8}><Text c="dimmed" ta="center" py="lg" size="sm">No notifications match this filter.</Text></Table.Td></Table.Tr>
              )}
            </Table.Tbody>
          </Table>
        </Tabs.Panel>

        <Tabs.Panel value="orders" pt="sm">
          <TextInput size="xs" w={280} mb="xs" leftSection={<IconSearch size={14} />}
            rightSection={oSearch ? (
              <ActionIcon variant="subtle" color="gray" size="xs" onClick={() => setOSearch("")} aria-label="Clear search"><IconX size={12} /></ActionIcon>
            ) : undefined}
            placeholder="Order, machine, work, technician…" value={oSearch} onChange={(e) => setOSearch(e.currentTarget.value)} />
          <Table className="panel" highlightOnHover>
            <Table.Thead>
              <Table.Tr>
                <SortTh id="number" sort={oSort} onSort={(k) => toggleSort(setOSort, k)}>Order</SortTh>
                <SortTh id="type" sort={oSort} onSort={(k) => toggleSort(setOSort, k)}>Type</SortTh>
                <SortTh id="machine" sort={oSort} onSort={(k) => toggleSort(setOSort, k)}>Machine</SortTh>
                <Table.Th>Work</Table.Th>
                <SortTh id="status" sort={oSort} onSort={(k) => toggleSort(setOSort, k)}>Status</SortTh>
                <SortTh id="planned" sort={oSort} onSort={(k) => toggleSort(setOSort, k)}>Planned</SortTh>
                <SortTh id="technician" sort={oSort} onSort={(k) => toggleSort(setOSort, k)}>Technician</SortTh>
                <Table.Th>SAP</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {orderRows.map((o) => (
                <Table.Tr key={o.id} style={{ cursor: "pointer" }} onClick={() => setParams({ tab: "orders", order: o.id })}>
                  <Table.Td className="mono">{o.number}</Table.Td>
                  <Table.Td><Text size="xs">{o.order_type === "PREVENTIVE" ? "PM02 preventive" : "PM01 corrective"}</Text></Table.Td>
                  <Table.Td className="mono">{o.equipment_code}</Table.Td>
                  <Table.Td><Text size="sm">{o.description}</Text><Text size="xs" c="dimmed">{o.checklist.filter((c) => c.done).length}/{o.checklist.length} tasks</Text></Table.Td>
                  <Table.Td><Badge size="sm" variant={o.status === "COMPLETED" ? "outline" : "light"} color={o.status === "IN_PROGRESS" ? "cyan" : o.status === "COMPLETED" ? "green" : o.overdue ? "red" : "navy.6"}>
                    {o.overdue && o.status === "CREATED" ? "OVERDUE" : o.status.replace("_", " ")}</Badge></Table.Td>
                  <Table.Td className="mono">{fmtDateTime(o.planned_start)}<Text size="10px" c="dimmed">{minutes(o.planned_duration_min)}</Text></Table.Td>
                  <Table.Td><Text size="xs">{o.technician_name ?? "—"}</Text></Table.Td>
                  <Table.Td><SapSync status={o.erp_sync_status} error={o.erp_error} extra={o.erp_system_status} /></Table.Td>
                </Table.Tr>
              ))}
              {!orderRows.length && (
                <Table.Tr><Table.Td colSpan={8}><Text c="dimmed" ta="center" py="lg" size="sm">No orders match this filter.</Text></Table.Td></Table.Tr>
              )}
            </Table.Tbody>
          </Table>
        </Tabs.Panel>

        <Tabs.Panel value="downtime" pt="sm">
          <TextInput size="xs" w={280} mb="xs" leftSection={<IconSearch size={14} />}
            rightSection={dSearch ? (
              <ActionIcon variant="subtle" color="gray" size="xs" onClick={() => setDSearch("")} aria-label="Clear search"><IconX size={12} /></ActionIcon>
            ) : undefined}
            placeholder="Machine, reason, description, reported by…" value={dSearch} onChange={(e) => setDSearch(e.currentTarget.value)} />
          <Table className="panel">
            <Table.Thead>
              <Table.Tr>
                <SortTh id="machine" sort={dSort} onSort={(k) => toggleSort(setDSort, k)}>Machine</SortTh>
                <SortTh id="from" sort={dSort} onSort={(k) => toggleSort(setDSort, k)}>From</SortTh>
                <Table.Th>To</Table.Th>
                <SortTh id="duration" align="right" sort={dSort} onSort={(k) => toggleSort(setDSort, k)}>Duration</SortTh>
                <SortTh id="kind" sort={dSort} onSort={(k) => toggleSort(setDSort, k)}>Kind</SortTh>
                <SortTh id="reason" sort={dSort} onSort={(k) => toggleSort(setDSort, k)}>Reason</SortTh>
                <Table.Th>Reported / released</Table.Th>
                <Table.Th>Notification / order</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {downtimeRows.map((d) => (
                <Table.Tr key={d.id}>
                  <Table.Td className="mono">{d.equipment_code}</Table.Td>
                  <Table.Td className="mono">{fmtDateTime(d.started_at)}</Table.Td>
                  <Table.Td className="mono">{d.ended_at ? fmtDateTime(d.ended_at) : <Text span c="red.7" className="pulse">ongoing</Text>}</Table.Td>
                  <Table.Td className="mono" style={{ textAlign: "right" }}>{minutes(Math.round(d.duration_min))}</Table.Td>
                  <Table.Td><Badge size="xs" variant="outline" color={d.planned ? "navy.6" : "red"}>{d.planned ? "planned" : "breakdown"}</Badge></Table.Td>
                  <Table.Td><Text size="sm">{d.reason_code.replaceAll("_", " ").toLowerCase()}</Text><Text size="xs" c="dimmed" lineClamp={1}>{d.description}</Text></Table.Td>
                  <Table.Td><Text size="xs">{d.reported_by_name}{d.ended_by_name ? ` / ${d.ended_by_name}` : ""}</Text></Table.Td>
                  <Table.Td className="mono"><Text size="xs">{[d.notification_number, d.maintenance_order_number].filter(Boolean).join(" · ") || "—"}</Text></Table.Td>
                </Table.Tr>
              ))}
              {!downtimeRows.length && (
                <Table.Tr><Table.Td colSpan={8}><Text c="dimmed" ta="center" py="lg" size="sm">No downtime events match this filter.</Text></Table.Td></Table.Tr>
              )}
            </Table.Tbody>
          </Table>
        </Tabs.Panel>

        <Tabs.Panel value="plans" pt="sm"><PlansTab machines={machines} /></Tabs.Panel>
      </Tabs>
      <OrderDrawer id={params.get("order")} onClose={() => setParams({ tab })} onChange={refresh} />
      {reporting && <ReportModal machine={reporting} onClose={() => setReporting(null)} onDone={() => { setReporting(null); refresh(); }} />}
      <ReasonModal opened={backUpFor !== null} title={`${backUpFor?.code} is back up`} label="What was done?" confirmLabel="Release machine"
        busy={backUp.isPending} onClose={() => setBackUpFor(null)} onSubmit={(note) => backUp.mutate({ id: backUpFor!.id, note }, { onSuccess: () => setBackUpFor(null) })} />
    </>
  );
}

function SapSync({ status, error, extra }: { status: string; error?: string | null; extra?: string | null }) {
  if (status === "POSTED") return <Text size="xs" c="green.7" className="mono">in SAP{extra ? ` · ${extra}` : ""}</Text>;
  if (status === "PENDING") return <Text size="xs" c="amber.8" className="pulse">sending…</Text>;
  if (status === "FAILED") return <Text size="xs" c="red.7" title={error ?? ""}>SAP rejected</Text>;
  return <Text size="xs" c="dimmed">local</Text>;
}

function ReportModal({ machine, onClose, onDone }: { machine: EquipmentHealth; onClose: () => void; onDone: () => void }) {
  const meta = useQuery({ queryKey: ["maint-meta"], queryFn: () => unwrap(core.GET("/api/v1/maintenance/meta")), staleTime: Infinity });
  const [kind, setKind] = useState("BREAKDOWN");
  const [down, setDown] = useState(true);
  const [reason, setReason] = useState<string | null>("MECHANICAL");
  const [text, setText] = useState("");
  const m = useMutation({
    mutationFn: () => kind === "BREAKDOWN"
      ? unwrap(core.POST("/api/v1/maintenance/breakdowns", { body: { equipment_id: machine.id, reason_code: reason!, description: text, machine_down: down, client_event_id: crypto.randomUUID() } }))
      : unwrap(core.POST("/api/v1/maintenance/requests", { body: { equipment_id: machine.id, description: text, priority: 3 } })),
    onSuccess: onDone,
  });
  return (
    <Modal opened onClose={onClose} centered title={<Text fw={600}>Report a problem on {machine.code}</Text>}>
      <Stack>
        <SegmentedControl value={kind} onChange={setKind} data={[{ value: "BREAKDOWN", label: "Breakdown (M2)" }, { value: "REQUEST", label: "Maintenance request (M1)" }]} />
        {kind === "BREAKDOWN" && <>
          <Select label="Reason" data={(meta.data?.downtime_reasons ?? []).map((r) => ({ value: r.code, label: r.label }))} value={reason} onChange={setReason} />
          <Switch label="Machine is down (pauses its run, blocks it for production)" checked={down} onChange={(e) => setDown(e.currentTarget.checked)} />
        </>}
        <Textarea label="Description" autosize minRows={2} value={text} onChange={(e) => setText(e.currentTarget.value)} />
        <Text size="xs" c="dimmed">A notification is created and mirrored to SAP PM.</Text>
        <Group justify="flex-end"><Button variant="default" onClick={onClose}>Cancel</Button>
          <Button loading={m.isPending} disabled={kind === "REQUEST" && !text.trim()} onClick={() => m.mutate()}>Report</Button></Group>
      </Stack>
    </Modal>
  );
}

function OrderDrawer({ id, onClose, onChange }: { id: string | null; onClose: () => void; onChange: () => void }) {
  const qc = useQueryClient();
  const { plant } = usePlant();
  const canExecute = useCan("maintenance:execute");
  const q = useQuery({
    queryKey: ["m-order", id], queryFn: () => unwrap(core.GET("/api/v1/maintenance/orders/{order_id}", { params: { path: { order_id: id! } } })),
    enabled: Boolean(id), refetchInterval: 5_000,
  });
  const parts = useQuery({ queryKey: ["spare-parts", plant?.id], queryFn: () => unwrap(core.GET("/api/v1/maintenance/spare-parts", { params: { query: { plant_id: plant!.id } } })), enabled: Boolean(plant && id) });
  const [part, setPart] = useState<string | null>(null);
  const [partQty, setPartQty] = useState<number | string>(1);
  const [labor, setLabor] = useState<number | string>("");
  const [findings, setFindings] = useState("");
  const [signing, setSigning] = useState(false);
  useEffect(() => { setFindings(""); setLabor(""); setPart(null); }, [id]);
  const done = (o: MaintOrderDetail) => { qc.setQueryData(["m-order", id], o); onChange(); };
  const start = useMutation({ mutationFn: () => unwrap(core.POST("/api/v1/maintenance/orders/{order_id}/start", { params: { path: { order_id: id! } } })), onSuccess: done });
  const work = useMutation({
    mutationFn: (body: { checklist: MaintOrderDetail["checklist"]; labor_min?: string | null }) =>
      unwrap(core.PUT("/api/v1/maintenance/orders/{order_id}/work", { params: { path: { order_id: id! } }, body })), onSuccess: done,
  });
  const issue = useMutation({
    mutationFn: () => unwrap(core.POST("/api/v1/maintenance/orders/{order_id}/parts", { params: { path: { order_id: id! } }, body: { material_id: part!, quantity: String(partQty), client_event_id: crypto.randomUUID() } })),
    onSuccess: (o) => { done(o); setPart(null); setPartQty(1); },
  });
  const complete = useMutation({
    mutationFn: (reason: string) => unwrap(core.POST("/api/v1/maintenance/orders/{order_id}/complete", { params: { path: { order_id: id! } }, body: { findings, reason } })),
    meta: { silent: true },
    onSuccess: (o) => { setSigning(false); done(o); },
  });
  const o = q.data;
  const working = o?.status === "IN_PROGRESS";
  return (
    <Drawer opened={Boolean(id)} onClose={onClose} position="right" size="xl"
      title={o && <Group gap="sm"><Text fw={700} className="mono">{o.number}</Text><Badge variant="light">{o.status.replace("_", " ")}</Badge>
        <Text size="sm" c="dimmed">{o.order_type === "PREVENTIVE" ? "PM02 preventive" : "PM01 corrective"} · {o.equipment_code}</Text></Group>}>
      {!o ? <Loader size="sm" /> : (
        <Stack>
          <div className="panel" style={{ padding: 12 }}>
            <Text fw={600}>{o.description}</Text>
            <Group gap="xl" mt="sm" align="flex-start">
              <div><div className="label">Machine</div><Text className="mono">{o.equipment_code}</Text><Text size="xs" c="dimmed">{o.line_code}</Text></div>
              <div><div className="label">Planned</div><Text className="mono" size="sm">{fmtDateTime(o.planned_start)} · {minutes(o.planned_duration_min)}</Text></div>
              <div><div className="label">Technician</div><Text size="sm">{o.technician_name ?? "—"}</Text>{o.started_at && <Text size="xs" c="dimmed">started {fmtDateTime(o.started_at)}</Text>}</div>
              <div><div className="label">SAP PM</div><SapSync status={o.erp_sync_status} error={o.erp_error} extra={o.erp_system_status} /></div>
            </Group>
            {o.notification && <Text size="sm" mt="sm" c="dimmed">From notification {o.notification.number} ({o.notification.reported_by_name}, {fmtDateTime(o.notification.reported_at)}): {o.notification.description}</Text>}
          </div>
          {canExecute && o.status === "CREATED" && <Button leftSection={<IconPlayerPlay size={14} />} loading={start.isPending} onClick={() => start.mutate()}>Start work (machine blocked for production)</Button>}
          <div className="panel" style={{ padding: 12 }}>
            <Text className="label" mb={6}>Checklist</Text>
            <Stack gap={6}>
              {o.checklist.map((c, i) => (
                <Checkbox key={i} label={c.text} checked={c.done} disabled={!working || !canExecute}
                  onChange={() => work.mutate({ checklist: o.checklist.map((x, j) => (j === i ? { ...x, done: !x.done } : x)) })} />
              ))}
            </Stack>
          </div>
          <div className="panel" style={{ padding: 12 }}>
            <Text className="label" mb={6}>Spare parts (261 to the maintenance order)</Text>
            {o.parts.map((p) => <Group key={p.movement_id} justify="space-between"><Text size="sm" className="mono">{p.material_number} <Text span c="dimmed" size="xs">{p.material_description}</Text></Text>
              <Group gap={6}><Text size="sm" className="mono">{num(p.quantity, 3)} {p.uom}</Text><SapSync status={p.erp_sync_status} /></Group></Group>)}
            {o.parts.length === 0 && <Text size="sm" c="dimmed">None.</Text>}
            {working && canExecute && (
              <Group mt="sm" align="flex-end">
                <Select style={{ flex: 1 }} size="xs" label="Part" searchable data={(parts.data ?? []).map((p) => ({ value: p.material_id, label: `${p.material_number} · ${p.description} (${num(p.in_stores)} in stores)` }))} value={part} onChange={setPart} />
                <NumberInput size="xs" w={90} label="Qty" min={1} value={partQty} onChange={setPartQty} />
                <Button size="xs" disabled={!part} loading={issue.isPending} onClick={() => issue.mutate()}>Book</Button>
              </Group>
            )}
          </div>
          {working && canExecute && (
            <div className="panel" style={{ padding: 12 }}>
              <Group align="flex-end">
                <NumberInput w={160} size="xs" label="Labour (min)" min={0} value={labor} onChange={setLabor} placeholder={`${num(o.labor_min)}`} />
                <Button size="xs" variant="light" disabled={labor === ""} onClick={() => work.mutate({ checklist: o.checklist, labor_min: String(labor) })}>Save</Button>
              </Group>
              <TextInput mt="sm" label="Findings" value={findings} onChange={(e) => setFindings(e.currentTarget.value)} placeholder="What was found and done" />
              <Button mt="sm" disabled={!findings.trim() || o.checklist.some((c) => !c.done)} onClick={() => setSigning(true)}>Sign off & release machine…</Button>
            </div>
          )}
          {o.status === "COMPLETED" && (
            <div className="panel" style={{ padding: 12 }}>
              <div className="label">Completed</div>
              <Text size="sm">{o.findings}</Text>
              <Text size="xs" c="dimmed">{fmtDateTime(o.completed_at)} · labour {minutes(Math.round(Number(o.labor_min)))} · {o.technician_name}</Text>
            </div>
          )}
          <SignatureModal opened={signing} meaning="RELEASED" subject={`Maintenance order ${o.number} on ${o.equipment_code} completed; machine released to production`}
            busy={complete.isPending} error={complete.error instanceof ApiError ? complete.error.message : null}
            onClose={() => { setSigning(false); complete.reset(); }} onSign={(r) => complete.mutate(r)} />
        </Stack>
      )}
    </Drawer>
  );
}

function PlansTab({ machines }: { machines: EquipmentHealth[] }) {
  const { plant } = usePlant();
  const qc = useQueryClient();
  const canPlan = useCan("maintenance:plan");
  const [editing, setEditing] = useState<MaintPlan | "new" | null>(null);
  const plans = useQuery({ queryKey: ["m-plans", plant?.id], queryFn: () => unwrap(core.GET("/api/v1/maintenance/plans", { params: { query: { plant_id: plant!.id } } })), enabled: Boolean(plant) });
  const gen = useMutation({
    mutationFn: () => unwrap(core.POST("/api/v1/maintenance/plans/generate", { params: { query: { plant_id: plant!.id } } })),
    onSuccess: () => ["m-plans", "m-orders", "equipment-health"].forEach((k) => qc.invalidateQueries({ queryKey: [k] })),
  });
  return (
    <>
      {canPlan && <Group justify="flex-end" mb="sm">
        <Button size="xs" variant="default" loading={gen.isPending} onClick={() => gen.mutate()}>Run scheduler now{gen.data ? ` (${gen.data.length} new)` : ""}</Button>
        <Button size="xs" leftSection={<IconPlus size={14} />} onClick={() => setEditing("new")}>New plan</Button></Group>}
      <Table className="panel">
        <Table.Thead><Table.Tr><Table.Th>Machine</Table.Th><Table.Th>Plan</Table.Th><Table.Th>Interval</Table.Th><Table.Th>Last done</Table.Th><Table.Th>Next due</Table.Th><Table.Th>Open order</Table.Th><Table.Th /></Table.Tr></Table.Thead>
        <Table.Tbody>
          {(plans.data ?? []).map((p) => (
            <Table.Tr key={p.id} style={{ opacity: p.active ? 1 : 0.5 }}>
              <Table.Td className="mono">{p.equipment_code}</Table.Td>
              <Table.Td><Text size="sm">{p.name}</Text><Text size="xs" c="dimmed">{p.checklist.length} tasks · {minutes(p.duration_min)}</Text></Table.Td>
              <Table.Td><Text size="sm">{p.interval_type === "CALENDAR" ? `every ${p.interval_days} days` : `every ${num(p.interval_hours)} running h`}</Text>
                <Text size="xs" c="dimmed">{p.interval_type === "CALENDAR" ? `order ${p.lead_days} d ahead` : `${num(p.runtime_since_last_h, 1)} h since last`}</Text></Table.Td>
              <Table.Td className="mono">{fmtDateTime(p.last_done_at)}</Table.Td>
              <Table.Td className="mono">{p.next_due_at ? fmtDateTime(p.next_due_at) : "by running hours"}</Table.Td>
              <Table.Td className="mono">{p.open_order_number ?? "—"}</Table.Td>
              <Table.Td>{canPlan && <ActionIcon variant="subtle" onClick={() => setEditing(p)}><IconPencil size={15} /></ActionIcon>}</Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      {editing && <PlanModal plan={editing === "new" ? null : editing} machines={machines} onClose={() => setEditing(null)} />}
    </>
  );
}

function PlanModal({ plan, machines, onClose }: { plan: MaintPlan | null; machines: EquipmentHealth[]; onClose: () => void }) {
  const { plant } = usePlant();
  const qc = useQueryClient();
  const [p, setP] = useState<MaintPlanIn>(() => plan
    ? { plant_id: plan.plant_id, equipment_id: plan.equipment_id, name: plan.name, interval_type: plan.interval_type as "CALENDAR", interval_days: plan.interval_days,
        interval_hours: plan.interval_hours, lead_days: plan.lead_days, duration_min: plan.duration_min, checklist: plan.checklist, active: plan.active }
    : { plant_id: plant!.id, equipment_id: "", name: "", interval_type: "CALENDAR", interval_days: 30, interval_hours: null, lead_days: 3, duration_min: 60, checklist: [""], active: true });
  const save = useMutation({
    mutationFn: () => unwrap(core.PUT("/api/v1/maintenance/plans", { body: { ...p, checklist: p.checklist.filter((c) => c.trim()) } })),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["m-plans"] }); onClose(); },
  });
  return (
    <Modal opened onClose={onClose} size="lg" centered title={<Text fw={600}>{plan ? "Edit preventive plan" : "New preventive plan"}</Text>}>
      <Stack>
        <Group grow>
          <Select label="Machine" disabled={Boolean(plan)} data={machines.map((m) => ({ value: m.id, label: `${m.code} · ${m.name}` }))} value={p.equipment_id || null} onChange={(v) => setP({ ...p, equipment_id: v ?? "" })} />
          <TextInput label="Name" disabled={Boolean(plan)} value={p.name} onChange={(e) => setP({ ...p, name: e.currentTarget.value })} />
        </Group>
        <SegmentedControl value={p.interval_type} onChange={(v) => setP({ ...p, interval_type: v as "CALENDAR" })} data={[{ value: "CALENDAR", label: "Calendar" }, { value: "RUNTIME", label: "Running hours" }]} />
        <Group grow>
          {p.interval_type === "CALENDAR"
            ? <><NumberInput label="Every (days)" min={1} value={p.interval_days ?? ""} onChange={(v) => setP({ ...p, interval_days: Number(v) || null })} />
                <NumberInput label="Order ahead (days)" min={0} value={p.lead_days ?? 3} onChange={(v) => setP({ ...p, lead_days: Number(v) || 0 })} /></>
            : <NumberInput label="Every (running hours)" min={1} value={p.interval_hours ?? ""} onChange={(v) => setP({ ...p, interval_hours: v === "" ? null : String(v) })} />}
          <NumberInput label="Duration (min)" min={1} value={p.duration_min ?? 60} onChange={(v) => setP({ ...p, duration_min: Number(v) || 60 })} />
        </Group>
        <div className="label">Checklist</div>
        {p.checklist.map((c, i) => (
          <Group key={i} gap="xs" wrap="nowrap">
            <TextInput style={{ flex: 1 }} value={c} onChange={(e) => setP({ ...p, checklist: p.checklist.map((x, j) => (j === i ? e.currentTarget.value : x)) })} />
            <ActionIcon variant="subtle" color="red" disabled={p.checklist.length === 1} onClick={() => setP({ ...p, checklist: p.checklist.filter((_, j) => j !== i) })}><IconTrash size={15} /></ActionIcon>
          </Group>
        ))}
        <Button variant="subtle" size="xs" w="fit-content" leftSection={<IconPlus size={14} />} onClick={() => setP({ ...p, checklist: [...p.checklist, ""] })}>Add task</Button>
        <Switch label="Active" checked={p.active ?? true} onChange={(e) => setP({ ...p, active: e.currentTarget.checked })} />
        <Group justify="flex-end"><Button variant="default" onClick={onClose}>Cancel</Button>
          <Button loading={save.isPending} disabled={!p.equipment_id || !p.name.trim() || !p.checklist.some((c) => c.trim())} onClick={() => save.mutate()}>Save plan</Button></Group>
      </Stack>
    </Modal>
  );
}
