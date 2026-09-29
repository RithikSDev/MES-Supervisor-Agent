import {
  ActionIcon, Alert, Anchor, Badge, Button, Group, Modal, NumberInput, SegmentedControl, Select, SimpleGrid, Stack, Table,
  Tabs, Text, TextInput, Tooltip, UnstyledButton,
} from "@mantine/core";
import { type Location, type MovementRow, type ShortageRow, type StockRow, core, unwrap } from "@mes/api-client";
import {
  IconAlertOctagon, IconArrowsSort, IconArrowsTransferDown, IconBox, IconBuildingWarehouse, IconChevronDown, IconChevronUp,
  IconDownload, IconPackageImport, IconPackageOff, IconSearch, IconX, type Icon,
} from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link } from "react-router";
import { useCan } from "../auth";
import { notifications } from "@mantine/notifications";
import { PageHeader } from "../components/Layout";
import { StockStatus } from "../components/quality";
import { usePlant } from "../components/plant";
import { type Cell, csvName, downloadCsv } from "../lib/csv";
import { fmtDateTime, num } from "../lib/format";

const MOVEMENT_LABEL: Record<string, string> = {
  "561": "Opening balance",
  "501": "Receipt",
  "311": "Transfer",
  "261": "Issue to order",
  "262": "Return from order",
  "101": "Goods receipt (order)",
  "102": "GR reversal",
  "321": "QI → free (accepted)",
  "350": "QI → blocked (rejected)",
  "344": "Free → blocked",
  "343": "Blocked → free",
  "555": "Scrap from blocked",
  "122": "Return to supplier",
};

/** Clickable KPI tile: icon + colour-accented top border + hover lift (same look as Work Orders/Maintenance/Quality). */
function MatKpi({
  icon: Icon, label, value, color, hot, onClick,
}: { icon: Icon; label: string; value: number; color: string; hot?: boolean; onClick: () => void }) {
  return (
    <div className="panel kpi wo-kpi" style={{ borderTop: `3px solid ${color}` }} onClick={onClick} role="button" tabIndex={0}
      onKeyDown={(e) => e.key === "Enter" && onClick()}>
      <div className="wo-kpi-top"><span className="wo-kpi-icon" style={{ background: color }}><Icon size={13} stroke={2.2} /></span></div>
      <div className="label">{label}</div>
      <div className="value" style={{ color: hot ? color : undefined }}>{value}</div>
    </div>
  );
}

export function SyncBadge({ m }: { m: MovementRow }) {
  if (m.erp_sync_status === "POSTED")
    return (
      <Group gap={4} wrap="nowrap">
        <Text size="xs" className="mono" c="green.7">{m.erp_material_document}</Text>
        {m.erp_reconciled && <Badge size="xs" color="amber" variant="outline">reconciled</Badge>}
      </Group>
    );
  if (m.erp_sync_status === "PENDING") return <Text size="xs" c="amber.8" className="pulse">sending…</Text>;
  if (m.erp_sync_status === "FAILED")
    return <Tooltip label={m.erp_error ?? ""} multiline w={360}><Text size="xs" c="red.7">SAP rejected</Text></Tooltip>;
  return <Text size="xs" c="dimmed">local only</Text>;
}

type MoveSortKey = "posted" | "type" | "material" | "qty" | "order" | "by" | "sap";
const MOVE_SORT_VALUE: Record<MoveSortKey, (m: MovementRow) => number | string> = {
  posted: (m) => m.posted_at,
  type: (m) => m.movement_type,
  material: (m) => m.material_number ?? "",
  qty: (m) => Number(m.quantity),
  order: (m) => m.erp_order_number ?? "",
  by: (m) => m.posted_by_name ?? "",
  sap: (m) => m.erp_sync_status,
};

/** Reused by WorkOrderTabs for a single order's movements (read-only there: no sort/onSort passed). */
export function MovementsTable({
  rows, sort, onSort,
}: { rows: MovementRow[]; sort?: { key: MoveSortKey; dir: 1 | -1 }; onSort?: (key: MoveSortKey) => void }) {
  const Th = ({ id, children, align }: { id: MoveSortKey; children: React.ReactNode; align?: "right" }) => {
    if (!onSort) return <Table.Th style={align === "right" ? { textAlign: "right" } : undefined}>{children}</Table.Th>;
    const active = sort?.key === id;
    return (
      <Table.Th style={align === "right" ? { textAlign: "right" } : undefined}>
        <UnstyledButton onClick={() => onSort(id)} className="sortable-th">
          <Group gap={4} wrap="nowrap" justify={align === "right" ? "flex-end" : "flex-start"}>
            <span>{children}</span>
            {active ? (sort!.dir === 1 ? <IconChevronUp size={12} /> : <IconChevronDown size={12} />) : <IconArrowsSort size={12} opacity={0.35} />}
          </Group>
        </UnstyledButton>
      </Table.Th>
    );
  };
  return (
    <Table className="panel">
      <Table.Thead>
        <Table.Tr>
          <Th id="posted">Posted</Th>
          <Th id="type">Type</Th>
          <Th id="material">Material / lot</Th>
          <Th id="qty" align="right">Qty</Th>
          <Table.Th>From → to</Table.Th>
          <Th id="order">Order</Th>
          <Th id="by">By</Th>
          <Th id="sap">SAP document</Th>
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {rows.map((m) => (
          <Table.Tr key={m.id}>
            <Table.Td className="mono">{fmtDateTime(m.posted_at)}</Table.Td>
            <Table.Td>
              <Group gap={6} wrap="nowrap">
                <Text className="mono" size="sm">{m.movement_type}</Text>
                <Text size="xs" c="dimmed">{MOVEMENT_LABEL[m.movement_type]}</Text>
                {m.backflush && <Badge size="xs" variant="outline" color="navy.6">backflush</Badge>}
              </Group>
            </Table.Td>
            <Table.Td><span className="mono">{m.material_number}</span> <Text span size="xs" c="dimmed" className="mono">{m.lot_number}</Text></Table.Td>
            <Table.Td className="mono" style={{ textAlign: "right" }}>{num(m.quantity, 3)} {m.uom}</Table.Td>
            <Table.Td className="mono">{m.from_location ?? "·"} → {m.to_location ?? "·"}</Table.Td>
            <Table.Td>{m.work_order_id ? <Link to={`/work-orders/${m.work_order_id}`} className="mono" style={{ color: "inherit" }}>{m.erp_order_number ?? "order"}</Link> : ""}</Table.Td>
            <Table.Td><Text size="xs">{m.posted_by_name}</Text></Table.Td>
            <Table.Td><SyncBadge m={m} /></Table.Td>
          </Table.Tr>
        ))}
        {!rows.length && (
          <Table.Tr><Table.Td colSpan={8}><Text c="dimmed" ta="center" py="lg" size="sm">No movements.</Text></Table.Td></Table.Tr>
        )}
      </Table.Tbody>
    </Table>
  );
}

type TransferDraft = { lot_id: string; material: string; lot_number: string; from_location_id: string; max: number; to_location_id?: string; qty?: number };

type StockSortKey = "location" | "material" | "lot" | "origin" | "qty" | "status" | "since";
const STOCK_SORT_VALUE: Record<StockSortKey, (r: StockRow) => number | string> = {
  location: (r) => r.location_code,
  material: (r) => r.material_number,
  lot: (r) => r.lot_number,
  origin: (r) => r.lot_origin,
  qty: (r) => Number(r.quantity),
  status: (r) => r.status,
  since: (r) => r.received_at,
};
function StockTh({
  id, children, align, sort, onSort,
}: { id: StockSortKey; children: React.ReactNode; align?: "right"; sort: { key: StockSortKey; dir: 1 | -1 }; onSort: (id: StockSortKey) => void }) {
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

type ShortSortKey = "material" | "demand" | "available" | "shortage";
const SHORT_SORT_VALUE: Record<ShortSortKey, (r: ShortageRow) => number | string> = {
  material: (r) => r.material_number,
  demand: (r) => Number(r.demand),
  available: (r) => Number(r.available),
  shortage: (r) => Number(r.shortage),
};
function ShortTh({
  id, children, align, sort, onSort,
}: { id: ShortSortKey; children: React.ReactNode; align?: "right"; sort: { key: ShortSortKey; dir: 1 | -1 }; onSort: (id: ShortSortKey) => void }) {
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

export function MaterialsPage() {
  const { plant } = usePlant();
  const qc = useQueryClient();
  const canManage = useCan("inventory:manage");
  const [tab, setTab] = useState<string | null>("stock");
  const [search, setSearch] = useState("");
  const [kind, setKind] = useState("ALL");
  const [stockSort, setStockSort] = useState<{ key: StockSortKey; dir: 1 | -1 }>({ key: "location", dir: 1 });
  const [mSearch, setMSearch] = useState("");
  const [mSort, setMSort] = useState<{ key: MoveSortKey; dir: 1 | -1 }>({ key: "posted", dir: -1 });
  const [shSearch, setShSearch] = useState("");
  const [shSort, setShSort] = useState<{ key: ShortSortKey; dir: 1 | -1 }>({ key: "shortage", dir: -1 });
  const [transfer, setTransfer] = useState<TransferDraft | null>(null);
  const [receiving, setReceiving] = useState(false);
  const pid = plant?.id;

  const locations = useQuery({
    queryKey: ["locations", pid],
    queryFn: () => unwrap(core.GET("/api/v1/inventory/locations", { params: { query: { plant_id: pid! } } })),
    enabled: Boolean(pid),
  });
  const stock = useQuery({
    queryKey: ["stock", pid],
    queryFn: () => unwrap(core.GET("/api/v1/inventory/stock", { params: { query: { plant_id: pid! } } })),
    enabled: Boolean(pid),
    refetchInterval: 10_000,
  });
  const shortages = useQuery({
    queryKey: ["shortages", pid],
    queryFn: () => unwrap(core.GET("/api/v1/inventory/shortages", { params: { query: { plant_id: pid! } } })),
    enabled: Boolean(pid),
    refetchInterval: 15_000,
  });
  const movements = useQuery({
    queryKey: ["movements", pid],
    queryFn: () => unwrap(core.GET("/api/v1/inventory/movements", { params: { query: { plant_id: pid!, limit: 300 } } })),
    enabled: Boolean(pid),
    refetchInterval: 5_000,
  });
  const refresh = () => ["stock", "shortages", "movements"].forEach((k) => qc.invalidateQueries({ queryKey: [k, pid] }));

  const rows = useMemo(() => {
    const s = search.trim().toLowerCase();
    const filtered = (stock.data ?? []).filter(
      (r: StockRow) =>
        (kind === "ALL" || r.location_kind === kind) &&
        (!s || r.material_number.toLowerCase().includes(s) || r.material_description.toLowerCase().includes(s) || r.lot_number.toLowerCase().includes(s)),
    );
    const val = STOCK_SORT_VALUE[stockSort.key];
    return [...filtered].sort((a, b) => {
      const va = val(a);
      const vb = val(b);
      const cmp = typeof va === "number" && typeof vb === "number" ? va - vb : String(va).localeCompare(String(vb));
      return cmp * stockSort.dir;
    });
  }, [stock.data, search, kind, stockSort]);

  const sortedMovements = useMemo(() => {
    const s = mSearch.trim().toLowerCase();
    const filtered = (movements.data ?? []).filter(
      (m) =>
        !s ||
        (m.material_number ?? "").toLowerCase().includes(s) ||
        (m.lot_number ?? "").toLowerCase().includes(s) ||
        (m.erp_order_number ?? "").toLowerCase().includes(s) ||
        (m.posted_by_name ?? "").toLowerCase().includes(s),
    );
    const val = MOVE_SORT_VALUE[mSort.key];
    return [...filtered].sort((a, b) => {
      const va = val(a);
      const vb = val(b);
      const cmp = typeof va === "number" && typeof vb === "number" ? va - vb : String(va).localeCompare(String(vb));
      return cmp * mSort.dir;
    });
  }, [movements.data, mSearch, mSort]);

  const shortRows = useMemo(() => {
    const s = shSearch.trim().toLowerCase();
    const filtered = (shortages.data ?? []).filter(
      (r) => !s || r.material_number.toLowerCase().includes(s) || r.material_description.toLowerCase().includes(s),
    );
    const val = SHORT_SORT_VALUE[shSort.key];
    return [...filtered].sort((a, b) => {
      const va = val(a);
      const vb = val(b);
      const cmp = typeof va === "number" && typeof vb === "number" ? va - vb : String(va).localeCompare(String(vb));
      return cmp * shSort.dir;
    });
  }, [shortages.data, shSearch, shSort]);
  const toggleShSort = (key: ShortSortKey) =>
    setShSort((s) => (s.key === key ? { key, dir: s.dir === 1 ? -1 : 1 } : { key, dir: 1 }));

  const locs = locations.data ?? [];
  const lotCount = useMemo(() => new Set((stock.data ?? []).map((r) => r.lot_id)).size, [stock.data]);
  const locationCount = useMemo(() => new Set((stock.data ?? []).map((r) => r.location_id)).size, [stock.data]);
  const holdCount = useMemo(() => (stock.data ?? []).filter((r) => r.status !== "UNRESTRICTED").length, [stock.data]);
  const shortCount = (shortages.data ?? []).filter((r) => Number(r.shortage) > 0 || r.lines.some((l) => Number(l.gap) > 0)).length;

  const toggleStockSort = (key: StockSortKey) =>
    setStockSort((s) => (s.key === key ? { key, dir: s.dir === 1 ? -1 : 1 } : { key, dir: 1 }));
  const toggleMSort = (key: MoveSortKey) =>
    setMSort((s) => (s.key === key ? { key, dir: s.dir === 1 ? -1 : 1 } : { key, dir: 1 }));

  const exportStockCsv = () => {
    const header: Cell[] = ["Location", "Location kind", "Material", "Description", "Lot", "Origin", "Quantity", "UoM", "Stock type", "Since"];
    const body: Cell[][] = rows.map((r) => [
      r.location_code, r.location_kind, r.material_number, r.material_description, r.lot_number, r.lot_origin, r.quantity, r.uom, r.status, r.received_at,
    ]);
    downloadCsv(csvName("stock"), [header, ...body]);
  };

  const openReplenish = (r: ShortageRow, lineId: string | null, gap: number) => {
    const target = locs.find((l) => l.kind === "LINESIDE" && l.line_id === lineId);
    const source = (stock.data ?? []).find((st) => st.material_id === r.material_id && st.location_kind !== "LINESIDE" && st.location_kind !== "FG");
    if (!source || !target) return;
    setTransfer({ lot_id: source.lot_id, material: r.material_number, lot_number: source.lot_number, from_location_id: source.location_id,
      max: Number(source.quantity), to_location_id: target.id, qty: Math.min(gap, Number(source.quantity)) });
  };

  return (
    <>
      <PageHeader
        title="Materials"
        sub="Stock by lot and location, open demand, and every goods movement posted to SAP"
        right={canManage && <Button leftSection={<IconPackageImport size={14} />} onClick={() => setReceiving(true)}>Receive material</Button>}
      />

      <SimpleGrid cols={{ base: 2, md: 4 }} mb="md" className="rise">
        <MatKpi icon={IconBox} label="Lots" value={lotCount} color="var(--mes-blue)" onClick={() => setTab("stock")} />
        <MatKpi icon={IconBuildingWarehouse} label="Locations" value={locationCount} color="var(--mes-cyan)" onClick={() => setTab("stock")} />
        <MatKpi icon={IconAlertOctagon} label="Blocked / in QI" value={holdCount} color="var(--mes-amber)" hot={holdCount > 0} onClick={() => setTab("stock")} />
        <MatKpi icon={IconPackageOff} label="Shortages" value={shortCount} color="var(--mes-red)" hot={shortCount > 0} onClick={() => setTab("shortages")} />
      </SimpleGrid>

      <Tabs value={tab} onChange={setTab}>
        <Tabs.List>
          <Tabs.Tab value="stock">Stock</Tabs.Tab>
          <Tabs.Tab value="shortages" c={shortCount ? "amber.8" : undefined}>Shortages {shortCount ? `(${shortCount})` : ""}</Tabs.Tab>
          <Tabs.Tab value="movements">Movements</Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="stock" pt="sm">
          {shortCount > 0 && (
            <Alert color="amber" variant="light" mb="xs" py={6}>
              <Group justify="space-between" wrap="nowrap">
                <Text size="sm">{shortCount} material{shortCount === 1 ? " is" : "s are"} short of demand somewhere in the plant.</Text>
                <Anchor size="sm" fw={600} onClick={() => setTab("shortages")}>View shortages →</Anchor>
              </Group>
            </Alert>
          )}
          <Group mb="xs" justify="space-between">
            <Group gap="xs">
              <SegmentedControl size="xs" value={kind} onChange={setKind}
                data={[{ value: "ALL", label: "All" }, { value: "STORE", label: "Stores" }, { value: "LINESIDE", label: "Line-side" }, { value: "SEMI", label: "Semi-finished" }, { value: "FG", label: "Finished goods" }]} />
              <TextInput size="xs" w={260} leftSection={<IconSearch size={14} />}
                rightSection={search ? (
                  <ActionIcon variant="subtle" color="gray" size="xs" onClick={() => setSearch("")} aria-label="Clear search"><IconX size={12} /></ActionIcon>
                ) : undefined}
                placeholder="Material, lot…" value={search} onChange={(e) => setSearch(e.currentTarget.value)} />
            </Group>
            <ActionIcon variant="light" size="input-xs" disabled={!rows.length} onClick={exportStockCsv} aria-label="Download CSV"><IconDownload size={14} /></ActionIcon>
          </Group>
          <Table className="panel">
            <Table.Thead>
              <Table.Tr>
                <StockTh id="location" sort={stockSort} onSort={toggleStockSort}>Location</StockTh>
                <StockTh id="material" sort={stockSort} onSort={toggleStockSort}>Material</StockTh>
                <StockTh id="lot" sort={stockSort} onSort={toggleStockSort}>Lot</StockTh>
                <StockTh id="origin" sort={stockSort} onSort={toggleStockSort}>Origin</StockTh>
                <StockTh id="qty" align="right" sort={stockSort} onSort={toggleStockSort}>Quantity</StockTh>
                <StockTh id="status" sort={stockSort} onSort={toggleStockSort}>Stock type</StockTh>
                <StockTh id="since" sort={stockSort} onSort={toggleStockSort}>Since</StockTh>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {rows.map((r) => (
                <Table.Tr key={`${r.location_id}-${r.lot_id}-${r.status}`}>
                  <Table.Td><span className="mono">{r.location_code}</span> <Text span size="xs" c="dimmed">{r.location_kind.toLowerCase()}</Text></Table.Td>
                  <Table.Td><span className="mono">{r.material_number}</span> <Text span size="xs" c="dimmed">{r.material_description}</Text></Table.Td>
                  <Table.Td className="mono"><Link to={`/traceability?lot=${r.lot_id}`} style={{ color: "inherit" }}>{r.lot_number}</Link></Table.Td>
                  <Table.Td><Text size="xs" c="dimmed">{r.lot_origin.toLowerCase()}</Text></Table.Td>
                  <Table.Td ta="right" className="mono">{num(r.quantity, 3)} {r.uom}</Table.Td>
                  <Table.Td><StockStatus status={r.status} /></Table.Td>
                  <Table.Td className="mono">{fmtDateTime(r.received_at)}</Table.Td>
                  <Table.Td ta="right">
                    {canManage && r.status === "UNRESTRICTED" && (
                      <Button size="compact-xs" variant="subtle" leftSection={<IconArrowsTransferDown size={12} />}
                        onClick={() => setTransfer({ lot_id: r.lot_id, material: r.material_number, lot_number: r.lot_number, from_location_id: r.location_id, max: Number(r.quantity) })}>
                        Move
                      </Button>
                    )}
                  </Table.Td>
                </Table.Tr>
              ))}
              {!rows.length && (
                <Table.Tr><Table.Td colSpan={8}><Text c="dimmed" ta="center" py="lg" size="sm">No stock matches this filter.</Text></Table.Td></Table.Tr>
              )}
            </Table.Tbody>
          </Table>
        </Tabs.Panel>

        <Tabs.Panel value="shortages" pt="sm">
          <Text size="xs" c="dimmed" mb="xs">Remaining component demand of scheduled, dispatched and running orders against stock. Line gaps show what is missing at the line itself.</Text>
          <Group mb="xs" justify="space-between">
            <TextInput size="xs" w={260} leftSection={<IconSearch size={14} />}
              rightSection={shSearch ? (
                <ActionIcon variant="subtle" color="gray" size="xs" onClick={() => setShSearch("")} aria-label="Clear search"><IconX size={12} /></ActionIcon>
              ) : undefined}
              placeholder="Material…" value={shSearch} onChange={(e) => setShSearch(e.currentTarget.value)} />
            <ActionIcon variant="light" size="input-xs" disabled={!shortRows.length} onClick={() => {
              const header: Cell[] = ["Material", "Description", "Demand", "In stock", "Plant shortage", "Line gaps", "Orders"];
              const body: Cell[][] = shortRows.map((r) => [
                r.material_number, r.material_description, r.demand, r.available, r.shortage,
                r.lines.map((l) => `${l.line_code}:${l.gap}`).join(" "), r.orders.join(" "),
              ]);
              downloadCsv(csvName("shortages"), [header, ...body]);
            }} aria-label="Download CSV"><IconDownload size={14} /></ActionIcon>
          </Group>
          <Table className="panel">
            <Table.Thead>
              <Table.Tr>
                <ShortTh id="material" sort={shSort} onSort={toggleShSort}>Material</ShortTh>
                <ShortTh id="demand" align="right" sort={shSort} onSort={toggleShSort}>Demand</ShortTh>
                <ShortTh id="available" align="right" sort={shSort} onSort={toggleShSort}>In stock</ShortTh>
                <ShortTh id="shortage" align="right" sort={shSort} onSort={toggleShSort}>Plant shortage</ShortTh>
                <Table.Th miw={220}>Line-side gaps</Table.Th>
                <Table.Th>Orders</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {shortRows.map((r) => (
                <Table.Tr key={r.material_id}>
                  <Table.Td><span className="mono">{r.material_number}</span> <Text span size="xs" c="dimmed">{r.material_description}</Text></Table.Td>
                  <Table.Td ta="right" className="mono">{num(r.demand, 3)} {r.uom}</Table.Td>
                  <Table.Td ta="right" className="mono">{num(r.available, 3)}</Table.Td>
                  <Table.Td ta="right" className="mono" c={Number(r.shortage) > 0 ? "red.7" : "dimmed"} fw={Number(r.shortage) > 0 ? 600 : undefined}>
                    {Number(r.shortage) > 0 ? num(r.shortage, 3) : "—"}
                  </Table.Td>
                  <Table.Td>
                    <Group gap={6}>
                      {r.lines.map((l) => (
                        <Group key={l.line_code} gap={4} wrap="nowrap">
                          <Text size="xs" className="mono" c={Number(l.gap) > 0 ? "amber.8" : "dimmed"}>
                            {l.line_code}: {Number(l.gap) > 0 ? `-${num(l.gap, 3)}` : "ok"}
                          </Text>
                          {canManage && Number(l.gap) > 0 && (
                            <Button size="compact-xs" variant="light" style={{ flexShrink: 0 }} onClick={() => openReplenish(r, l.line_id ?? null, Number(l.gap))}>Replenish</Button>
                          )}
                        </Group>
                      ))}
                    </Group>
                  </Table.Td>
                  <Table.Td><Text size="xs" className="mono" c="dimmed" lineClamp={1}>{r.orders.join(", ")}</Text></Table.Td>
                </Table.Tr>
              ))}
              {!shortRows.length && (
                <Table.Tr><Table.Td colSpan={6}><Text c="dimmed" ta="center" py="lg" size="sm">
                  {(shortages.data ?? []).length ? "No shortages match this filter." : "No open demand right now."}
                </Text></Table.Td></Table.Tr>
              )}
            </Table.Tbody>
          </Table>
        </Tabs.Panel>

        <Tabs.Panel value="movements" pt="sm">
          <Group mb="xs" justify="space-between">
            <TextInput size="xs" w={280} leftSection={<IconSearch size={14} />}
              rightSection={mSearch ? (
                <ActionIcon variant="subtle" color="gray" size="xs" onClick={() => setMSearch("")} aria-label="Clear search"><IconX size={12} /></ActionIcon>
              ) : undefined}
              placeholder="Material, lot, order, posted by…" value={mSearch} onChange={(e) => setMSearch(e.currentTarget.value)} />
            <ActionIcon variant="light" size="input-xs" disabled={!sortedMovements.length} onClick={() => {
              const header: Cell[] = ["Posted", "Type", "Material", "Lot", "Quantity", "UoM", "From", "To", "Order", "By", "SAP status", "SAP document"];
              const body: Cell[][] = sortedMovements.map((m) => [
                m.posted_at, m.movement_type, m.material_number, m.lot_number, m.quantity, m.uom, m.from_location, m.to_location,
                m.erp_order_number, m.posted_by_name, m.erp_sync_status, m.erp_material_document,
              ]);
              downloadCsv(csvName("movements"), [header, ...body]);
            }} aria-label="Download CSV"><IconDownload size={14} /></ActionIcon>
          </Group>
          <MovementsTable rows={sortedMovements} sort={mSort} onSort={toggleMSort} />
        </Tabs.Panel>
      </Tabs>

      {transfer && <TransferModal draft={transfer} locations={locs} onClose={() => setTransfer(null)} onDone={() => { setTransfer(null); refresh(); }} />}
      {receiving && <ReceiveModal locations={locs} onClose={() => setReceiving(false)} onDone={() => { setReceiving(false); refresh(); }} />}
    </>
  );
}

function TransferModal({ draft, locations, onClose, onDone }: { draft: TransferDraft; locations: Location[]; onClose: () => void; onDone: () => void }) {
  const { plant } = usePlant();
  const [to, setTo] = useState<string | null>(draft.to_location_id ?? null);
  const [qty, setQty] = useState<number>(draft.qty ?? draft.max);
  const m = useMutation({
    mutationFn: () =>
      unwrap(core.POST("/api/v1/inventory/transfers", {
        body: { plant_id: plant!.id, lot_id: draft.lot_id, quantity: String(qty), from_location_id: draft.from_location_id, to_location_id: to! },
      })),
    onSuccess: onDone,
  });
  return (
    <Modal opened onClose={onClose} centered title={<Text fw={600}>Transfer {draft.material} · lot {draft.lot_number} (311)</Text>}>
      <Stack>
        <Select label="To location" data={locations.filter((l) => l.id !== draft.from_location_id).map((l) => ({ value: l.id, label: `${l.code} · ${l.name}` }))} value={to} onChange={setTo} />
        <NumberInput label={`Quantity (max ${draft.max})`} min={0.001} max={draft.max} value={qty} onChange={(v) => setQty(Number(v) || 0)} />
        <Text size="xs" c="dimmed">Posted to SAP as a transfer posting (movement type 311).</Text>
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button loading={m.isPending} disabled={!to || qty <= 0 || qty > draft.max} onClick={() => m.mutate()}>Transfer</Button>
        </Group>
      </Stack>
    </Modal>
  );
}

function ReceiveModal({ locations, onClose, onDone }: { locations: Location[]; onClose: () => void; onDone: () => void }) {
  const { plant } = usePlant();
  const materials = useQuery({ queryKey: ["materials-all"], queryFn: () => unwrap(core.GET("/api/v1/materials", { params: { query: { limit: 500 } } })) });
  const [material, setMaterial] = useState<string | null>(null);
  const [location, setLocation] = useState<string | null>(locations.find((l) => l.kind === "STORE")?.id ?? null);
  const [qty, setQty] = useState<number>(100);
  const [reason, setReason] = useState("");
  const m = useMutation({
    mutationFn: () =>
      unwrap(core.POST("/api/v1/inventory/receipts", {
        body: { plant_id: plant!.id, material_id: material!, quantity: String(qty), location_id: location!, reason: reason || null },
      })),
    onSuccess: (r) => {
      if (r.inspection_lot_number)
        notifications.show({ color: "cyan", title: `Lot ${r.lot_number} is in quality inspection`,
          message: `Incoming inspection ${r.inspection_lot_number} opened. The lot can be used once its usage decision releases it.` });
      onDone();
    },
  });
  return (
    <Modal opened onClose={onClose} centered title={<Text fw={600}>Receive material (501)</Text>}>
      <Stack>
        <Select searchable label="Material" data={(materials.data ?? []).map((x) => ({ value: x.id, label: `${x.material_number} · ${x.description}` }))} value={material} onChange={setMaterial} />
        <Select label="Into location" data={locations.map((l) => ({ value: l.id, label: `${l.code} · ${l.name}` }))} value={location} onChange={setLocation} />
        <NumberInput label="Quantity" min={0.001} value={qty} onChange={(v) => setQty(Number(v) || 0)} />
        <TextInput label="Reference / reason" placeholder="Delivery note…" value={reason} onChange={(e) => setReason(e.currentTarget.value)} />
        <Text size="xs" c="dimmed">Creates a new lot and posts a goods receipt without order to SAP (movement type 501).</Text>
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button loading={m.isPending} disabled={!material || !location || qty <= 0} onClick={() => m.mutate()}>Receive</Button>
        </Group>
      </Stack>
    </Modal>
  );
}
