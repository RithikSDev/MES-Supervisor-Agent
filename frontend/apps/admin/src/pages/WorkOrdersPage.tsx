import {
  ActionIcon, Badge, Button, Drawer, Group, Highlight, Loader, Progress, SimpleGrid, Stack, Table, Text, TextInput, Timeline,
  UnstyledButton,
} from "@mantine/core";
import {
  IconAlertTriangle, IconArrowsSort, IconArrowUpRight, IconCalendarTime, IconChevronDown, IconChevronRight, IconChevronUp,
  IconCircleCheck, IconDownload, IconInbox, IconPlayerPause, IconPlayerPlay, IconProgress, IconRefresh, IconSearch,
  IconTruckDelivery, IconX, type Icon,
} from "@tabler/icons-react";
import { useQuery } from "@tanstack/react-query";
import dayjs from "dayjs";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router";
import { core, unwrap, type WorkOrderSummary } from "@mes/api-client";
import { OpStatus, WoStatus } from "../components/badges";
import { PageHeader } from "../components/Layout";
import { usePlant } from "../components/plant";
import { type Cell, csvName, downloadCsv } from "../lib/csv";
import { fmtDate, fmtDateTime, isLate, minutes, num } from "../lib/format";
import { Fact } from "./WorkOrderDetailPage";

type Bucket = { key: string; label: string; match: (w: WorkOrderSummary) => boolean; alarm?: boolean; color: string; icon: Icon };

/** Accent colours mirror WoStatus's badge colours below, so the strip and the table read as one system. */
const BUCKETS: Bucket[] = [
  { key: "open", label: "Open", match: (w) => !["COMPLETED", "CLOSED", "CANCELLED"].includes(w.status), color: "var(--mes-blue)", icon: IconInbox },
  { key: "RELEASED", label: "Released", match: (w) => w.status === "RELEASED", color: "var(--mes-navy)", icon: IconPlayerPlay },
  { key: "SCHEDULED", label: "Scheduled", match: (w) => w.status === "SCHEDULED", color: "var(--mes-navy)", icon: IconCalendarTime },
  { key: "DISPATCHED", label: "Dispatched", match: (w) => w.status === "DISPATCHED", color: "var(--mes-cyan)", icon: IconTruckDelivery },
  { key: "IN_PROGRESS", label: "In progress", match: (w) => w.status === "IN_PROGRESS", color: "var(--mes-blue)", icon: IconProgress },
  { key: "ON_HOLD", label: "On hold", match: (w) => w.status === "ON_HOLD", alarm: true, color: "var(--mes-amber)", icon: IconPlayerPause },
  { key: "late", label: "Late", match: (w) => isLate(w.erp_basic_end, w.status), alarm: true, color: "var(--mes-red)", icon: IconAlertTriangle },
  { key: "done", label: "Completed", match: (w) => ["COMPLETED", "CLOSED"].includes(w.status), color: "var(--mes-green)", icon: IconCircleCheck },
];

/** Priority 1 = most urgent (DB constraint: 1-5). */
const PRIO_STYLE: Record<number, { color: string; variant: "filled" | "outline" | "light" }> = {
  1: { color: "red", variant: "filled" },
  2: { color: "amber", variant: "filled" },
  3: { color: "navy.6", variant: "outline" },
  4: { color: "navy.4", variant: "light" },
  5: { color: "navy.4", variant: "light" },
};
function PrioBadge({ p }: { p: number }) {
  const s = PRIO_STYLE[p] ?? PRIO_STYLE[3];
  return <Badge size="sm" color={s.color} variant={s.variant}>{p}</Badge>;
}

/** Eases the displayed number toward a new value instead of jumping, so live refetches feel alive rather than jumpy. */
function useCountUp(value: number, duration = 400) {
  const [display, setDisplay] = useState(value);
  const prev = useRef(value);
  useEffect(() => {
    if (prev.current === value) return;
    const from = prev.current;
    const start = performance.now();
    let raf = 0;
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - (1 - t) * (1 - t);
      setDisplay(Math.round(from + (value - from) * eased));
      if (t < 1) raf = requestAnimationFrame(tick);
      else prev.current = value;
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [value, duration]);
  return display;
}

function KpiTile({
  b, count, pct, selected, hot, sub, onClick,
}: { b: Bucket; count: number; pct: number; selected: boolean; hot: boolean; sub: React.ReactNode; onClick: () => void }) {
  const display = useCountUp(count);
  return (
    <div
      className={`panel kpi wo-kpi${selected ? " selected" : ""}`}
      style={{ borderTop: `3px solid ${b.color}` }}
      onClick={onClick}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => e.key === "Enter" && onClick()}
    >
      <div className="wo-kpi-top">
        <span className="wo-kpi-icon" style={{ background: b.color }}><b.icon size={13} stroke={2.2} /></span>
        <span className="wo-kpi-pct">{pct}%</span>
      </div>
      <div className="label">{b.label}</div>
      <div className="value" style={{ color: hot ? (b.key === "late" ? "var(--mes-red)" : "var(--mes-amber)") : undefined }}>
        {display}
      </div>
      <div className="wo-kpi-bar"><div className="wo-kpi-bar-fill" style={{ width: `${pct}%`, background: b.color }} /></div>
      {sub}
    </div>
  );
}

type SortKey = "order" | "material" | "qty" | "ops" | "due" | "sched" | "prio" | "status" | "sap";
const SORT_VALUE: Record<SortKey, (w: WorkOrderSummary) => number | string> = {
  order: (w) => w.erp_order_number,
  material: (w) => w.material_number ?? "",
  qty: (w) => Number(w.yield_qty) / Math.max(1, Number(w.planned_qty)),
  ops: (w) => (w.op_count ? w.ops_confirmed / w.op_count : 0),
  due: (w) => w.erp_basic_end,
  sched: (w) => w.sched_start ?? "",
  prio: (w) => w.priority,
  status: (w) => w.status,
  sap: (w) => w.erp_status,
};

function Sortable({
  id, label, active, dir, onSort, align,
}: { id: SortKey; label: string; active: boolean; dir: 1 | -1; onSort: (id: SortKey) => void; align?: "right" }) {
  return (
    <Table.Th style={align === "right" ? { textAlign: "right" } : undefined}>
      <UnstyledButton onClick={() => onSort(id)} className="sortable-th">
        <Group gap={4} wrap="nowrap" justify={align === "right" ? "flex-end" : "flex-start"}>
          <span>{label}</span>
          {active ? (dir === 1 ? <IconChevronUp size={12} /> : <IconChevronDown size={12} />) : <IconArrowsSort size={12} opacity={0.35} />}
        </Group>
      </UnstyledButton>
    </Table.Th>
  );
}

/** Ticks once a second so "Updated Ns ago" stays fresh without re-fetching. */
function UpdatedAgo({ at }: { at: number }) {
  const [, tick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => tick((x) => x + 1), 1000);
    return () => clearInterval(t);
  }, []);
  if (!at) return <Text size="xs" c="dimmed" className="mono">Loading…</Text>;
  const s = Math.max(0, Math.round((Date.now() - at) / 1000));
  const txt = s < 3 ? "just now" : s < 60 ? `${s}s ago` : `${Math.floor(s / 60)}m ago`;
  return <Text size="xs" c="dimmed" className="mono">Updated {txt}</Text>;
}

/** Fast peek at a row without leaving the list; "Open full page" goes to the real detail page for actions. */
function WorkOrderQuickView({ id, onClose }: { id: string | null; onClose: () => void }) {
  const q = useQuery({
    queryKey: ["work-order", id],
    queryFn: () => unwrap(core.GET("/api/v1/work-orders/{wo_id}", { params: { path: { wo_id: id! } } })),
    enabled: Boolean(id),
  });
  const w = q.data;
  const late = w ? isLate(w.erp_basic_end, w.status) : false;

  return (
    <Drawer
      opened={Boolean(id)}
      onClose={onClose}
      position="right"
      size="lg"
      title={w && (
        <Group gap="sm">
          <Text fw={700} className="mono">{w.erp_order_number}</Text>
          <WoStatus status={w.status} />
          {late && <Badge color="red" variant="filled" size="sm">LATE</Badge>}
        </Group>
      )}
    >
      {!w ? (
        <Group p="xl" justify="center"><Loader size="sm" /></Group>
      ) : (
        <Stack>
          <div className="panel" style={{ padding: 12 }}>
            <Text fw={600} size="lg" className="mono">{w.material_number}</Text>
            <Text size="sm" c="dimmed">{w.material_description}</Text>
          </div>

          <SimpleGrid cols={2} className="panel" p="md" spacing="lg">
            <Fact label="Quantity">
              <span className="mono">{num(w.yield_qty)} / {num(w.planned_qty)} {w.uom}</span>
              {Number(w.scrap_qty) > 0 && <Text span c="red.7" size="xs" className="mono"> · scrap {num(w.scrap_qty)}</Text>}
            </Fact>
            <Fact label="SAP basic dates"><span className="mono">{fmtDate(w.erp_basic_start)} → {fmtDate(w.erp_basic_end)}</span></Fact>
            <Fact label="Scheduled"><span className="mono">{fmtDateTime(w.sched_start)} → {fmtDateTime(w.sched_end)}</span></Fact>
            <Fact label="SAP status"><span className="mono">{w.erp_status}</span></Fact>
            <Fact label="Order type"><span className="mono">{w.order_type}</span> · {w.source}</Fact>
            <Fact label="Last SAP change">{fmtDateTime(w.erp_last_changed_at)}</Fact>
          </SimpleGrid>

          <div>
            <Text className="label" mb={6}>Operations ({w.operations.length})</Text>
            <Table className="panel">
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Op</Table.Th>
                  <Table.Th>Description</Table.Th>
                  <Table.Th>Work center</Table.Th>
                  <Table.Th>Duration</Table.Th>
                  <Table.Th>Yield</Table.Th>
                  <Table.Th>Status</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {w.operations.map((o) => (
                  <Table.Tr key={o.id}>
                    <Table.Td className="mono">{o.op_no}</Table.Td>
                    <Table.Td>{o.description}</Table.Td>
                    <Table.Td className="mono">{o.work_center_code}</Table.Td>
                    <Table.Td className="mono">{minutes(o.duration_min)}</Table.Td>
                    <Table.Td className="mono">{num(o.yield_qty)}/{num(o.planned_qty)}</Table.Td>
                    <Table.Td><OpStatus status={o.status} /></Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </div>

          {w.history.length > 0 && (
            <div>
              <Text className="label" mb={6}>Recent status changes</Text>
              <Timeline bulletSize={10} lineWidth={2} color="brand">
                {[...w.history].reverse().slice(0, 5).map((h) => (
                  <Timeline.Item key={h.id} title={<Group gap={6}>{h.from_status && <><WoStatus status={h.from_status} />→</>}<WoStatus status={h.to_status} /></Group>}>
                    <Text size="xs" c="dimmed" className="mono">{fmtDateTime(h.changed_at)} · {h.changed_by_name ?? h.changed_by}</Text>
                    {h.reason && <Text size="sm">{h.reason}</Text>}
                  </Timeline.Item>
                ))}
              </Timeline>
            </div>
          )}

          <Group justify="flex-end">
            <Button component={Link} to={`/work-orders/${w.id}`} variant="light" rightSection={<IconArrowUpRight size={14} />}>
              Open full page
            </Button>
          </Group>
        </Stack>
      )}
    </Drawer>
  );
}

export function WorkOrdersPage() {
  const { plant } = usePlant();
  const [bucket, setBucket] = useState("open");
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("due");
  const [sortDir, setSortDir] = useState<1 | -1>(1);
  const [openId, setOpenId] = useState<string | null>(null);

  const q = useQuery({
    queryKey: ["work-orders", plant?.id],
    queryFn: () =>
      unwrap(core.GET("/api/v1/work-orders", { params: { query: { plant_id: plant!.id, limit: 500, sort: "due" } } })),
    enabled: Boolean(plant),
    refetchInterval: 10_000,
  });
  const all = q.data?.items ?? [];

  // Real, derivable secondary stats (no fabricated "vs yesterday" trend: the API has no history/creation timestamp).
  const dueToday = useMemo(() => {
    const today = dayjs().format("YYYY-MM-DD");
    return all.filter((w) => !["COMPLETED", "CLOSED", "CANCELLED"].includes(w.status) && w.erp_basic_end === today).length;
  }, [all]);
  const oldestLateDays = useMemo(() => {
    const late = all.filter((w) => isLate(w.erp_basic_end, w.status));
    if (!late.length) return 0;
    return Math.max(...late.map((w) => dayjs().endOf("day").diff(dayjs(w.erp_basic_end).endOf("day"), "day")));
  }, [all]);

  const rows = useMemo(() => {
    const b = BUCKETS.find((x) => x.key === bucket)!;
    const s = search.trim().toLowerCase();
    return all.filter(
      (w) =>
        b.match(w) &&
        (!s ||
          w.erp_order_number.includes(s) ||
          (w.material_number ?? "").toLowerCase().includes(s) ||
          (w.material_description ?? "").toLowerCase().includes(s)),
    );
  }, [all, bucket, search]);

  const sortedRows = useMemo(() => {
    const val = SORT_VALUE[sortKey];
    return [...rows].sort((a, b) => {
      const va = val(a);
      const vb = val(b);
      const cmp = typeof va === "number" && typeof vb === "number" ? va - vb : String(va).localeCompare(String(vb));
      return cmp * sortDir;
    });
  }, [rows, sortKey, sortDir]);

  const toggleSort = (key: SortKey) => {
    if (key === sortKey) setSortDir((d) => (d === 1 ? -1 : 1));
    else {
      setSortKey(key);
      setSortDir(1);
    }
  };

  const exportCsv = () => {
    const header: Cell[] = ["Order", "Material", "Description", "Yield", "Planned", "UoM", "Ops confirmed", "Ops total", "Due", "Scheduled start", "Priority", "Status", "SAP"];
    const body: Cell[][] = sortedRows.map((w) => [
      w.erp_order_number, w.material_number, w.material_description, w.yield_qty, w.planned_qty, w.uom,
      w.ops_confirmed, w.op_count, w.erp_basic_end, w.sched_start, w.priority, w.status, w.erp_status,
    ]);
    downloadCsv(csvName(`work-orders-${bucket}`), [header, ...body]);
  };

  const sortProps = (id: SortKey, align?: "right") => ({ id, active: sortKey === id, dir: sortDir, onSort: toggleSort, align });

  return (
    <>
      <PageHeader
        title="Work orders"
        sub={`${all.length} orders from SAP · plant ${plant?.code ?? ""}`}
        right={
          <TextInput
            placeholder="Order, material…"
            leftSection={<IconSearch size={14} />}
            rightSection={search ? (
              <ActionIcon variant="subtle" color="gray" size="xs" onClick={() => setSearch("")} aria-label="Clear search">
                <IconX size={13} />
              </ActionIcon>
            ) : undefined}
            value={search}
            onChange={(e) => setSearch(e.currentTarget.value)}
            w={260}
            size="xs"
          />
        }
      />

      <SimpleGrid cols={{ base: 4, lg: 8 }} spacing={8} mb="md" className="rise">
        {BUCKETS.map((b) => {
          const count = all.filter(b.match).length;
          const hot = Boolean(b.alarm && count > 0);
          const pct = all.length ? Math.round((count / all.length) * 100) : 0;
          const sub = b.key === "open" && dueToday > 0
            ? <div className="kpi-sub">{dueToday} due today</div>
            : b.key === "late" && count > 0
              ? <div className="kpi-sub">oldest {oldestLateDays}d overdue</div>
              : null;
          return (
            <KpiTile key={b.key} b={b} count={count} pct={pct} selected={bucket === b.key} hot={hot} sub={sub}
              onClick={() => setBucket(b.key)} />
          );
        })}
      </SimpleGrid>

      <Group justify="space-between" mb={8} wrap="nowrap">
        <UpdatedAgo at={q.dataUpdatedAt} />
        <Group gap={8} wrap="nowrap">
          <ActionIcon variant="light" size="input-xs" loading={q.isFetching} onClick={() => q.refetch()} aria-label="Refresh">
            <IconRefresh size={14} />
          </ActionIcon>
          <ActionIcon variant="light" size="input-xs" disabled={sortedRows.length === 0} onClick={exportCsv} aria-label="Download CSV">
            <IconDownload size={14} />
          </ActionIcon>
        </Group>
      </Group>

      <div className="panel" style={{ maxHeight: 680, overflow: "auto" }}>
        {q.isPending ? (
          <Group p="xl" justify="center"><Loader size="sm" /></Group>
        ) : (
          <Table stickyHeader striped>
            <Table.Thead>
              <Table.Tr>
                <Sortable label="Order" {...sortProps("order")} />
                <Sortable label="Material" {...sortProps("material")} />
                <Table.Th w={170}><Sortable label="Quantity" {...sortProps("qty", "right")} /></Table.Th>
                <Sortable label="Ops" {...sortProps("ops", "right")} />
                <Sortable label="Due" {...sortProps("due")} />
                <Sortable label="Scheduled start" {...sortProps("sched")} />
                <Sortable label="Prio" {...sortProps("prio", "right")} />
                <Sortable label="Status" {...sortProps("status")} />
                <Sortable label="SAP" {...sortProps("sap")} />
                <Table.Th w={28} />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {sortedRows.map((w) => {
                const late = isLate(w.erp_basic_end, w.status);
                const pct = (Number(w.yield_qty) / Number(w.planned_qty)) * 100;
                return (
                  <Table.Tr
                    key={w.id}
                    className={`wo-row${late ? " row-late" : ""}`}
                    onClick={() => setOpenId(w.id)}
                    style={{ cursor: "pointer" }}
                  >
                    <Table.Td className="mono" fw={500}>
                      <Highlight highlight={search} size="sm" className="mono" fw={500}>{w.erp_order_number}</Highlight>
                    </Table.Td>
                    <Table.Td>
                      <Highlight highlight={search} size="sm" className="mono">{w.material_number ?? ""}</Highlight>
                      <Highlight highlight={search} size="xs" c="dimmed">{w.material_description ?? ""}</Highlight>
                    </Table.Td>
                    <Table.Td>
                      <Group gap={6} wrap="nowrap" justify="flex-end">
                        <Text size="xs" className="mono" w={70}>
                          {num(w.yield_qty)}/{num(w.planned_qty)} {w.uom}
                        </Text>
                        <Progress value={pct} size={4} w={70} color={pct >= 100 ? "green" : "blue"} bg="navy.1" />
                      </Group>
                    </Table.Td>
                    <Table.Td className="mono" style={{ textAlign: "right" }}>{w.ops_confirmed}/{w.op_count}</Table.Td>
                    <Table.Td>
                      <Text size="sm" className="mono" c={late ? "red.7" : undefined} fw={late ? 600 : undefined}>
                        {fmtDate(w.erp_basic_end)}
                      </Text>
                    </Table.Td>
                    <Table.Td className="mono">{fmtDateTime(w.sched_start)}</Table.Td>
                    <Table.Td style={{ textAlign: "right" }}><PrioBadge p={w.priority} /></Table.Td>
                    <Table.Td><WoStatus status={w.status} /></Table.Td>
                    <Table.Td><Text size="xs" className="mono" c="dimmed">{w.erp_status}</Text></Table.Td>
                    <Table.Td><IconChevronRight size={14} className="wo-row-arrow" /></Table.Td>
                  </Table.Tr>
                );
              })}
              {rows.length === 0 && (
                <Table.Tr>
                  <Table.Td colSpan={10}>
                    <Text c="dimmed" ta="center" py="lg" size="sm">
                      No work orders here. New SAP orders appear within a poll cycle (about 10 s).
                    </Text>
                  </Table.Td>
                </Table.Tr>
              )}
            </Table.Tbody>
          </Table>
        )}
      </div>

      <WorkOrderQuickView id={openId} onClose={() => setOpenId(null)} />
    </>
  );
}
