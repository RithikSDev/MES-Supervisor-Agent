import {
  Badge, Button, Group, Loader, Modal, NumberInput, Progress, SegmentedControl, Select, Stack, Table, Text, Textarea, Timeline,
} from "@mantine/core";
import { core, unwrap } from "@mes/api-client";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { useCan } from "../auth";
import {
  InspStatus, InspectionLotDrawer, NcDrawer, NcStatus, Severity, StockStatus, TYPE_LABEL, label, useQualityMeta,
} from "./quality";
import { fmtDateTime, minutes, num } from "../lib/format";
import { MovementsTable } from "../pages/MaterialsPage";

const EVENT_COLOR: Record<string, string> = {
  START_SETUP: "cyan", START_RUN: "green", START_PRODUCTION: "green", PAUSE: "amber", RESUME: "green",
  REPORT: "blue", COMPLETE: "green", STOP: "gray",
};

export function ExecutionTab({ workOrderId }: { workOrderId: string }) {
  const q = useQuery({
    queryKey: ["wo-execution", workOrderId],
    queryFn: () => unwrap(core.GET("/api/v1/work-orders/{wo_id}/execution", { params: { path: { wo_id: workOrderId } } })),
    refetchInterval: 5_000,
  });
  if (q.isPending) return <Loader size="sm" />;
  const { runs = [], events = [] } = q.data ?? {};
  if (!runs.length) return <Text c="dimmed" size="sm">No shop-floor activity yet. Operators start work on the line terminal.</Text>;
  return (
    <Group align="flex-start" gap="xl" wrap="nowrap">
      <Table className="panel" style={{ flex: 1.4 }}>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Op</Table.Th>
            <Table.Th>Machine</Table.Th>
            <Table.Th>Operator</Table.Th>
            <Table.Th>Status</Table.Th>
            <Table.Th>Setup</Table.Th>
            <Table.Th>Run</Table.Th>
            <Table.Th>Paused</Table.Th>
            <Table.Th>Good / scrap</Table.Th>
            <Table.Th>Started</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {runs.map((r) => (
            <Table.Tr key={r.id}>
              <Table.Td className="mono">{r.op_no}</Table.Td>
              <Table.Td className="mono">{r.equipment_code}</Table.Td>
              <Table.Td>{r.operator_name}</Table.Td>
              <Table.Td><Badge size="sm" variant={r.status === "ENDED" ? "outline" : "light"} color={r.status === "PAUSED" ? "amber" : r.status === "ENDED" ? "navy.6" : "green"}>{r.status}</Badge></Table.Td>
              <Table.Td className="mono">{minutes(Math.round(r.setup_s / 60))}</Table.Td>
              <Table.Td className="mono">{minutes(Math.round(r.run_s / 60))}</Table.Td>
              <Table.Td className="mono" c={r.paused_s ? "amber.8" : "dimmed"}>{minutes(Math.round(r.paused_s / 60))}</Table.Td>
              <Table.Td className="mono">{num(r.yield_qty)} / <Text span c={Number(r.scrap_qty) ? "red.7" : "dimmed"} inherit>{num(r.scrap_qty)}</Text></Table.Td>
              <Table.Td className="mono">{fmtDateTime(r.started_at)}</Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      <div className="panel" style={{ flex: 1, padding: 12, maxHeight: 460, overflow: "auto" }}>
        <Text className="label" mb="xs">Shop-floor events</Text>
        <Timeline bulletSize={12} lineWidth={2}>
          {events.map((e) => (
            <Timeline.Item key={e.id} color={EVENT_COLOR[e.event_type] ?? "gray"}
              title={<Text size="sm" fw={600}>{e.event_type.replaceAll("_", " ").toLowerCase()} <Text span size="xs" c="dimmed" className="mono">{e.equipment_code}</Text></Text>}>
              <Text size="xs" c="dimmed" className="mono">{fmtDateTime(e.occurred_at)} · {e.operator_name}</Text>
              {e.reason_code && <Text size="xs" c="amber.8">{e.reason_code.replaceAll("_", " ").toLowerCase()}</Text>}
              {e.details && "yield_qty" in e.details && (
                <Text size="xs">good {String(e.details.yield_qty)} · scrap {String(e.details.scrap_qty)} · setup {String(e.details.setup_min)} min · run {String(e.details.run_min)} min</Text>
              )}
              {new Date(e.recorded_at).getTime() - new Date(e.occurred_at).getTime() > 60_000 && (
                <Text size="xs" c="amber.8">recorded offline, synced {fmtDateTime(e.recorded_at)}</Text>
              )}
            </Timeline.Item>
          ))}
        </Timeline>
      </div>
    </Group>
  );
}

export function MaterialsTab({ workOrderId }: { workOrderId: string }) {
  const q = useQuery({
    queryKey: ["wo-materials", workOrderId],
    queryFn: () => unwrap(core.GET("/api/v1/work-orders/{wo_id}/materials", { params: { path: { wo_id: workOrderId } } })),
    refetchInterval: 5_000,
  });
  if (q.isPending) return <Loader size="sm" />;
  const d = q.data!;
  return (
    <Stack>
      <Group align="flex-start" gap="xl" wrap="nowrap">
        <Table className="panel" style={{ flex: 1.3 }}>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Component</Table.Th>
              <Table.Th>Op</Table.Th>
              <Table.Th w={260}>Issued / required</Table.Th>
              <Table.Th>Method</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {d.components.map((c) => {
              const pct = (Number(c.issued_qty) / Number(c.required_qty)) * 100;
              return (
                <Table.Tr key={c.component_id}>
                  <Table.Td><span className="mono">{c.material_number}</span> <Text span size="xs" c="dimmed">{c.material_description}</Text></Table.Td>
                  <Table.Td className="mono">{c.op_no}</Table.Td>
                  <Table.Td>
                    <Group gap={6} wrap="nowrap">
                      <Progress value={Math.min(pct, 100)} size={5} w={90} color={pct >= 100 ? "green" : "blue"} bg="navy.1" />
                      <Text size="xs" className="mono">{num(c.issued_qty, 3)} / {num(c.required_qty, 3)} {c.uom}</Text>
                    </Group>
                  </Table.Td>
                  <Table.Td>{c.backflush ? <Badge size="xs" variant="outline" color="navy.6">backflush</Badge> : <Badge size="xs" variant="outline" color="navy.6">issue</Badge>}</Table.Td>
                </Table.Tr>
              );
            })}
          </Table.Tbody>
        </Table>
        <Table className="panel" style={{ flex: 1 }}>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Op</Table.Th>
              <Table.Th>Good</Table.Th>
              <Table.Th>Scrap</Table.Th>
              <Table.Th>Waiting here (WIP)</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {d.wip.map((w) => (
              <Table.Tr key={w.operation_id}>
                <Table.Td><span className="mono">{w.op_no}</span> <Text span size="xs" c="dimmed">{w.description}</Text></Table.Td>
                <Table.Td className="mono">{num(w.yield_qty)}</Table.Td>
                <Table.Td className="mono" c={Number(w.scrap_qty) ? "red.7" : "dimmed"}>{num(w.scrap_qty)}</Table.Td>
                <Table.Td className="mono" c={Number(w.waiting_qty) ? "amber.8" : "dimmed"}>{num(w.waiting_qty)}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </Group>
      <Text className="label">Goods movements for this order</Text>
      <MovementsTable rows={d.movements} />
    </Stack>
  );
}

// ------------------------------------------------------------------ quality (phase 3)

export function QualityTab({ workOrderId, plantId, operations }: { workOrderId: string; plantId: string; operations: { id: string; op_no: string; description: string }[] }) {
  const canReport = useCan("nc:create");
  const meta = useQualityMeta();
  const [lotId, setLotId] = useState<string | null>(null);
  const [ncId, setNcId] = useState<string | null>(null);
  const [reporting, setReporting] = useState(false);
  const lots = useQuery({
    queryKey: ["wo-quality", workOrderId, "lots"],
    queryFn: () => unwrap(core.GET("/api/v1/quality/inspection-lots", { params: { query: { plant_id: plantId, work_order_id: workOrderId } } })),
    refetchInterval: 5_000,
  });
  const ncs = useQuery({
    queryKey: ["wo-quality", workOrderId, "ncs"],
    queryFn: () => unwrap(core.GET("/api/v1/quality/nonconformances", { params: { query: { plant_id: plantId, work_order_id: workOrderId } } })),
    refetchInterval: 5_000,
  });
  const produced = useQuery({
    queryKey: ["wo-quality", workOrderId, "produced"],
    queryFn: () => unwrap(core.GET("/api/v1/trace/orders/{wo_id}", { params: { path: { wo_id: workOrderId }, query: { direction: "forward" } } })),
  });
  const outLots = (produced.data?.root.children ?? []).filter((c) => c.type === "LOT");
  return (
    <Stack>
      <Group justify="space-between">
        <Text size="sm" c="dimmed">Inspections, nonconformances and what this order produced (lots and serial numbers).</Text>
        <Group gap="xs">
          <Button size="xs" variant="default" component={Link} to={`/traceability?order=${workOrderId}`}>Trace this order</Button>
          {canReport && <Button size="xs" color="amber" variant="light" onClick={() => setReporting(true)}>Report defect</Button>}
        </Group>
      </Group>
      <Group align="flex-start" gap="lg" wrap="nowrap">
        <Table className="panel" highlightOnHover style={{ flex: 1 }}>
          <Table.Thead><Table.Tr><Table.Th>Inspection</Table.Th><Table.Th>Type</Table.Th><Table.Th>Status</Table.Th><Table.Th>Op / lot</Table.Th><Table.Th>Opened</Table.Th></Table.Tr></Table.Thead>
          <Table.Tbody>
            {(lots.data ?? []).map((l) => (
              <Table.Tr key={l.id} style={{ cursor: "pointer" }} onClick={() => setLotId(l.id)}>
                <Table.Td className="mono">{l.number}</Table.Td>
                <Table.Td><Text size="sm">{TYPE_LABEL[l.inspection_type]}</Text></Table.Td>
                <Table.Td><InspStatus status={l.status} decision={l.usage_decision} /></Table.Td>
                <Table.Td className="mono">{l.op_no ?? l.lot_number ?? "—"}</Table.Td>
                <Table.Td className="mono">{fmtDateTime(l.created_at)}</Table.Td>
              </Table.Tr>
            ))}
            {lots.data?.length === 0 && <Table.Tr><Table.Td colSpan={5}><Text size="sm" c="dimmed">No inspections for this order.</Text></Table.Td></Table.Tr>}
          </Table.Tbody>
        </Table>
        <Table className="panel" highlightOnHover style={{ flex: 1 }}>
          <Table.Thead><Table.Tr><Table.Th>NC</Table.Th><Table.Th>Status</Table.Th><Table.Th>Defect</Table.Th><Table.Th>Reported</Table.Th></Table.Tr></Table.Thead>
          <Table.Tbody>
            {(ncs.data ?? []).map((n) => (
              <Table.Tr key={n.id} style={{ cursor: "pointer" }} onClick={() => setNcId(n.id)}>
                <Table.Td className="mono">{n.number}</Table.Td>
                <Table.Td><Group gap={4} wrap="nowrap"><NcStatus status={n.status} /><Severity severity={n.severity} /></Group></Table.Td>
                <Table.Td><Text size="sm">{label(meta.data, "defect_codes", n.defect_code)}</Text><Text size="xs" c="dimmed" lineClamp={1}>{n.description}</Text></Table.Td>
                <Table.Td><Text size="xs">{n.reported_by_name}</Text><Text size="10px" c="dimmed">{fmtDateTime(n.reported_at)}</Text></Table.Td>
              </Table.Tr>
            ))}
            {ncs.data?.length === 0 && <Table.Tr><Table.Td colSpan={4}><Text size="sm" c="dimmed">No nonconformances.</Text></Table.Td></Table.Tr>}
          </Table.Tbody>
        </Table>
      </Group>
      <div className="panel" style={{ padding: 12 }}>
        <Text className="label" mb={6}>Produced lots</Text>
        {outLots.length === 0 && <Text size="sm" c="dimmed">Nothing received yet (goods receipt happens on the last operation).</Text>}
        {outLots.map((l) => (
          <Group key={l.id} gap="sm" mb={6} align="flex-start" wrap="nowrap">
            <Link to={`/traceability?lot=${l.id}`} className="mono" style={{ color: "inherit", minWidth: 110 }}>{l.label}</Link>
            <Text size="xs" className="mono" miw={70}>{num(l.quantity, 3)} {l.uom}</Text>
            <Group gap={4}>{(l.lot?.stock ?? []).map((s, i) => <Group key={i} gap={3}><Text size="xs" className="mono">{s.location_code}</Text><StockStatus status={s.status} /></Group>)}</Group>
            <Group gap={4}>{(l.lot?.serials ?? []).map((s) => <Badge key={s} size="xs" variant="outline" color="navy.6">{s}</Badge>)}</Group>
          </Group>
        ))}
      </div>
      <InspectionLotDrawer id={lotId} onClose={() => setLotId(null)} />
      <NcDrawer id={ncId} onClose={() => setNcId(null)} />
      {reporting && <DefectModal workOrderId={workOrderId} operations={operations} onClose={() => setReporting(false)} />}
    </Stack>
  );
}

function DefectModal({ workOrderId, operations, onClose }: { workOrderId: string; operations: { id: string; op_no: string; description: string }[]; onClose: () => void }) {
  const qc = useQueryClient();
  const meta = useQualityMeta();
  const [op, setOp] = useState<string | null>(null);
  const [code, setCode] = useState<string | null>(null);
  const [severity, setSeverity] = useState("MINOR");
  const [qty, setQty] = useState<number | string>("");
  const [desc, setDesc] = useState("");
  const m = useMutation({
    mutationFn: () => unwrap(core.POST("/api/v1/quality/nonconformances", {
      body: { work_order_id: workOrderId, operation_id: op, defect_code: code!, severity: severity as "MINOR", description: desc,
        quantity: qty === "" ? null : String(qty), client_event_id: crypto.randomUUID() },
    })),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["wo-quality", workOrderId] }); qc.invalidateQueries({ queryKey: ["wo"] }); onClose(); },
  });
  return (
    <Modal opened onClose={onClose} centered title={<Text fw={600}>Report defect</Text>}>
      <Stack>
        <Select label="Operation" clearable data={operations.map((o) => ({ value: o.id, label: `${o.op_no} · ${o.description}` }))} value={op} onChange={setOp} />
        <Select label="Defect" data={(meta.data?.defect_codes ?? []).map((d) => ({ value: d.code, label: d.label }))} value={code} onChange={setCode} />
        <SegmentedControl value={severity} onChange={setSeverity} data={["MINOR", "MAJOR", "CRITICAL"]} color={severity === "CRITICAL" ? "red" : undefined} />
        {severity === "CRITICAL" && <Text size="xs" c="red.7">A critical defect puts the order on hold.</Text>}
        <NumberInput label="Affected quantity" min={0} value={qty} onChange={setQty} />
        <Textarea label="Description" autosize minRows={2} value={desc} onChange={(e) => setDesc(e.currentTarget.value)} />
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button loading={m.isPending} disabled={!code || !desc.trim()} onClick={() => m.mutate()}>Report</Button>
        </Group>
      </Stack>
    </Modal>
  );
}
