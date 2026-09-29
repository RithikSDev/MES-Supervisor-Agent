import {
  Anchor,
  Badge,
  Button,
  Checkbox,
  Group,
  Loader,
  Modal,
  NumberInput,
  Select,
  SimpleGrid,
  Stack,
  Table,
  Tabs,
  Text,
  Textarea,
  Timeline,
} from "@mantine/core";
import { IconArrowLeft, IconPlayerPause, IconPlayerPlay, IconSend } from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router";
import { core, type Operation, unwrap, type WorkOrderDetail } from "@mes/api-client";
import { useCan } from "../auth";
import { RecordAuditTrail } from "../components/AuditTrail";
import { ExecutionTab, MaterialsTab, QualityTab } from "../components/WorkOrderTabs";
import { ErpSync, OpStatus, WoStatus } from "../components/badges";
import { ReasonModal } from "../components/dialogs";
import { fmtDate, fmtDateTime, isLate, minutes, num } from "../lib/format";

export function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="label">{label}</div>
      <Text size="sm">{children}</Text>
    </div>
  );
}

export function WorkOrderDetailPage() {
  const { id } = useParams();
  const qc = useQueryClient();
  const canManage = useCan("work_order:manage");
  const canConfirm = useCan("confirmation:create");
  const canAudit = useCan("audit:read");
  const canQuality = useCan("quality:read");
  const [holdOpen, setHoldOpen] = useState(false);
  const [confirmOp, setConfirmOp] = useState<Operation | null>(null);

  const wo = useQuery({
    queryKey: ["work-order", id],
    queryFn: () => unwrap(core.GET("/api/v1/work-orders/{wo_id}", { params: { path: { wo_id: id! } } })),
    refetchInterval: 10_000,
  });
  const confirmations = useQuery({
    queryKey: ["confirmations", id],
    queryFn: () => unwrap(core.GET("/api/v1/work-orders/{wo_id}/confirmations", { params: { path: { wo_id: id! } } })),
    refetchInterval: (q) => (q.state.data?.some((c) => c.erp_sync_status === "PENDING") ? 2_000 : 15_000),
  });

  const refresh = (data?: WorkOrderDetail) => {
    if (data) qc.setQueryData(["work-order", id], data);
    qc.invalidateQueries({ queryKey: ["work-orders"] });
    qc.invalidateQueries({ queryKey: ["confirmations", id] });
  };
  const rv = () => ({ row_version: wo.data!.row_version });
  const dispatch = useMutation({
    mutationFn: () => unwrap(core.POST("/api/v1/work-orders/{wo_id}/dispatch", { params: { path: { wo_id: id! } }, body: rv() })),
    onSuccess: refresh,
  });
  const hold = useMutation({
    mutationFn: (reason: string) =>
      unwrap(core.POST("/api/v1/work-orders/{wo_id}/hold", { params: { path: { wo_id: id! } }, body: { ...rv(), reason } })),
    onSuccess: (d) => {
      refresh(d);
      setHoldOpen(false);
    },
  });
  const resume = useMutation({
    mutationFn: () => unwrap(core.POST("/api/v1/work-orders/{wo_id}/resume", { params: { path: { wo_id: id! } }, body: { ...rv(), reason: "" } })),
    onSuccess: refresh,
  });
  const priority = useMutation({
    mutationFn: (p: number) =>
      unwrap(core.PUT("/api/v1/work-orders/{wo_id}/priority", { params: { path: { wo_id: id! } }, body: { ...rv(), priority: p } })),
    onSuccess: refresh,
  });

  if (wo.isPending) return <Loader size="sm" />;
  if (wo.isError) return <Text c="red">{wo.error.message}</Text>;
  const w = wo.data;
  const late = isLate(w.erp_basic_end, w.status);
  const confirmable = ["DISPATCHED", "IN_PROGRESS"].includes(w.status);

  return (
    <Stack gap="md" className="rise">
      <Anchor component={Link} to="/work-orders" size="xs" c="dimmed">
        <Group gap={4}><IconArrowLeft size={12} /> Work orders</Group>
      </Anchor>

      <Group justify="space-between" align="flex-start">
        <div>
          <Group gap="sm" align="baseline">
            <Text className="mono" fz={28} fw={500} lh={1}>{w.erp_order_number}</Text>
            <WoStatus status={w.status} />
            {late && <Badge color="red" variant="filled" size="sm">LATE</Badge>}
          </Group>
          <Text mt={4}>
            <span className="mono">{w.material_number}</span> · {w.material_description}
          </Text>
        </div>
        {canManage && (
          <Group gap="xs">
            <Select
              size="xs"
              w={130}
              label="Priority"
              value={String(w.priority)}
              data={["1", "2", "3", "4", "5"].map((p) => ({ value: p, label: `${p}${p === "1" ? " (urgent)" : ""}` }))}
              onChange={(v) => v && priority.mutate(Number(v))}
              allowDeselect={false}
            />
            {w.status === "SCHEDULED" && (
              <Button mt={18} leftSection={<IconSend size={14} />} loading={dispatch.isPending} onClick={() => dispatch.mutate()}>
                Dispatch to floor
              </Button>
            )}
            {w.status === "ON_HOLD" ? (
              <Button mt={18} variant="light" leftSection={<IconPlayerPlay size={14} />} loading={resume.isPending} onClick={() => resume.mutate()}>
                Resume
              </Button>
            ) : (
              ["RELEASED", "SCHEDULED", "DISPATCHED", "IN_PROGRESS"].includes(w.status) && (
                <Button mt={18} variant="default" leftSection={<IconPlayerPause size={14} />} onClick={() => setHoldOpen(true)}>
                  Hold
                </Button>
              )
            )}
          </Group>
        )}
      </Group>

      <SimpleGrid cols={{ base: 3, lg: 6 }} className="panel" p="md" spacing="lg">
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

      <Tabs defaultValue="ops" keepMounted={false}>
        <Tabs.List>
          <Tabs.Tab value="ops">Operations ({w.operations.length})</Tabs.Tab>
          <Tabs.Tab value="conf">Confirmations ({confirmations.data?.length ?? 0})</Tabs.Tab>
          <Tabs.Tab value="exec">Execution</Tabs.Tab>
          <Tabs.Tab value="mat">Materials</Tabs.Tab>
          {canQuality && <Tabs.Tab value="qm">Quality</Tabs.Tab>}
          <Tabs.Tab value="comp">Components ({w.components.length})</Tabs.Tab>
          <Tabs.Tab value="hist">Status history</Tabs.Tab>
          {canAudit && <Tabs.Tab value="audit">Audit trail</Tabs.Tab>}
        </Tabs.List>

        <Tabs.Panel value="ops" pt="sm">
          <Table className="panel">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Op</Table.Th>
                <Table.Th>Description</Table.Th>
                <Table.Th>Work center</Table.Th>
                <Table.Th>Std. times</Table.Th>
                <Table.Th>Planned duration</Table.Th>
                <Table.Th>Yield / scrap</Table.Th>
                <Table.Th>Status</Table.Th>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {w.operations.map((o) => (
                <Table.Tr key={o.id}>
                  <Table.Td className="mono">{o.op_no}</Table.Td>
                  <Table.Td>{o.description}</Table.Td>
                  <Table.Td className="mono">{o.work_center_code}</Table.Td>
                  <Table.Td className="mono" fz="xs" c="dimmed">
                    setup {num(o.setup_min)} · run {num(o.run_min_per_unit, 2)}/pc
                  </Table.Td>
                  <Table.Td className="mono">{minutes(o.duration_min)}</Table.Td>
                  <Table.Td className="mono">{num(o.yield_qty)} / <Text span c={Number(o.scrap_qty) ? "red.7" : "dimmed"} inherit>{num(o.scrap_qty)}</Text></Table.Td>
                  <Table.Td><OpStatus status={o.status} /></Table.Td>
                  <Table.Td ta="right">
                    {canConfirm && confirmable && o.status !== "CONFIRMED" && (
                      <Button variant="light" onClick={() => setConfirmOp(o)}>Confirm…</Button>
                    )}
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
          {!confirmable && (
            <Text size="xs" c="dimmed" mt="xs">
              Operations can be confirmed once the order is dispatched (publish a schedule, then dispatch).
            </Text>
          )}
        </Tabs.Panel>

        <Tabs.Panel value="conf" pt="sm">
          <Table className="panel">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Reported</Table.Th>
                <Table.Th>Op</Table.Th>
                <Table.Th>Yield</Table.Th>
                <Table.Th>Scrap</Table.Th>
                <Table.Th>Rework</Table.Th>
                <Table.Th>Final</Table.Th>
                <Table.Th>By</Table.Th>
                <Table.Th>SAP posting</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {(confirmations.data ?? []).map((c) => (
                <Table.Tr key={c.id}>
                  <Table.Td className="mono">{fmtDateTime(c.reported_at)}</Table.Td>
                  <Table.Td className="mono">{w.operations.find((o) => o.id === c.operation_id)?.op_no}</Table.Td>
                  <Table.Td className="mono">{num(c.yield_qty)}</Table.Td>
                  <Table.Td className="mono">{num(c.scrap_qty)}</Table.Td>
                  <Table.Td className="mono">{num(c.rework_qty)}</Table.Td>
                  <Table.Td>{c.final ? <Badge size="xs" variant="outline">final</Badge> : ""}</Table.Td>
                  <Table.Td>{c.reported_by_name}</Table.Td>
                  <Table.Td>
                    <ErpSync
                      status={c.erp_sync_status}
                      group={c.erp_confirmation_group}
                      count={c.erp_confirmation_count}
                      error={c.erp_error}
                      reconciled={c.erp_reconciled}
                    />
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Tabs.Panel>

        <Tabs.Panel value="exec" pt="sm">
          <ExecutionTab workOrderId={w.id} />
        </Tabs.Panel>

        <Tabs.Panel value="mat" pt="sm">
          <MaterialsTab workOrderId={w.id} />
        </Tabs.Panel>

        {canQuality && (
          <Tabs.Panel value="qm" pt="sm">
            <QualityTab workOrderId={w.id} plantId={w.plant_id} operations={w.operations} />
          </Tabs.Panel>
        )}

        <Tabs.Panel value="comp" pt="sm">
          <Table className="panel">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Material</Table.Th>
                <Table.Th>Op</Table.Th>
                <Table.Th>Required</Table.Th>
                <Table.Th>Storage loc.</Table.Th>
                <Table.Th>Backflush</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {w.components.map((c) => (
                <Table.Tr key={c.id}>
                  <Table.Td>
                    <span className="mono">{c.material_number}</span> <Text span c="dimmed" size="xs">{c.material_description}</Text>
                  </Table.Td>
                  <Table.Td className="mono">{c.op_no}</Table.Td>
                  <Table.Td className="mono">{num(c.required_qty, 3)} {c.uom}</Table.Td>
                  <Table.Td className="mono">{c.storage_location}</Table.Td>
                  <Table.Td>{c.backflush ? "yes" : "no"}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Tabs.Panel>

        <Tabs.Panel value="hist" pt="md">
          <Timeline bulletSize={12} lineWidth={2} color="brand">
            {w.history.map((h) => (
              <Timeline.Item key={h.id} title={<Group gap={6}>{h.from_status && <><WoStatus status={h.from_status} />→</>}<WoStatus status={h.to_status} /></Group>}>
                <Text size="xs" c="dimmed" className="mono">{fmtDateTime(h.changed_at)} · {h.changed_by_name ?? h.changed_by}</Text>
                {h.reason && <Text size="sm">{h.reason}</Text>}
              </Timeline.Item>
            ))}
          </Timeline>
        </Tabs.Panel>

        {canAudit && (
          <Tabs.Panel value="audit" pt="sm">
            <div className="panel">
              <RecordAuditTrail recordIds={[w.id, ...w.operations.map((o) => o.id), ...(confirmations.data ?? []).map((c) => c.id)]} />
            </div>
          </Tabs.Panel>
        )}
      </Tabs>

      <ReasonModal
        opened={holdOpen}
        title={`Put ${w.erp_order_number} on hold`}
        confirmLabel="Hold order"
        busy={hold.isPending}
        onClose={() => setHoldOpen(false)}
        onSubmit={(r) => hold.mutate(r)}
      />
      <ConfirmOperationModal
        wo={w}
        op={confirmOp}
        onClose={() => setConfirmOp(null)}
        onDone={() => {
          setConfirmOp(null);
          qc.invalidateQueries({ queryKey: ["work-order", id] });
          refresh();
        }}
      />
    </Stack>
  );
}

function ConfirmOperationModal({
  wo,
  op,
  onClose,
  onDone,
}: {
  wo: WorkOrderDetail;
  op: Operation | null;
  onClose: () => void;
  onDone: () => void;
}) {
  const remaining = op ? Math.max(Number(op.planned_qty) - Number(op.yield_qty), 0) : 0;
  const [f, setF] = useState({ yield_qty: 0, scrap_qty: 0, rework_qty: 0, setup_min: 0, run_min: 0, labor_min: 0, final: false, comment: "" });
  const [lastOp, setLastOp] = useState<string | null>(null);
  if (op && op.id !== lastOp) {
    setLastOp(op.id);
    setF({ yield_qty: remaining, scrap_qty: 0, rework_qty: 0, setup_min: Number(op.setup_min), run_min: Math.round(remaining * Number(op.run_min_per_unit)), labor_min: 0, final: true, comment: "" });
  }
  const m = useMutation({
    mutationFn: () =>
      unwrap(
        core.POST("/api/v1/work-orders/{wo_id}/operations/{op_id}/confirmations", {
          params: { path: { wo_id: wo.id, op_id: op!.id } },
          body: {
            yield_qty: String(f.yield_qty),
            scrap_qty: String(f.scrap_qty),
            rework_qty: String(f.rework_qty),
            setup_min: String(f.setup_min),
            run_min: String(f.run_min),
            labor_min: String(f.labor_min),
            final: f.final,
            comment: f.comment || null,
          },
        }),
      ),
    onSuccess: onDone,
  });
  const set = (k: keyof typeof f) => (v: string | number) => setF((s) => ({ ...s, [k]: Number(v) || 0 }));
  return (
    <Modal opened={Boolean(op)} onClose={onClose} centered size="lg" title={<Text fw={600}>Confirm {wo.erp_order_number} / {op?.op_no} · {op?.description}</Text>}>
      <Stack>
        <Text size="xs" c="dimmed">
          Planned {num(op?.planned_qty)} {wo.uom}, already confirmed {num(op?.yield_qty)}. The confirmation goes to SAP through the ERP adapter.
        </Text>
        <SimpleGrid cols={3}>
          <NumberInput label="Yield" min={0} value={f.yield_qty} onChange={set("yield_qty")} data-autofocus />
          <NumberInput label="Scrap" min={0} value={f.scrap_qty} onChange={set("scrap_qty")} />
          <NumberInput label="Rework" min={0} value={f.rework_qty} onChange={set("rework_qty")} />
          <NumberInput label="Setup (min)" min={0} value={f.setup_min} onChange={set("setup_min")} />
          <NumberInput label="Machine (min)" min={0} value={f.run_min} onChange={set("run_min")} />
          <NumberInput label="Labour (min)" min={0} value={f.labor_min} onChange={set("labor_min")} />
        </SimpleGrid>
        <Textarea label="Comment" value={f.comment} onChange={(e) => setF((s) => ({ ...s, comment: e.currentTarget.value }))} autosize />
        <Checkbox
          checked={f.final}
          onChange={(e) => setF((s) => ({ ...s, final: e.currentTarget.checked }))}
          label="Final confirmation (operation finished; no more quantities expected)"
        />
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button loading={m.isPending} disabled={!f.yield_qty && !f.scrap_qty && !f.final} onClick={() => m.mutate()}>
            Confirm operation
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
