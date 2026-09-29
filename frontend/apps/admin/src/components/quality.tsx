import {
  Alert, Badge, Button, Drawer, Group, Loader, NumberInput, SegmentedControl, Stack, Table, Text, Tooltip,
} from "@mantine/core";
import {
  ApiError, type Characteristic, type InspectionLotDetail, type NcDetail, type QualityMeta, core, unwrap,
} from "@mes/api-client";
import { IconAlertTriangle, IconCheck, IconX } from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import { useCan } from "../auth";
import { fmtDateTime, num } from "../lib/format";
import { SignatureModal } from "./dialogs";

// ------------------------------------------------------------------ badges

const INSP: Record<string, { color: string; variant: "light" | "outline" | "filled" }> = {
  OPEN: { color: "cyan", variant: "outline" },
  PASSED: { color: "green", variant: "light" },
  FAILED: { color: "red", variant: "filled" },
  CANCELLED: { color: "navy.5", variant: "outline" },
};

export function InspStatus({ status, decision }: { status: string; decision?: string | null }) {
  const s = INSP[status] ?? { color: "gray", variant: "outline" };
  return (
    <Group gap={4} wrap="nowrap">
      <Badge size="sm" color={s.color} variant={s.variant}>{status}</Badge>
      {decision && (
        <Badge size="sm" variant="outline" color={decision === "ACCEPTED" ? "green" : "red"}
          leftSection={decision === "ACCEPTED" ? <IconCheck size={10} /> : <IconX size={10} />}>
          UD {decision.toLowerCase()}
        </Badge>
      )}
    </Group>
  );
}

export function NcStatus({ status }: { status: string }) {
  const color = status === "OPEN" ? "amber" : status === "REWORK" ? "blue" : "navy.6";
  return <Badge size="sm" color={color} variant={status === "CLOSED" ? "outline" : "light"}>{status}</Badge>;
}

export function Severity({ severity }: { severity: string }) {
  const color = severity === "CRITICAL" ? "red" : severity === "MAJOR" ? "amber" : "navy.6";
  return <Badge size="xs" color={color} variant={severity === "CRITICAL" ? "filled" : "outline"}>{severity}</Badge>;
}

/** Stock type: free stock is quiet, anything that can't be used is coloured. */
export function StockStatus({ status }: { status: string }) {
  if (status === "UNRESTRICTED") return <Text size="xs" c="dimmed">free</Text>;
  return (
    <Badge size="xs" color={status === "BLOCKED" ? "red" : "cyan"} variant="light">
      {status === "QUALITY" ? "in QI" : "blocked"}
    </Badge>
  );
}

export const TYPE_LABEL: Record<string, string> = { INCOMING: "Incoming", IN_PROCESS: "First-off", FINAL: "Final" };

export function useQualityMeta() {
  return useQuery({
    queryKey: ["quality-meta"],
    queryFn: () => unwrap(core.GET("/api/v1/quality/meta")),
    staleTime: Infinity,
  });
}

export const label = (meta: QualityMeta | undefined, kind: "defect_codes" | "dispositions", code?: string | null) =>
  (code && meta?.[kind].find((x) => x.code === code)?.label) || code || "—";

function limits(c: Characteristic) {
  if (c.kind === "QUALITATIVE") return "OK / not OK";
  const lo = c.lower_limit != null ? num(c.lower_limit, 4) : "−∞";
  const hi = c.upper_limit != null ? num(c.upper_limit, 4) : "+∞";
  return `${lo} … ${hi} ${c.uom ?? ""}`;
}

// ------------------------------------------------------------------ inspection lot

type Cell = { value?: string; ok?: boolean };

export function InspectionLotDrawer({ id, onClose }: { id: string | null; onClose: () => void }) {
  const qc = useQueryClient();
  const canInspect = useCan("quality:inspect");
  const canDecide = useCan("quality:decide");
  const q = useQuery({
    queryKey: ["inspection-lot", id],
    queryFn: () => unwrap(core.GET("/api/v1/quality/inspection-lots/{lot_id}", { params: { path: { lot_id: id! } } })),
    enabled: Boolean(id),
  });
  const [cells, setCells] = useState<Record<string, Cell>>({});
  const [signing, setSigning] = useState<"ACCEPTED" | "REJECTED" | null>(null);
  const d = q.data;
  useEffect(() => {
    if (!d) return;
    const init: Record<string, Cell> = {};
    for (const r of d.results) {
      init[`${r.characteristic_id}:${r.sample_no}`] = { value: r.measured_value ?? undefined, ok: r.attribute_ok ?? undefined };
    }
    setCells(init);
  }, [d]);
  const done = (x: InspectionLotDetail) => {
    qc.setQueryData(["inspection-lot", id], x);
    qc.invalidateQueries({ queryKey: ["inspection-lots"] });
    qc.invalidateQueries({ queryKey: ["ncs"] });
    qc.invalidateQueries({ queryKey: ["wo-quality"] });
  };
  const save = useMutation({
    mutationFn: () => {
      const results = Object.entries(cells).flatMap(([k, c]) => {
        const [characteristic_id, n] = k.split(":");
        const ch = d!.characteristics.find((x) => x.id === characteristic_id)!;
        if (ch.kind === "QUANTITATIVE" ? c.value === undefined || c.value === "" : c.ok === undefined) return [];
        return [{ characteristic_id, sample_no: Number(n), measured_value: ch.kind === "QUANTITATIVE" ? String(c.value) : null,
          attribute_ok: ch.kind === "QUALITATIVE" ? c.ok! : null }];
      });
      return unwrap(core.POST("/api/v1/quality/inspection-lots/{lot_id}/results", { params: { path: { lot_id: id! } }, body: { results } }));
    },
    onSuccess: done,
  });
  const decide = useMutation({
    mutationFn: (reason: string) =>
      unwrap(core.POST("/api/v1/quality/inspection-lots/{lot_id}/usage-decision", {
        params: { path: { lot_id: id! } }, body: { decision: signing!, reason },
      })),
    meta: { silent: true },
    onSuccess: (x) => { setSigning(null); done(x); },
  });
  const byKey = useMemo(() => new Map((d?.results ?? []).map((r) => [`${r.characteristic_id}:${r.sample_no}`, r])), [d]);
  const samples = d ? Array.from({ length: d.sample_size }, (_, i) => i + 1) : [];
  const editable = d?.status === "OPEN" && canInspect;
  const needsDecision = d && d.lot_id && !d.usage_decision && d.status !== "CANCELLED";

  return (
    <Drawer opened={Boolean(id)} onClose={onClose} position="right" size="xl"
      title={d && <Group gap="sm"><Text fw={700} className="mono">{d.number}</Text><InspStatus status={d.status} decision={d.usage_decision} /></Group>}>
      {!d ? <Loader size="sm" /> : (
        <Stack>
          <div className="panel" style={{ padding: 12 }}>
            <Group gap="xl" align="flex-start">
              <div><div className="label">Type</div><Text>{TYPE_LABEL[d.inspection_type]}</Text></div>
              <div><div className="label">Plan</div><Text>{d.plan_name}</Text></div>
              <div><div className="label">Material</div><Text className="mono">{d.material_number}</Text><Text size="xs" c="dimmed">{d.material_description}</Text></div>
              {d.erp_order_number && <div><div className="label">Order / op</div>
                <Link to={`/work-orders/${d.work_order_id}`} className="mono" style={{ color: "inherit" }}>{d.erp_order_number}</Link>
                {d.op_no && <Text span className="mono" c="dimmed"> / {d.op_no}</Text>}</div>}
              {d.lot_number && <div><div className="label">Lot</div><Text className="mono">{d.lot_number}</Text><Text size="xs" c="dimmed">{num(d.quantity, 3)} {d.uom}</Text></div>}
              <div><div className="label">Opened</div><Text className="mono">{fmtDateTime(d.created_at)}</Text></div>
            </Group>
            {d.stock.length > 0 && (
              <Group gap="xs" mt="sm">
                <Text size="xs" c="dimmed">Stock now:</Text>
                {d.stock.map((s, i) => <Group key={i} gap={4}><Text size="xs" className="mono">{s.location_code} {num(s.quantity, 3)}</Text><StockStatus status={s.status} /></Group>)}
              </Group>
            )}
          </div>

          <Table className="panel" withColumnBorders>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Characteristic</Table.Th>
                <Table.Th>Tolerance</Table.Th>
                {samples.map((n) => <Table.Th key={n}>Sample {n}</Table.Th>)}
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {d.characteristics.map((c) => (
                <Table.Tr key={c.id}>
                  <Table.Td><Text size="sm" fw={500}>{c.name}</Text></Table.Td>
                  <Table.Td className="mono"><Text size="xs">{limits(c)}</Text></Table.Td>
                  {samples.map((n) => {
                    const k = `${c.id}:${n}`;
                    const r = byKey.get(k);
                    const cell = cells[k] ?? {};
                    const tone = r ? (r.conforming ? "var(--mes-green)" : "var(--mes-red)") : undefined;
                    return (
                      <Table.Td key={n} style={{ boxShadow: tone ? `inset 3px 0 0 ${tone}` : undefined, minWidth: 140 }}>
                        {c.kind === "QUANTITATIVE" ? (
                          <NumberInput size="xs" hideControls decimalScale={4} disabled={!editable}
                            value={cell.value ?? ""} rightSection={<Text size="10px" c="dimmed">{c.uom}</Text>}
                            onChange={(v) => setCells((p) => ({ ...p, [k]: { value: v === "" ? undefined : String(v) } }))} />
                        ) : (
                          <SegmentedControl size="xs" disabled={!editable}
                            data={[{ value: "ok", label: "OK" }, { value: "nok", label: "Not OK" }]}
                            value={cell.ok === undefined ? "" : cell.ok ? "ok" : "nok"}
                            onChange={(v) => setCells((p) => ({ ...p, [k]: { ok: v === "ok" } }))} />
                        )}
                        {r && <Text size="10px" c="dimmed" mt={2}>{r.recorded_by_name} · {fmtDateTime(r.recorded_at)}</Text>}
                      </Table.Td>
                    );
                  })}
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
          <Group justify="space-between">
            <Text size="xs" c="dimmed">{d.results_recorded} of {d.results_expected} results recorded. One nonconforming value fails the lot.</Text>
            {editable && <Button loading={save.isPending} onClick={() => save.mutate()}>Save results</Button>}
          </Group>

          {d.nonconformances.length > 0 && (
            <Alert color="red" variant="light" icon={<IconAlertTriangle size={16} />} title="Nonconformance">
              {d.nonconformances.map((n) => (
                <Text key={n.id} size="sm"><span className="mono">{n.number}</span> · {n.status.toLowerCase()} · {n.description}</Text>
              ))}
            </Alert>
          )}

          {d.usage_decision && (
            <div className="panel" style={{ padding: 12 }}>
              <div className="label">Usage decision</div>
              <Text fw={600} c={d.usage_decision === "ACCEPTED" ? "green.7" : "red.7"}>{d.usage_decision}</Text>
              <Text size="sm">{d.decision_reason}</Text>
              <Text size="xs" c="dimmed">signed by {d.decided_by} · {fmtDateTime(d.decided_at)}</Text>
            </div>
          )}
          {needsDecision && canDecide && (
            <Group justify="flex-end">
              <Button color="red" variant="light" onClick={() => setSigning("REJECTED")}>Reject → blocked stock</Button>
              <Tooltip label="Only a passed inspection can be accepted. Nonconforming stock is released via its nonconformance (use as is)." disabled={d.status === "PASSED"}>
                <Button color="green" disabled={d.status !== "PASSED"} onClick={() => setSigning("ACCEPTED")}>Accept → free stock</Button>
              </Tooltip>
            </Group>
          )}
          <SignatureModal
            opened={signing !== null}
            meaning="APPROVED"
            subject={`Usage decision ${signing?.toLowerCase()} for ${d.number} (${d.material_number}, lot ${d.lot_number}, ${num(d.quantity, 3)} ${d.uom})`}
            busy={decide.isPending}
            error={decide.error instanceof ApiError ? decide.error.message : null}
            onClose={() => { setSigning(null); decide.reset(); }}
            onSign={(r) => decide.mutate(r)}
          />
        </Stack>
      )}
    </Drawer>
  );
}

// ------------------------------------------------------------------ nonconformance

export function NcDrawer({ id, onClose }: { id: string | null; onClose: () => void }) {
  const qc = useQueryClient();
  const canDecide = useCan("quality:decide");
  const meta = useQualityMeta();
  const q = useQuery({
    queryKey: ["nc", id],
    queryFn: () => unwrap(core.GET("/api/v1/quality/nonconformances/{nc_id}", { params: { path: { nc_id: id! } } })),
    enabled: Boolean(id),
  });
  const [disp, setDisp] = useState<string | null>(null);
  const [rework, setRework] = useState<{ good: number; scrap: number } | null>(null);
  const [signing, setSigning] = useState<"disp" | "rework" | null>(null);
  const d = q.data;
  const blockedNow = (d?.stock ?? []).filter((s) => s.status !== "UNRESTRICTED").reduce((a, s) => a + Number(s.quantity), 0);
  useEffect(() => { setDisp(null); setRework(null); }, [id]);
  const done = (x: NcDetail) => {
    setSigning(null);
    qc.setQueryData(["nc", id], x);
    ["ncs", "inspection-lots", "wo-quality", "stock", "movements", "trace"].forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
  };
  const act = useMutation({
    mutationFn: (reason: string) =>
      signing === "disp"
        ? unwrap(core.POST("/api/v1/quality/nonconformances/{nc_id}/disposition", {
            params: { path: { nc_id: id! } }, body: { disposition: disp as "SCRAP", reason } }))
        : unwrap(core.POST("/api/v1/quality/nonconformances/{nc_id}/rework-complete", {
            params: { path: { nc_id: id! } }, body: { good_qty: String(rework!.good), scrap_qty: String(rework!.scrap), reason } })),
    meta: { silent: true },
    onSuccess: done,
  });
  const dispOptions = (meta.data?.dispositions ?? []).filter((x) => x.code !== "RETURN_TO_SUPPLIER" || (d?.lot_origin && d.lot_origin !== "PRODUCTION"));
  const effect: Record<string, string> = {
    USE_AS_IS: "Stock in QI / blocked is released to free stock (321/343).",
    REWORK: "Stock is blocked (350) until rework is reported: good back to free stock (343), rest scrapped (555).",
    SCRAP: "Stock is blocked and scrapped (350 + 555).",
    RETURN_TO_SUPPLIER: "Stock is blocked and returned to the supplier (350 + 122).",
  };

  return (
    <Drawer opened={Boolean(id)} onClose={onClose} position="right" size="lg"
      title={d && <Group gap="sm"><Text fw={700} className="mono">{d.number}</Text><NcStatus status={d.status} /><Severity severity={d.severity} /></Group>}>
      {!d ? <Loader size="sm" /> : (
        <Stack>
          <div className="panel" style={{ padding: 12 }}>
            <Text fw={600}>{label(meta.data, "defect_codes", d.defect_code)}</Text>
            <Text size="sm" mt={4}>{d.description}</Text>
            <Group gap="xl" mt="sm" align="flex-start">
              <div><div className="label">Source</div><Text size="sm">{d.source.toLowerCase()}</Text></div>
              <div><div className="label">Reported</div><Text size="sm">{d.reported_by_name}</Text><Text size="xs" c="dimmed" className="mono">{fmtDateTime(d.reported_at)}</Text></div>
              {d.material_number && <div><div className="label">Material</div><Text className="mono" size="sm">{d.material_number}</Text></div>}
              {d.erp_order_number && <div><div className="label">Order / op</div>
                <Link to={`/work-orders/${d.work_order_id}`} className="mono" style={{ color: "inherit" }}>{d.erp_order_number}</Link>
                {d.op_no && <Text span className="mono" c="dimmed"> / {d.op_no}</Text>}</div>}
              {d.lot_number && <div><div className="label">Lot</div>
                <Link to={`/traceability?lot=${d.lot_id}`} className="mono" style={{ color: "inherit" }}>{d.lot_number}</Link></div>}
              {d.quantity && <div><div className="label">Qty</div><Text className="mono" size="sm">{num(d.quantity, 3)} {d.uom}</Text></div>}
              {d.inspection_lot_number && <div><div className="label">Inspection</div><Text className="mono" size="sm">{d.inspection_lot_number}</Text></div>}
            </Group>
            {d.stock.length > 0 && (
              <Group gap="xs" mt="sm">
                <Text size="xs" c="dimmed">Stock of the lot:</Text>
                {d.stock.map((s, i) => <Group key={i} gap={4}><Text size="xs" className="mono">{s.location_code} {num(s.quantity, 3)}</Text><StockStatus status={s.status} /></Group>)}
              </Group>
            )}
          </div>

          {d.disposition && (
            <div className="panel" style={{ padding: 12 }}>
              <div className="label">Disposition</div>
              <Text fw={600}>{label(meta.data, "dispositions", d.disposition)}</Text>
              <Text size="sm">{d.disposition_reason}</Text>
              <Text size="xs" c="dimmed">signed by {d.dispositioned_by} · {fmtDateTime(d.dispositioned_at)}</Text>
              {d.rework_good_qty != null && <Text size="sm" mt={4}>Rework result: <span className="mono">{num(d.rework_good_qty, 3)}</span> good, <span className="mono">{num(d.rework_scrap_qty, 3)}</span> scrap</Text>}
            </div>
          )}

          {canDecide && d.status === "OPEN" && (
            <div className="panel" style={{ padding: 12 }}>
              <div className="label" style={{ marginBottom: 6 }}>Decide disposition</div>
              <SegmentedControl fullWidth value={disp ?? ""} onChange={setDisp} data={dispOptions.map((x) => ({ value: x.code, label: x.label }))} />
              {disp && <Text size="xs" c="dimmed" mt={6}>{d.lot_id ? effect[disp] : "No stock is attached to this nonconformance: the decision is recorded only."}</Text>}
              <Group justify="flex-end" mt="sm">
                <Button disabled={!disp} onClick={() => setSigning("disp")}>Sign disposition…</Button>
              </Group>
            </div>
          )}
          {canDecide && d.status === "REWORK" && (
            <div className="panel" style={{ padding: 12 }}>
              <div className="label" style={{ marginBottom: 6 }}>Report rework result</div>
              {d.lot_id && <Text size="xs" c="dimmed" mb={6}>Blocked quantity to account for: <span className="mono">{num(blockedNow, 3)} {d.uom}</span></Text>}
              <Group grow>
                <NumberInput label="Good after rework" min={0} value={rework?.good ?? blockedNow} onChange={(v) => setRework((p) => ({ good: Number(v) || 0, scrap: p?.scrap ?? 0 }))} />
                <NumberInput label="Scrap" min={0} value={rework?.scrap ?? 0} onChange={(v) => setRework((p) => ({ good: p?.good ?? blockedNow, scrap: Number(v) || 0 }))} />
              </Group>
              <Group justify="flex-end" mt="sm">
                <Button onClick={() => { setRework((p) => p ?? { good: blockedNow, scrap: 0 }); setSigning("rework"); }}>Sign rework result…</Button>
              </Group>
            </div>
          )}
          <SignatureModal
            opened={signing !== null}
            meaning="APPROVED"
            subject={signing === "disp"
              ? `Disposition "${label(meta.data, "dispositions", disp)}" for ${d.number}${d.lot_number ? ` (lot ${d.lot_number})` : ""}`
              : `Rework result for ${d.number}: ${rework?.good ?? 0} good, ${rework?.scrap ?? 0} scrap`}
            busy={act.isPending}
            error={act.error instanceof ApiError ? act.error.message : null}
            onClose={() => { setSigning(null); act.reset(); }}
            onSign={(r) => act.mutate(r)}
          />
        </Stack>
      )}
    </Drawer>
  );
}
