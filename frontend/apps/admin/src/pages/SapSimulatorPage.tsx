import { Alert, Badge, Button, Group, NumberInput, SimpleGrid, Stack, Table, Tabs, Text, Tooltip } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import {
  IconAlertTriangle, IconArrowsExchange, IconBolt, IconBoltOff, IconCopyCheck, IconFileText, IconPlus, IconServer2, type Icon,
} from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { mockSap } from "@mes/api-client";
import { PageHeader } from "../components/Layout";
import { fmtDate, fmtDateTime } from "../lib/format";

/** Clickable KPI tile: icon + colour-accented top border (same look used across the app). */
function SapKpi({
  icon: Icon, label, value, sub, color, hot,
}: { icon: Icon; label: string; value: string | number; sub?: string; color: string; hot?: boolean }) {
  return (
    <div className="panel kpi wo-kpi" style={{ borderTop: `3px solid ${color}`, cursor: "default" }}>
      <div className="wo-kpi-top"><span className="wo-kpi-icon" style={{ background: color }}><Icon size={13} stroke={2.2} /></span></div>
      <div className="label">{label}</div>
      <div className="value" style={{ color: hot ? color : undefined }}>{value}</div>
      {sub && <Text size="xs" c="dimmed" mt={2}>{sub}</Text>}
    </div>
  );
}

type MockOrder = {
  order: string;
  material: string;
  description: string;
  planned_qty: string;
  unit: string;
  basic_start: string;
  basic_end: string;
  status: string;
  last_change: string;
  operations: { op: string; work_center: string; confirmed_yield: string; final: boolean }[];
};
type MockConf = { group: string; count: string; order: string; op: string; yield_qty: string; scrap_qty: string; final: boolean; text: string; entered_at: string };
type MockMatDoc = {
  document: string;
  year: string;
  movement_code: string;
  header_text: string;
  entered_at: string;
  items: { GoodsMovementType: string; Material: string; QuantityInEntryUnit: string; EntryUnit: string; StorageLocation?: string; IssuingOrReceivingStorageLoc?: string; ManufacturingOrder?: string; Batch?: string }[];
};
type Faults = { latency_ms: number; fail_next_503: number; fail_next_lock: number; commit_then_timeout_next: number; hang_seconds: number };

export function SapSimulatorPage() {
  const qc = useQueryClient();
  const [count, setCount] = useState<number>(5);
  const orders = useQuery({ queryKey: ["mock", "orders"], queryFn: () => mockSap<MockOrder[]>("/_mock/orders"), refetchInterval: 4000 });
  const confs = useQuery({ queryKey: ["mock", "confs"], queryFn: () => mockSap<MockConf[]>("/_mock/confirmations"), refetchInterval: 3000 });
  const docs = useQuery({ queryKey: ["mock", "docs"], queryFn: () => mockSap<MockMatDoc[]>("/_mock/material-documents"), refetchInterval: 3000 });
  const faults = useQuery({ queryKey: ["mock", "faults"], queryFn: () => mockSap<Faults>("/_mock/faults"), refetchInterval: 3000 });

  const act = useMutation({
    mutationFn: ({ order, action }: { order: string; action: string }) => mockSap(`/_mock/orders/${order}/${action}`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["mock"] }),
  });
  const generate = useMutation({
    mutationFn: () => mockSap<{ created: string[] }>("/_mock/orders/generate", { method: "POST", body: JSON.stringify({ count, release_ratio: 0.8 }) }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["mock"] });
      notifications.show({ title: `${r.created.length} production orders created in SAP`, message: "They reach MES on the next adapter poll (about 10 s).", color: "amber" });
    },
  });

  const duplicates = useMemo(() => {
    const seen = new Map<string, number>();
    (confs.data ?? []).forEach((c) => c.text && seen.set(c.text, (seen.get(c.text) ?? 0) + 1));
    (docs.data ?? []).forEach((d) => d.header_text && seen.set(d.header_text, (seen.get(d.header_text) ?? 0) + 1));
    return new Set([...seen].filter(([, n]) => n > 1).map(([t]) => t));
  }, [confs.data, docs.data]);
  const released = (orders.data ?? []).filter((o) => !o.status.startsWith("CRTD")).length;
  const faultsArmed = faults.data
    ? faults.data.fail_next_503 > 0 || faults.data.fail_next_lock > 0 || faults.data.commit_then_timeout_next > 0 || faults.data.latency_ms > 0
    : false;

  return (
    <>
      <PageHeader
        title="SAP simulator"
        sub="Mock S/4HANA On-Prem (PP, plant 1000). Acts as the ERP side for demos. It is not part of MES."
        right={
          <Group gap="xs" align="flex-end">
            <NumberInput size="xs" w={80} min={1} max={50} value={count} onChange={(v) => setCount(Number(v) || 1)} label="Orders" />
            <Button leftSection={<IconPlus size={14} />} loading={generate.isPending} onClick={() => generate.mutate()}>
              Create in SAP
            </Button>
          </Group>
        }
      />
      <SimpleGrid cols={{ base: 2, md: 5 }} mb="md" className="rise">
        <SapKpi icon={IconServer2} label="Production orders" value={orders.data?.length ?? "…"} color="var(--mes-blue)"
          sub={`${released} released`} />
        <SapKpi icon={IconArrowsExchange} label="Confirmations" value={confs.data?.length ?? "…"} color="var(--mes-blue)" />
        <SapKpi icon={IconFileText} label="Material documents" value={docs.data?.length ?? "…"} color="var(--mes-blue)" />
        <SapKpi icon={IconCopyCheck} label="Duplicate postings" value={duplicates.size} color="var(--mes-red)" hot={duplicates.size > 0} />
        <SapKpi icon={faultsArmed ? IconBolt : IconBoltOff} label="Fault injection" value={faultsArmed ? "armed" : "clean"}
          color={faultsArmed ? "var(--mes-amber)" : "var(--mes-green)"} hot={faultsArmed} />
      </SimpleGrid>
      <div className="hazard" style={{ padding: "6px 12px", marginBottom: 12 }}>
        <Text size="xs" c="amber.8">
          Everything on this page happens "inside SAP". MES only sees it through the ERP adapter: orders by polling OData,
          confirmations by posting to it.
        </Text>
      </div>

      <SimpleGrid cols={{ base: 1, lg: 3 }} spacing="md">
        <div style={{ gridColumn: "span 2" }}>
          <Tabs defaultValue="orders">
            <Tabs.List>
              <Tabs.Tab value="orders">Production orders ({orders.data?.length ?? 0})</Tabs.Tab>
              <Tabs.Tab value="confs">
                Confirmations received ({confs.data?.length ?? 0})
                {duplicates.size > 0 && <Badge ml={6} size="xs" color="red">DUPLICATES</Badge>}
              </Tabs.Tab>
              <Tabs.Tab value="docs">Material documents ({docs.data?.length ?? 0})</Tabs.Tab>
            </Tabs.List>
            <Tabs.Panel value="docs" pt="sm">
              <Table className="panel">
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Entered</Table.Th>
                    <Table.Th>Document</Table.Th>
                    <Table.Th>Movement</Table.Th>
                    <Table.Th>Material / batch</Table.Th>
                    <Table.Th>Qty</Table.Th>
                    <Table.Th>SLoc</Table.Th>
                    <Table.Th>Order</Table.Th>
                    <Table.Th>Header text (MES key)</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {(docs.data ?? []).map((d) =>
                    d.items.map((it, i) => (
                      <Table.Tr key={`${d.document}-${i}`} bg={duplicates.has(d.header_text) ? "rgba(229,72,77,0.12)" : undefined}>
                        <Table.Td className="mono">{fmtDateTime(d.entered_at)}</Table.Td>
                        <Table.Td className="mono">{d.document}/{d.year}</Table.Td>
                        <Table.Td className="mono">{it.GoodsMovementType}</Table.Td>
                        <Table.Td className="mono">{it.Material} <Text span size="xs" c="dimmed">{it.Batch}</Text></Table.Td>
                        <Table.Td className="mono">{Number(it.QuantityInEntryUnit)} {it.EntryUnit}</Table.Td>
                        <Table.Td className="mono">{it.StorageLocation}{it.IssuingOrReceivingStorageLoc ? ` → ${it.IssuingOrReceivingStorageLoc}` : ""}</Table.Td>
                        <Table.Td className="mono">{it.ManufacturingOrder}</Table.Td>
                        <Table.Td className="mono" fz="xs" c={duplicates.has(d.header_text) ? "red.7" : "dimmed"}>{d.header_text}</Table.Td>
                      </Table.Tr>
                    )),
                  )}
                  {docs.data?.length === 0 && (
                    <Table.Tr><Table.Td colSpan={8}><Text c="dimmed" size="sm">No material documents posted yet.</Text></Table.Td></Table.Tr>
                  )}
                </Table.Tbody>
              </Table>
            </Tabs.Panel>
            <Tabs.Panel value="orders" pt="sm">
              <Table className="panel">
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Order</Table.Th>
                    <Table.Th>Material</Table.Th>
                    <Table.Th>Qty</Table.Th>
                    <Table.Th>Basic dates</Table.Th>
                    <Table.Th>Ops confirmed</Table.Th>
                    <Table.Th>System status</Table.Th>
                    <Table.Th />
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {(orders.data ?? []).map((o) => (
                    <Table.Tr key={o.order}>
                      <Table.Td className="mono">{o.order}</Table.Td>
                      <Table.Td><span className="mono">{o.material}</span> <Text span size="xs" c="dimmed">{o.description}</Text></Table.Td>
                      <Table.Td className="mono">{Number(o.planned_qty)} {o.unit}</Table.Td>
                      <Table.Td className="mono">{fmtDate(o.basic_start)} → {fmtDate(o.basic_end)}</Table.Td>
                      <Table.Td className="mono">{o.operations.filter((x) => x.final).length}/{o.operations.length}</Table.Td>
                      <Table.Td className="mono">{o.status}</Table.Td>
                      <Table.Td>
                        <Group gap={4} justify="flex-end" wrap="nowrap">
                          {o.status.startsWith("CRTD") && (
                            <Button size="compact-xs" variant="light" onClick={() => act.mutate({ order: o.order, action: "release" })}>Release</Button>
                          )}
                          {!o.status.includes("TECO") && !o.status.includes("DLFL") && (
                            <>
                              <Button size="compact-xs" variant="default" onClick={() => act.mutate({ order: o.order, action: "teco" })}>TECO</Button>
                              <Button size="compact-xs" variant="subtle" color="red" onClick={() => act.mutate({ order: o.order, action: "delete-flag" })}>Delete flag</Button>
                            </>
                          )}
                        </Group>
                      </Table.Td>
                    </Table.Tr>
                  ))}
                  {orders.data?.length === 0 && (
                    <Table.Tr><Table.Td colSpan={7}><Text c="dimmed" size="sm">No production orders in SAP yet.</Text></Table.Td></Table.Tr>
                  )}
                </Table.Tbody>
              </Table>
            </Tabs.Panel>
            <Tabs.Panel value="confs" pt="sm">
              {duplicates.size > 0 && (
                <Alert color="red" icon={<IconAlertTriangle />} mb="sm" title="Duplicate postings in SAP">
                  The same MES confirmation was posted more than once. With the adapter's reconciliation this should never happen.
                </Alert>
              )}
              <Table className="panel">
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Entered</Table.Th>
                    <Table.Th>Conf. no.</Table.Th>
                    <Table.Th>Order / op</Table.Th>
                    <Table.Th>Yield</Table.Th>
                    <Table.Th>Scrap</Table.Th>
                    <Table.Th>Final</Table.Th>
                    <Table.Th>ConfirmationText (MES key)</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {(confs.data ?? []).map((c) => (
                    <Table.Tr key={`${c.group}-${c.count}`} bg={duplicates.has(c.text) ? "rgba(229,72,77,0.12)" : undefined}>
                      <Table.Td className="mono">{fmtDateTime(c.entered_at)}</Table.Td>
                      <Table.Td className="mono">{c.group}/{c.count}</Table.Td>
                      <Table.Td className="mono">{c.order} / {c.op}</Table.Td>
                      <Table.Td className="mono">{Number(c.yield_qty)}</Table.Td>
                      <Table.Td className="mono">{Number(c.scrap_qty)}</Table.Td>
                      <Table.Td>{c.final ? "X" : ""}</Table.Td>
                      <Table.Td className="mono" fz="xs" c={duplicates.has(c.text) ? "red.7" : "dimmed"}>{c.text}</Table.Td>
                    </Table.Tr>
                  ))}
                  {confs.data?.length === 0 && (
                    <Table.Tr><Table.Td colSpan={7}><Text c="dimmed" size="sm">No confirmations posted yet.</Text></Table.Td></Table.Tr>
                  )}
                </Table.Tbody>
              </Table>
            </Tabs.Panel>
          </Tabs>
        </div>
        {faults.data && <FaultPanel current={faults.data} />}
      </SimpleGrid>
    </>
  );
}

function FaultPanel({ current }: { current: Faults }) {
  const qc = useQueryClient();
  const [f, setF] = useState<Faults>(current);
  const [dirty, setDirty] = useState(false);
  useEffect(() => {
    if (!dirty) setF(current);
  }, [current, dirty]);
  const save = useMutation({
    mutationFn: (next: Faults) => mockSap<Faults>("/_mock/faults", { method: "PUT", body: JSON.stringify(next) }),
    onSuccess: (r) => {
      setF(r);
      setDirty(false);
      qc.invalidateQueries({ queryKey: ["mock", "faults"] });
    },
  });
  const field = (k: keyof Faults, label: string, hint: string) => (
    <Tooltip label={hint} multiline w={280} position="left">
      <NumberInput
        size="xs"
        label={label}
        min={0}
        value={f[k]}
        onChange={(v) => {
          setDirty(true);
          setF((s) => ({ ...s, [k]: Number(v) || 0 }));
        }}
      />
    </Tooltip>
  );
  return (
    <div className="panel" style={{ padding: 14, alignSelf: "flex-start" }}>
      <Group gap={6} mb="xs">
        <IconBolt size={16} color="var(--mes-amber)" />
        <Text fw={600}>Fault injection</Text>
      </Group>
      <Text size="xs" c="dimmed" mb="sm">
        Break SAP on purpose and watch the adapter recover (SAP integration page). Counters apply to the next POSTs.
      </Text>
      <Stack gap="xs">
        {field("commit_then_timeout_next", "Commit, then time out (next N)", "SAP saves the confirmation but answers too late. The adapter sees a timeout, reconciles on retry, and does not post a duplicate.")}
        {field("fail_next_lock", "Order locked (next N)", "SAP rejects with 'order is being processed by another user' (retryable).")}
        {field("fail_next_503", "HTTP 503 (next N)", "Gateway unavailable; nothing committed (retryable).")}
        {field("latency_ms", "Latency (ms)", "Added to every SAP request.")}
        {field("hang_seconds", "Hang duration (s)", "How long the 'commit, then time out' case stalls. The adapter times out after 5 s.")}
        <Group justify="flex-end" mt={4}>
          <Button
            size="xs"
            variant="default"
            onClick={() => save.mutate({ latency_ms: 0, fail_next_503: 0, fail_next_lock: 0, commit_then_timeout_next: 0, hang_seconds: 15 })}
          >
            Reset
          </Button>
          <Button size="xs" disabled={!dirty} loading={save.isPending} onClick={() => save.mutate(f)}>Apply</Button>
        </Group>
      </Stack>
    </div>
  );
}
