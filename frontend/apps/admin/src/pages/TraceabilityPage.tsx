import {
  ActionIcon, Alert, Badge, Button, Collapse, Group, Loader, SegmentedControl, Stack, Table, Text, TextInput, Tooltip,
  UnstyledButton,
} from "@mantine/core";
import { ApiError, type QuarantinedLot, type TraceHit, type TraceNode, core, unwrap } from "@mes/api-client";
import {
  IconArrowBackUp, IconArrowForwardUp, IconArrowsMaximize, IconArrowsMinimize, IconBarcode, IconBox, IconChevronDown,
  IconChevronRight, IconClipboardList, IconDownload, IconLock, IconSearch,
} from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { useCan } from "../auth";
import { WoStatus } from "../components/badges";
import { SignatureModal } from "../components/dialogs";
import { PageHeader } from "../components/Layout";
import { usePlant } from "../components/plant";
import { InspStatus, StockStatus, TYPE_LABEL } from "../components/quality";
import { type Cell, csvName, downloadCsv } from "../lib/csv";
import { fmtDateTime, num } from "../lib/format";

type Dir = "backward" | "forward";

function useDebounced<T>(v: T, ms = 250): T {
  const [d, setD] = useState(v);
  useEffect(() => {
    const t = setTimeout(() => setD(v), ms);
    return () => clearTimeout(t);
  }, [v, ms]);
  return d;
}

const HIT_ICON = { LOT: IconBox, SERIAL: IconBarcode, ORDER: IconClipboardList } as const;
/** Same colours as .trace-card.lot / .trace-card.order below, so the picker and the tree read as one system. */
const HIT_COLOR: Record<string, string> = { LOT: "var(--mes-cyan)", ORDER: "var(--mes-blue)", SERIAL: "var(--mes-amber)" };

/** Every lot/order reachable in the trace, walked once: for the summary stat line and the CSV export. */
function traceStats(root: TraceNode): { lots: number; orders: number; maxDepth: number } {
  const lots = new Set<string>();
  const orders = new Set<string>();
  let maxDepth = 0;
  const walk = (n: TraceNode, depth: number) => {
    (n.type === "LOT" ? lots : orders).add(n.id);
    maxDepth = Math.max(maxDepth, depth);
    n.children.forEach((c) => walk(c, depth + 1));
  };
  walk(root, 0);
  return { lots: lots.size, orders: orders.size, maxDepth };
}

function flattenForCsv(n: TraceNode, depth = 0, parentLabel = ""): Cell[][] {
  const row: Cell[] = [
    depth, n.type, n.label, n.material_number, n.material_description, n.quantity, n.uom,
    n.type === "LOT" ? (n.lot?.origin ?? "") : (n.order?.status ?? ""), parentLabel,
  ];
  return [row, ...n.children.flatMap((c) => flattenForCsv(c, depth + 1, n.label))];
}

export function TraceabilityPage() {
  const { plant } = usePlant();
  const pid = plant?.id;
  const [params, setParams] = useSearchParams();
  const [text, setText] = useState("");
  const q = useDebounced(text.trim());
  const dir = (params.get("dir") as Dir) ?? "backward";
  const lot = params.get("lot");
  const order = params.get("order");
  const serial = params.get("serial");
  const canQuarantine = useCan("quality:decide");
  const [signing, setSigning] = useState(false);
  const [blocked, setBlocked] = useState<QuarantinedLot[] | null>(null);
  const [expandSignal, setExpandSignal] = useState<{ open: boolean } | null>(null);
  const qc = useQueryClient();

  const hits = useQuery({
    queryKey: ["trace-search", pid, q],
    queryFn: () => unwrap(core.GET("/api/v1/trace/search", { params: { query: { plant_id: pid!, q } } })),
    enabled: Boolean(pid) && q.length >= 2,
  });
  const trace = useQuery({
    queryKey: ["trace", lot, order, serial, dir],
    queryFn: () =>
      serial
        ? unwrap(core.GET("/api/v1/trace/serials/{serial}", { params: { path: { serial }, query: { plant_id: pid!, direction: dir } } }))
        : lot
          ? unwrap(core.GET("/api/v1/trace/lots/{lot_id}", { params: { path: { lot_id: lot }, query: { direction: dir } } }))
          : unwrap(core.GET("/api/v1/trace/orders/{wo_id}", { params: { path: { wo_id: order! }, query: { direction: dir } } })),
    enabled: Boolean(pid && (lot || order || serial)),
  });
  const pick = (h: TraceHit) => {
    setBlocked(null);
    setParams(h.type === "SERIAL" ? { serial: h.label, dir } : h.type === "LOT" ? { lot: h.id, dir } : { order: h.id, dir });
  };
  const setDir = (d: string) => setParams((p) => { p.set("dir", d); return p; });

  const root = trace.data?.root;
  const stats = useMemo(() => (root ? traceStats(root) : null), [root]);
  const affected = useMemo(() => (root && dir === "forward" ? flattenLots(root).filter((n) => freeQty(n) > 0) : []), [root, dir]);
  const exportCsv = () => {
    if (!root) return;
    const header: Cell[] = ["Depth", "Type", "Label", "Material", "Description", "Quantity", "UoM", "Status / origin", "Parent"];
    downloadCsv(csvName(`trace-${root.label}`), [header, ...flattenForCsv(root)]);
  };
  const quarantine = useMutation({
    mutationFn: (reason: string) =>
      unwrap(core.POST("/api/v1/trace/lots/{lot_id}/quarantine-affected", { params: { path: { lot_id: root!.id } }, body: { reason, include_root: true } })),
    meta: { silent: true },
    onSuccess: (r) => {
      setSigning(false);
      setBlocked(r);
      qc.invalidateQueries({ queryKey: ["trace"] });
    },
  });

  return (
    <>
      <PageHeader title="Traceability" sub="Where did it come from, where did it go: lots, serial numbers and orders with machines, operators, inspections and nonconformances" />
      <Group align="flex-start" gap="lg" wrap="nowrap">
        <Stack w={300} gap="xs" style={{ flexShrink: 0 }}>
          <TextInput leftSection={<IconSearch size={14} />} placeholder="Lot, serial or order number" value={text} onChange={(e) => setText(e.currentTarget.value)} data-autofocus />
          {hits.isFetching && <Loader size="xs" />}
          <div className="panel" style={{ maxHeight: 560, overflow: "auto" }}>
            {(hits.data ?? []).map((h) => {
              const Icon = HIT_ICON[h.type];
              const active = (h.type === "SERIAL" && serial === h.label) || (h.type === "LOT" && lot === h.id) || (h.type === "ORDER" && order === h.id);
              return (
                <UnstyledButton key={`${h.type}${h.id}`} onClick={() => pick(h)} w="100%" p={8}
                  style={{
                    borderBottom: "1px solid var(--mes-line)",
                    borderLeft: `3px solid ${HIT_COLOR[h.type]}`,
                    background: active ? "rgba(31,95,209,0.07)" : undefined,
                  }}>
                  <Group gap={8} wrap="nowrap">
                    <Icon size={16} color="var(--mes-text-dim)" />
                    <div>
                      <Text className="mono" size="sm" fw={600}>{h.label}</Text>
                      <Text size="xs" c="dimmed">{h.type.toLowerCase()} · {h.material_number}</Text>
                    </div>
                  </Group>
                </UnstyledButton>
              );
            })}
            {q.length >= 2 && hits.data?.length === 0 && <Text size="sm" c="dimmed" p="sm">No match.</Text>}
            {q.length < 2 && <Text size="xs" c="dimmed" p="sm">Type at least two characters, e.g. a lot (L…), a serial number (FG-1001-…) or an order number.</Text>}
          </div>
        </Stack>

        <Stack style={{ flex: 1, minWidth: 0 }}>
          {(lot || order || serial) && (
            <Group justify="space-between" wrap="wrap">
              <Group gap="sm" wrap="wrap">
                <SegmentedControl value={dir} onChange={setDir} data={[
                  { value: "backward", label: <Group gap={6} wrap="nowrap"><IconArrowBackUp size={14} />Backward (where from)</Group> },
                  { value: "forward", label: <Group gap={6} wrap="nowrap"><IconArrowForwardUp size={14} />Forward (where to)</Group> },
                ]} />
                {serial && <Badge size="lg" variant="outline" color="amber" leftSection={<IconBarcode size={12} />}>{serial}</Badge>}
                {stats && (
                  <Text size="xs" c="dimmed" className="mono">
                    {stats.lots} lot{stats.lots === 1 ? "" : "s"} · {stats.orders} order{stats.orders === 1 ? "" : "s"} · {stats.maxDepth + 1} level{stats.maxDepth ? "s" : ""} deep
                  </Text>
                )}
              </Group>
              {root && (
                <Group gap={6}>
                  <Tooltip label="Expand all"><ActionIcon variant="light" size="input-xs" onClick={() => setExpandSignal({ open: true })}><IconArrowsMaximize size={14} /></ActionIcon></Tooltip>
                  <Tooltip label="Collapse all"><ActionIcon variant="light" size="input-xs" onClick={() => setExpandSignal({ open: false })}><IconArrowsMinimize size={14} /></ActionIcon></Tooltip>
                  <Tooltip label="Export trace as CSV"><ActionIcon variant="light" size="input-xs" onClick={exportCsv}><IconDownload size={14} /></ActionIcon></Tooltip>
                </Group>
              )}
            </Group>
          )}
          {trace.isPending && (lot || order || serial) && <Loader size="sm" />}
          {trace.error && <Alert color="red" variant="light">{trace.error.message}</Alert>}
          {!lot && !order && !serial && <Text c="dimmed" size="sm">Search on the left and pick a lot, serial number or order.</Text>}

          {root && dir === "forward" && root.type === "LOT" && (
            <div className="panel" style={{ padding: 12 }}>
              <Group justify="space-between">
                <div>
                  <Text fw={600}>{affected.length} lot{affected.length === 1 ? "" : "s"} with usable stock in this trace</Text>
                  <Text size="xs" c="dimmed">Quarantine blocks their free and in-inspection stock (344/350) under one nonconformance per lot.</Text>
                </div>
                {canQuarantine && <Button color="red" variant="light" leftSection={<IconLock size={14} />} disabled={!affected.length} onClick={() => setSigning(true)}>Quarantine affected lots…</Button>}
              </Group>
              {blocked && (
                <Alert mt="sm" color="amber" variant="light" title={`${blocked.length} lot(s) blocked`}>
                  {blocked.map((b) => <Text key={b.lot_id} size="sm" className="mono">{b.lot_number} · {b.material_number} · {num(b.blocked_qty, 3)} {b.uom} → {b.nc_number}</Text>)}
                  <Link to="/quality?tab=ncs" style={{ color: "inherit", fontSize: 13 }}>Open nonconformances →</Link>
                </Alert>
              )}
            </div>
          )}
          {root && <div className="trace-tree"><NodeView node={root} depth={0} expandSignal={expandSignal} /></div>}
        </Stack>
      </Group>
      <SignatureModal
        opened={signing}
        meaning="APPROVED"
        subject={`Quarantine of ${affected.length} lot(s) traced forward from ${root?.label}: ${affected.map((a) => a.label).join(", ")}`}
        busy={quarantine.isPending}
        error={quarantine.error instanceof ApiError ? quarantine.error.message : null}
        onClose={() => { setSigning(false); quarantine.reset(); }}
        onSign={(r) => quarantine.mutate(r)}
      />
    </>
  );
}

function flattenLots(n: TraceNode, seen = new Set<string>()): TraceNode[] {
  const out: TraceNode[] = [];
  if (n.type === "LOT" && !seen.has(n.id)) {
    seen.add(n.id);
    out.push(n);
  }
  for (const c of n.children) out.push(...flattenLots(c, seen));
  return out;
}

const freeQty = (n: TraceNode) => (n.lot?.stock ?? []).filter((s) => s.status !== "BLOCKED").reduce((a, s) => a + Number(s.quantity), 0);

function NodeView({ node, depth, expandSignal }: { node: TraceNode; depth: number; expandSignal: { open: boolean } | null }) {
  const [open, setOpen] = useState(depth < 1);
  const [kidsOpen, setKidsOpen] = useState(true);
  useEffect(() => {
    if (!expandSignal) return;
    setOpen(expandSignal.open);
    setKidsOpen(expandSignal.open);
  }, [expandSignal]);
  const Icon = node.type === "LOT" ? IconBox : IconClipboardList;
  const openNcs = node.nonconformances.filter((n) => n.status !== "CLOSED");
  return (
    <div className="trace-node" style={{ marginLeft: depth ? 22 : 0 }}>
      <div className={`panel trace-card ${node.type === "LOT" ? "lot" : "order"}`}>
        <Group justify="space-between" wrap="nowrap" align="flex-start">
          <Group gap={10} wrap="nowrap" align="flex-start">
            {node.children.length > 0 ? (
              <UnstyledButton onClick={() => setKidsOpen((v) => !v)} mt={2}>
                {kidsOpen ? <IconChevronDown size={16} /> : <IconChevronRight size={16} />}
              </UnstyledButton>
            ) : <span style={{ width: 16 }} />}
            <Icon size={18} color={node.type === "LOT" ? "var(--mes-cyan)" : "var(--mes-amber)"} style={{ marginTop: 2 }} />
            <div>
              <Group gap={8}>
                {node.type === "ORDER"
                  ? <Link to={`/work-orders/${node.id}`} className="mono" style={{ color: "inherit", fontWeight: 700 }}>{node.label}</Link>
                  : <Text className="mono" fw={700}>{node.label}</Text>}
                <Text size="sm"><span className="mono">{node.material_number}</span> <Text span c="dimmed" size="xs">{node.material_description}</Text></Text>
                {node.order && <WoStatus status={node.order.status} />}
                {node.lot && <Badge size="xs" variant="outline" color="navy.6">{node.lot.origin.toLowerCase()}</Badge>}
              </Group>
              <Group gap="md" mt={4}>
                {node.quantity != null && <Text size="xs" c="dimmed">{depth ? (node.type === "LOT" ? "qty on link" : "consumed") : ""} <span className="mono" style={{ color: "var(--mes-text)" }}>{num(node.quantity, 3)} {node.uom}</span></Text>}
                {node.lot && node.lot.stock.map((s, i) => (
                  <Group key={i} gap={4}><Text size="xs" className="mono">{s.location_code} {num(s.quantity, 3)}</Text><StockStatus status={s.status} /></Group>
                ))}
                {node.lot && node.lot.stock.length === 0 && <Text size="xs" c="dimmed">no stock left</Text>}
                {node.order && <Text size="xs" c="dimmed">good <span className="mono">{num(node.order.yield_qty)}</span> / scrap <span className="mono">{num(node.order.scrap_qty)}</span> of {num(node.order.planned_qty)}</Text>}
                {node.lot && node.lot.serial_count > 0 && <Text size="xs" c="dimmed"><IconBarcode size={11} /> {node.lot.serial_count} serial{node.lot.serial_count > 1 ? "s" : ""}</Text>}
              </Group>
            </div>
          </Group>
          <Group gap={4} wrap="nowrap">
            {node.inspections.map((i) => (
              <Link key={i.id} to={`/quality?lot=${i.id}`} style={{ textDecoration: "none" }} title={`${TYPE_LABEL[i.inspection_type]} ${i.number}`}>
                <InspStatus status={i.status} decision={i.usage_decision} />
              </Link>
            ))}
            {openNcs.map((n) => <Link key={n.id} to={`/quality?tab=ncs&nc=${n.id}`} style={{ textDecoration: "none" }}><Badge size="sm" color="amber" variant="light">{n.number}</Badge></Link>)}
            {node.truncated && node.children.length === 0 && <Badge size="xs" variant="outline" color="navy.6" title="Shown elsewhere in this tree">↺</Badge>}
            <UnstyledButton onClick={() => setOpen((v) => !v)}><Text size="xs" c="dimmed">{open ? "less" : "details"}</Text></UnstyledButton>
          </Group>
        </Group>
        <Collapse in={open}>
          <div style={{ marginTop: 8, paddingLeft: 44 }}>
            {node.order && (
              <Table withRowBorders={false} verticalSpacing={2} fz="xs">
                <Table.Tbody>
                  {node.order.operations.map((o) => (
                    <Table.Tr key={o.op_no}>
                      <Table.Td className="mono" w={50}>{o.op_no}</Table.Td>
                      <Table.Td w={220}>{o.description} <Text span c="dimmed" size="xs" className="mono">{o.work_center}</Text></Table.Td>
                      <Table.Td>
                        {o.steps.length === 0 ? <Text size="xs" c="dimmed">not started</Text> : o.steps.map((s, i) => (
                          <Text key={i} size="xs"><span className="mono">{s.equipment_code ?? "—"}</span> · {s.operator_name} · <span className="mono">{fmtDateTime(s.started_at)}</span>
                            {s.ended_at && s.ended_at !== s.started_at && <> → <span className="mono">{fmtDateTime(s.ended_at)}</span></>} · {num(s.yield_qty)} good{Number(s.scrap_qty) ? `, ${num(s.scrap_qty)} scrap` : ""}</Text>
                        ))}
                      </Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            )}
            {node.lot && (
              <Text size="xs" c="dimmed">Created {fmtDateTime(node.lot.created_at)}{node.lot.received_qty ? ` · received ${num(node.lot.received_qty, 3)} ${node.uom}` : ""}</Text>
            )}
            {node.lot && node.lot.serials.length > 0 && (
              <Group gap={4} mt={4}>{node.lot.serials.map((s) => <Badge key={s} size="xs" variant="outline" color="navy.6" className="mono">{s}</Badge>)}</Group>
            )}
            {node.nonconformances.map((n) => (
              <Text key={n.id} size="xs" mt={4}><span className="mono">{n.number}</span> <Text span c={n.status === "CLOSED" ? "dimmed" : "amber.8"} inherit>{n.status.toLowerCase()}</Text> · {n.description}{n.disposition ? ` → ${n.disposition.toLowerCase().replaceAll("_", " ")}` : ""}</Text>
            ))}
          </div>
        </Collapse>
      </div>
      <Collapse in={kidsOpen}>
        {node.children.map((c) => <NodeView key={`${c.type}${c.id}`} node={c} depth={depth + 1} expandSignal={expandSignal} />)}
      </Collapse>
    </div>
  );
}
