import {
  ActionIcon, Badge, Button, Group, Modal, NumberInput, SegmentedControl, Select, SimpleGrid, Stack, Switch, Table, Tabs,
  Text, TextInput, UnstyledButton,
} from "@mantine/core";
import { type Characteristic, type InspectionLot, type InspectionPlan, type Nonconformance, type PlanIn, core, unwrap } from "@mes/api-client";
import {
  IconAlertCircle, IconArrowsSort, IconChevronDown, IconChevronUp, IconClipboardCheck, IconFlame, IconPencil, IconPlus,
  IconScale, IconSearch, IconTrash, IconX, type Icon,
} from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { useCan } from "../auth";
import { PageHeader } from "../components/Layout";
import { usePlant } from "../components/plant";
import {
  InspStatus, InspectionLotDrawer, NcDrawer, NcStatus, Severity, TYPE_LABEL, label, useQualityMeta,
} from "../components/quality";
import { fmtDateTime, fromNow, num } from "../lib/format";

/** Generic sortable column header, shared by the inspection lots and nonconformances tables below. */
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

/** Clickable KPI tile: icon + colour-accented top border + hover lift (same look as Work Orders/Maintenance). */
function QualityKpi({
  icon: Icon, label: lbl, value, color, hot, onClick,
}: { icon: Icon; label: string; value: number | string; color: string; hot?: boolean; onClick: () => void }) {
  return (
    <div className="panel kpi wo-kpi" style={{ borderTop: `3px solid ${color}` }} onClick={onClick} role="button" tabIndex={0}
      onKeyDown={(e) => e.key === "Enter" && onClick()}>
      <div className="wo-kpi-top"><span className="wo-kpi-icon" style={{ background: color }}><Icon size={13} stroke={2.2} /></span></div>
      <div className="label">{lbl}</div>
      <div className="value" style={{ color: hot ? color : undefined }}>{value}</div>
    </div>
  );
}

type LotSortKey = "number" | "type" | "status" | "material" | "order" | "results" | "opened";
const LOT_SORT_VALUE: Record<LotSortKey, (l: InspectionLot) => number | string> = {
  number: (l) => l.number,
  type: (l) => l.inspection_type,
  status: (l) => l.status,
  material: (l) => l.material_number,
  order: (l) => l.erp_order_number ?? "",
  results: (l) => l.results_recorded,
  opened: (l) => l.created_at,
};

type NcSortKey = "number" | "status" | "material" | "order" | "reported";
const NC_SORT_VALUE: Record<NcSortKey, (n: Nonconformance) => number | string> = {
  number: (n) => n.number,
  status: (n) => n.status,
  material: (n) => n.material_number ?? "",
  order: (n) => n.erp_order_number ?? "",
  reported: (n) => n.reported_at,
};

type LotFilter = "open" | "decision" | "all";
type NcFilter = "active" | "all";

export function QualityPage() {
  const { plant } = usePlant();
  const pid = plant?.id;
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "inspections";
  const [lotFilter, setLotFilter] = useState<LotFilter>("open");
  const [ncFilter, setNcFilter] = useState<NcFilter>("active");
  const meta = useQualityMeta();
  const open = (key: "lot" | "nc", id: string | null) =>
    setParams((p) => { if (id) p.set(key, id); else p.delete(key); return p; });

  const lots = useQuery({
    queryKey: ["inspection-lots", pid, lotFilter],
    queryFn: () =>
      unwrap(core.GET("/api/v1/quality/inspection-lots", {
        params: { query: { plant_id: pid!, ...(lotFilter === "open" ? { status: ["OPEN"] } : lotFilter === "decision" ? { awaiting_decision: true } : {}) } },
      })),
    enabled: Boolean(pid),
    refetchInterval: 5_000,
  });
  const counts = useQuery({
    queryKey: ["inspection-lots", pid, "counts"],
    queryFn: async () => {
      const [openL, decision, ncs] = await Promise.all([
        unwrap(core.GET("/api/v1/quality/inspection-lots", { params: { query: { plant_id: pid!, status: ["OPEN"] } } })),
        unwrap(core.GET("/api/v1/quality/inspection-lots", { params: { query: { plant_id: pid!, awaiting_decision: true } } })),
        unwrap(core.GET("/api/v1/quality/nonconformances", { params: { query: { plant_id: pid!, status: ["OPEN", "REWORK"] } } })),
      ]);
      return { open: openL.length, decision: decision.length, ncs: ncs.length, critical: ncs.filter((n) => n.severity === "CRITICAL").length };
    },
    enabled: Boolean(pid),
    refetchInterval: 5_000,
  });
  const ncs = useQuery({
    queryKey: ["ncs", pid, ncFilter],
    queryFn: () =>
      unwrap(core.GET("/api/v1/quality/nonconformances", {
        params: { query: { plant_id: pid!, ...(ncFilter === "active" ? { status: ["OPEN", "REWORK"] } : {}) } },
      })),
    enabled: Boolean(pid),
    refetchInterval: 5_000,
  });

  const [lSearch, setLSearch] = useState("");
  const [lSort, setLSort] = useState<{ key: LotSortKey; dir: 1 | -1 }>({ key: "opened", dir: -1 });
  const lotRows = useMemo(() => {
    const s = lSearch.trim().toLowerCase();
    const filtered = (lots.data ?? []).filter(
      (l) => !s || l.number.toLowerCase().includes(s) || l.material_number.toLowerCase().includes(s) || (l.erp_order_number ?? "").toLowerCase().includes(s) || (l.lot_number ?? "").toLowerCase().includes(s),
    );
    return sortBy(filtered, lSort, LOT_SORT_VALUE);
  }, [lots.data, lSearch, lSort]);

  const [ncSearch, setNcSearch] = useState("");
  const [ncSort, setNcSort] = useState<{ key: NcSortKey; dir: 1 | -1 }>({ key: "reported", dir: -1 });
  const ncRows = useMemo(() => {
    const s = ncSearch.trim().toLowerCase();
    const filtered = (ncs.data ?? []).filter(
      (n) => !s || n.number.toLowerCase().includes(s) || (n.material_number ?? "").toLowerCase().includes(s) || (n.erp_order_number ?? "").toLowerCase().includes(s) || n.description.toLowerCase().includes(s),
    );
    return sortBy(filtered, ncSort, NC_SORT_VALUE);
  }, [ncs.data, ncSearch, ncSort]);

  return (
    <>
      <PageHeader title="Quality" sub="Inspection lots, usage decisions and nonconformances. Decisions that move stock are e-signed and posted to SAP." />
      <SimpleGrid cols={4} mb="md" className="rise">
        <QualityKpi icon={IconClipboardCheck} label="Inspections open" value={counts.data?.open ?? "·"} color="var(--mes-blue)"
          onClick={() => { setLotFilter("open"); setParams({ tab: "inspections" }); }} />
        <QualityKpi icon={IconScale} label="Awaiting usage decision" value={counts.data?.decision ?? "·"} color="var(--mes-amber)" hot={Boolean(counts.data?.decision)}
          onClick={() => { setLotFilter("decision"); setParams({ tab: "inspections" }); }} />
        <QualityKpi icon={IconAlertCircle} label="Open nonconformances" value={counts.data?.ncs ?? "·"} color="var(--mes-amber)" hot={Boolean(counts.data?.ncs)}
          onClick={() => { setNcFilter("active"); setParams({ tab: "ncs" }); }} />
        <QualityKpi icon={IconFlame} label="Critical" value={counts.data?.critical ?? "·"} color="var(--mes-red)" hot={Boolean(counts.data?.critical)}
          onClick={() => { setNcFilter("active"); setParams({ tab: "ncs" }); }} />
      </SimpleGrid>
      <Tabs value={tab} onChange={(v) => setParams({ tab: v ?? "inspections" })}>
        <Tabs.List>
          <Tabs.Tab value="inspections">Inspection lots</Tabs.Tab>
          <Tabs.Tab value="ncs">Nonconformances</Tabs.Tab>
          <Tabs.Tab value="plans">Inspection plans</Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="inspections" pt="sm">
          <SegmentedControl mb="sm" size="xs" value={lotFilter} onChange={(v) => setLotFilter(v as LotFilter)}
            data={[{ value: "open", label: "Open" }, { value: "decision", label: "Awaiting decision" }, { value: "all", label: "All" }]} />
          <TextInput size="xs" w={280} mb="xs" leftSection={<IconSearch size={14} />}
            rightSection={lSearch ? (
              <ActionIcon variant="subtle" color="gray" size="xs" onClick={() => setLSearch("")} aria-label="Clear search"><IconX size={12} /></ActionIcon>
            ) : undefined}
            placeholder="Lot, material, order…" value={lSearch} onChange={(e) => setLSearch(e.currentTarget.value)} />
          <Table className="panel" highlightOnHover>
            <Table.Thead>
              <Table.Tr>
                <SortTh id="number" sort={lSort} onSort={(k) => toggleSort(setLSort, k)}>Lot</SortTh>
                <SortTh id="type" sort={lSort} onSort={(k) => toggleSort(setLSort, k)}>Type</SortTh>
                <SortTh id="status" sort={lSort} onSort={(k) => toggleSort(setLSort, k)}>Status</SortTh>
                <SortTh id="material" sort={lSort} onSort={(k) => toggleSort(setLSort, k)}>Material</SortTh>
                <SortTh id="order" sort={lSort} onSort={(k) => toggleSort(setLSort, k)}>Order / op</SortTh>
                <Table.Th>Stock lot</Table.Th>
                <SortTh id="results" align="right" sort={lSort} onSort={(k) => toggleSort(setLSort, k)}>Results</SortTh>
                <SortTh id="opened" sort={lSort} onSort={(k) => toggleSort(setLSort, k)}>Opened</SortTh>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {lotRows.map((l) => (
                <Table.Tr key={l.id} style={{ cursor: "pointer" }} onClick={() => open("lot", l.id)}>
                  <Table.Td className="mono">{l.number}</Table.Td>
                  <Table.Td><Text size="sm">{TYPE_LABEL[l.inspection_type]}</Text>{l.inspection_type === "IN_PROCESS" && l.blocking && <Text size="10px" c="dimmed">gates reporting</Text>}</Table.Td>
                  <Table.Td><InspStatus status={l.status} decision={l.usage_decision} /></Table.Td>
                  <Table.Td><span className="mono">{l.material_number}</span> <Text span size="xs" c="dimmed">{l.material_description}</Text></Table.Td>
                  <Table.Td className="mono">{l.erp_order_number ? `${l.erp_order_number}${l.op_no ? ` / ${l.op_no}` : ""}` : "—"}</Table.Td>
                  <Table.Td className="mono">{l.lot_number ? `${l.lot_number} · ${num(l.quantity, 3)} ${l.uom}` : "—"}</Table.Td>
                  <Table.Td className="mono" style={{ textAlign: "right" }}>{l.results_recorded}/{l.results_expected}</Table.Td>
                  <Table.Td><Text size="xs" c="dimmed">{fromNow(l.created_at)}</Text></Table.Td>
                </Table.Tr>
              ))}
              {!lotRows.length && <Table.Tr><Table.Td colSpan={8}><Text c="dimmed" size="sm">{lots.data?.length ? "No inspection lots match this filter." : "Nothing here."}</Text></Table.Td></Table.Tr>}
            </Table.Tbody>
          </Table>
        </Tabs.Panel>

        <Tabs.Panel value="ncs" pt="sm">
          <SegmentedControl mb="sm" size="xs" value={ncFilter} onChange={(v) => setNcFilter(v as NcFilter)}
            data={[{ value: "active", label: "Open / rework" }, { value: "all", label: "All" }]} />
          <TextInput size="xs" w={280} mb="xs" leftSection={<IconSearch size={14} />}
            rightSection={ncSearch ? (
              <ActionIcon variant="subtle" color="gray" size="xs" onClick={() => setNcSearch("")} aria-label="Clear search"><IconX size={12} /></ActionIcon>
            ) : undefined}
            placeholder="NC, material, order, description…" value={ncSearch} onChange={(e) => setNcSearch(e.currentTarget.value)} />
          <Table className="panel" highlightOnHover>
            <Table.Thead>
              <Table.Tr>
                <SortTh id="number" sort={ncSort} onSort={(k) => toggleSort(setNcSort, k)}>NC</SortTh>
                <SortTh id="status" sort={ncSort} onSort={(k) => toggleSort(setNcSort, k)}>Status</SortTh>
                <Table.Th>Defect</Table.Th>
                <SortTh id="material" sort={ncSort} onSort={(k) => toggleSort(setNcSort, k)}>Material</SortTh>
                <SortTh id="order" sort={ncSort} onSort={(k) => toggleSort(setNcSort, k)}>Order / lot</SortTh>
                <Table.Th>Qty</Table.Th>
                <Table.Th>Source</Table.Th>
                <SortTh id="reported" sort={ncSort} onSort={(k) => toggleSort(setNcSort, k)}>Reported</SortTh>
                <Table.Th>Disposition</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {ncRows.map((n) => (
                <Table.Tr key={n.id} style={{ cursor: "pointer" }} onClick={() => open("nc", n.id)}>
                  <Table.Td className="mono">{n.number}</Table.Td>
                  <Table.Td><Group gap={4} wrap="nowrap"><NcStatus status={n.status} /><Severity severity={n.severity} /></Group></Table.Td>
                  <Table.Td><Text size="sm">{label(meta.data, "defect_codes", n.defect_code)}</Text><Text size="xs" c="dimmed" lineClamp={1} maw={320}>{n.description}</Text></Table.Td>
                  <Table.Td className="mono">{n.material_number ?? "—"}</Table.Td>
                  <Table.Td className="mono">{[n.erp_order_number && `${n.erp_order_number}${n.op_no ? `/${n.op_no}` : ""}`, n.lot_number].filter(Boolean).join(" · ") || "—"}</Table.Td>
                  <Table.Td className="mono">{n.quantity ? `${num(n.quantity, 3)} ${n.uom ?? ""}` : "—"}</Table.Td>
                  <Table.Td><Text size="xs">{n.source.toLowerCase()}</Text></Table.Td>
                  <Table.Td><Text size="xs">{n.reported_by_name}</Text><Text size="10px" c="dimmed">{fmtDateTime(n.reported_at)}</Text></Table.Td>
                  <Table.Td><Text size="xs">{n.disposition ? label(meta.data, "dispositions", n.disposition) : "—"}</Text></Table.Td>
                </Table.Tr>
              ))}
              {!ncRows.length && <Table.Tr><Table.Td colSpan={9}><Text c="dimmed" size="sm">{ncs.data?.length ? "No nonconformances match this filter." : "No nonconformances."}</Text></Table.Td></Table.Tr>}
            </Table.Tbody>
          </Table>
        </Tabs.Panel>

        <Tabs.Panel value="plans" pt="sm">
          <PlansTab />
        </Tabs.Panel>
      </Tabs>
      <InspectionLotDrawer id={params.get("lot")} onClose={() => open("lot", null)} />
      <NcDrawer id={params.get("nc")} onClose={() => open("nc", null)} />
    </>
  );
}

// ------------------------------------------------------------------ plans

function PlansTab() {
  const { plant } = usePlant();
  const canPlan = useCan("quality:plan");
  const [editing, setEditing] = useState<InspectionPlan | "new" | null>(null);
  const [search, setSearch] = useState("");
  const plans = useQuery({
    queryKey: ["inspection-plans", plant?.id],
    queryFn: () => unwrap(core.GET("/api/v1/quality/plans", { params: { query: { plant_id: plant!.id } } })),
    enabled: Boolean(plant),
  });
  const rows = useMemo(() => {
    const s = search.trim().toLowerCase();
    return (plans.data ?? []).filter((p) => !s || p.material_number.toLowerCase().includes(s) || p.material_description.toLowerCase().includes(s) || p.name.toLowerCase().includes(s));
  }, [plans.data, search]);
  return (
    <>
      <Group justify="space-between" mb="sm">
        <TextInput size="xs" w={280} leftSection={<IconSearch size={14} />}
          rightSection={search ? (
            <ActionIcon variant="subtle" color="gray" size="xs" onClick={() => setSearch("")} aria-label="Clear search"><IconX size={12} /></ActionIcon>
          ) : undefined}
          placeholder="Material, plan name…" value={search} onChange={(e) => setSearch(e.currentTarget.value)} />
        {canPlan && <Button size="xs" leftSection={<IconPlus size={14} />} onClick={() => setEditing("new")}>New plan</Button>}
      </Group>
      <Table className="panel">
        <Table.Thead>
          <Table.Tr><Table.Th>Material</Table.Th><Table.Th>Type</Table.Th><Table.Th>Plan</Table.Th><Table.Th>Samples</Table.Th><Table.Th>Characteristics</Table.Th><Table.Th /></Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {rows.map((p) => (
            <Table.Tr key={p.id} style={{ opacity: p.active ? 1 : 0.5 }}>
              <Table.Td><span className="mono">{p.material_number}</span> <Text span size="xs" c="dimmed">{p.material_description}</Text></Table.Td>
              <Table.Td><Text size="sm">{TYPE_LABEL[p.inspection_type]}{p.op_no && <Text span className="mono" c="dimmed"> op {p.op_no}</Text>}</Text>
                {p.inspection_type === "IN_PROCESS" && <Text size="10px" c="dimmed">{p.blocking ? "blocks reporting until passed" : "informational"}</Text>}</Table.Td>
              <Table.Td>{p.name}{!p.active && <Badge ml={6} size="xs" variant="outline" color="navy.6">inactive</Badge>}</Table.Td>
              <Table.Td className="mono">{p.sample_size}</Table.Td>
              <Table.Td>
                {p.characteristics.map((c) => (
                  <Text key={c.id} size="xs"><b>{c.name}</b> <span className="mono" style={{ color: "var(--mes-text-dim)" }}>
                    {c.kind === "QUANTITATIVE" ? `${num(c.lower_limit, 4)} … ${num(c.upper_limit, 4)} ${c.uom ?? ""}` : "OK / not OK"}</span></Text>
                ))}
              </Table.Td>
              <Table.Td>{canPlan && <ActionIcon variant="subtle" onClick={() => setEditing(p)}><IconPencil size={15} /></ActionIcon>}</Table.Td>
            </Table.Tr>
          ))}
          {!rows.length && <Table.Tr><Table.Td colSpan={6}><Text c="dimmed" size="sm">{plans.data?.length ? "No plans match this filter." : "No inspection plans yet."}</Text></Table.Td></Table.Tr>}
        </Table.Tbody>
      </Table>
      {editing && <PlanModal plan={editing === "new" ? null : editing} onClose={() => setEditing(null)} />}
    </>
  );
}

const emptyChar = (): Characteristic => ({ name: "", kind: "QUANTITATIVE", lower_limit: null, upper_limit: null, uom: "mm", target: null });

function PlanModal({ plan, onClose }: { plan: InspectionPlan | null; onClose: () => void }) {
  const { plant } = usePlant();
  const qc = useQueryClient();
  const materials = useQuery({ queryKey: ["materials-all"], queryFn: () => unwrap(core.GET("/api/v1/materials", { params: { query: { limit: 500 } } })) });
  const [p, setP] = useState<PlanIn>(() => plan
    ? { plant_id: plan.plant_id, material_id: plan.material_id, inspection_type: plan.inspection_type as PlanIn["inspection_type"], op_no: plan.op_no,
        name: plan.name, sample_size: plan.sample_size, blocking: plan.blocking, active: plan.active, characteristics: plan.characteristics }
    : { plant_id: plant!.id, material_id: "", inspection_type: "FINAL", op_no: null, name: "", sample_size: 1, blocking: true, active: true, characteristics: [emptyChar()] });
  const setChar = (i: number, c: Partial<Characteristic>) => setP((x) => ({ ...x, characteristics: x.characteristics.map((y, j) => (j === i ? { ...y, ...c } : y)) }));
  const save = useMutation({
    mutationFn: () => unwrap(core.PUT("/api/v1/quality/plans", { body: { ...p, op_no: p.inspection_type === "IN_PROCESS" ? p.op_no : null } })),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["inspection-plans"] }); onClose(); },
  });
  const valid = p.material_id && p.name.trim() && p.characteristics.every((c) => c.name.trim()) && (p.inspection_type !== "IN_PROCESS" || p.op_no);
  const dec = (v: string | number) => (v === "" ? null : String(v));
  return (
    <Modal opened onClose={onClose} size="xl" centered title={<Text fw={600}>{plan ? "Edit inspection plan" : "New inspection plan"}</Text>}>
      <Stack>
        <Group grow>
          <Select searchable label="Material" disabled={Boolean(plan)} value={p.material_id || null}
            data={(materials.data ?? []).map((m) => ({ value: m.id, label: `${m.material_number} · ${m.description}` }))}
            onChange={(v) => setP({ ...p, material_id: v ?? "" })} />
          <Select label="Type" disabled={Boolean(plan)} value={p.inspection_type}
            data={[{ value: "INCOMING", label: "Incoming (goods receipt)" }, { value: "IN_PROCESS", label: "First-off (operation)" }, { value: "FINAL", label: "Final (order receipt)" }]}
            onChange={(v) => setP({ ...p, inspection_type: (v ?? "FINAL") as PlanIn["inspection_type"] })} />
          {p.inspection_type === "IN_PROCESS" && <TextInput label="Operation" placeholder="0020" disabled={Boolean(plan)} value={p.op_no ?? ""} onChange={(e) => setP({ ...p, op_no: e.currentTarget.value })} />}
        </Group>
        <Group grow align="flex-end">
          <TextInput label="Name" value={p.name} onChange={(e) => setP({ ...p, name: e.currentTarget.value })} />
          <NumberInput label="Sample size" min={1} max={50} value={p.sample_size} onChange={(v) => setP({ ...p, sample_size: Number(v) || 1 })} />
          {p.inspection_type === "IN_PROCESS" && <Switch label="Blocks reporting until passed" checked={p.blocking} onChange={(e) => setP({ ...p, blocking: e.currentTarget.checked })} />}
          <Switch label="Active" checked={p.active} onChange={(e) => setP({ ...p, active: e.currentTarget.checked })} />
        </Group>
        <div className="label">Characteristics</div>
        {p.characteristics.map((c, i) => (
          <Group key={i} gap="xs" align="flex-end" wrap="nowrap">
            <TextInput style={{ flex: 2 }} label={i === 0 ? "Name" : undefined} value={c.name} onChange={(e) => setChar(i, { name: e.currentTarget.value })} />
            <Select w={150} label={i === 0 ? "Kind" : undefined} value={c.kind} data={[{ value: "QUANTITATIVE", label: "Measured" }, { value: "QUALITATIVE", label: "OK / not OK" }]}
              onChange={(v) => setChar(i, { kind: (v ?? "QUANTITATIVE") as Characteristic["kind"] })} />
            {c.kind === "QUANTITATIVE" && <>
              <NumberInput w={100} label={i === 0 ? "Lower" : undefined} decimalScale={4} value={c.lower_limit ?? ""} onChange={(v) => setChar(i, { lower_limit: dec(v) })} />
              <NumberInput w={100} label={i === 0 ? "Upper" : undefined} decimalScale={4} value={c.upper_limit ?? ""} onChange={(v) => setChar(i, { upper_limit: dec(v) })} />
              <TextInput w={60} label={i === 0 ? "Unit" : undefined} value={c.uom ?? ""} onChange={(e) => setChar(i, { uom: e.currentTarget.value || null })} />
            </>}
            <ActionIcon variant="subtle" color="red" disabled={p.characteristics.length === 1}
              onClick={() => setP({ ...p, characteristics: p.characteristics.filter((_, j) => j !== i) })}><IconTrash size={15} /></ActionIcon>
          </Group>
        ))}
        <Button variant="subtle" size="xs" w="fit-content" leftSection={<IconPlus size={14} />}
          onClick={() => setP({ ...p, characteristics: [...p.characteristics, emptyChar()] })}>Add characteristic</Button>
        <Text size="xs" c="dimmed">Characteristics of a plan that inspection lots already use can't be changed; create a new plan instead. <Link to="/audit" style={{ color: "inherit" }}>Changes are audited.</Link></Text>
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button loading={save.isPending} disabled={!valid} onClick={() => save.mutate()}>Save plan</Button>
        </Group>
      </Stack>
    </Modal>
  );
}
