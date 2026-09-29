import { Group, Progress, Stack, Text } from "@mantine/core";
import type { QueueOut } from "@mes/api-client";
import { IconCloudUpload } from "@tabler/icons-react";
import dayjs from "dayjs";
import { Elapsed, qty } from "../components/ui";
import type { ViewItem } from "../state/optimistic";

const STATE_LABEL: Record<string, string> = { SETUP: "Setup", RUNNING: "Running", PAUSED: "Paused" };

export function Work({
  data,
  items,
  onOpen,
}: {
  data: QueueOut;
  items: ViewItem[];
  onOpen: (operationId: string) => void;
}) {
  const runOn = new Map(items.filter((i) => i.active_run).map((i) => [i.active_run!.equipment_id, i]));
  return (
    <Stack gap="lg">
      <Group gap={10} wrap="nowrap" style={{ overflowX: "auto" }}>
        {data.machines.map((m) => {
          const it = runOn.get(m.id);
          const state = m.status !== "AVAILABLE" ? m.status : (it?.active_run?.status ?? "IDLE");
          return (
            <div key={m.id} className="t-machine" data-state={state}>
              <Group justify="space-between" gap={6} wrap="nowrap">
                <Text className="t-big" size="lg">{m.code}</Text>
                <Text className="label">{m.work_center_code}</Text>
              </Group>
              {m.status !== "AVAILABLE" ? (
                <Text size="sm" c="red.4" fw={700}>{m.status === "DOWN" ? "DOWN · maintenance called" : "IN MAINTENANCE"}</Text>
              ) : it ? (
                <>
                  <Text size="sm" className="mono">{it.erp_order_number}/{it.op_no}</Text>
                  <Text size="sm" c={state === "PAUSED" ? "amber.4" : "dimmed"}>
                    {STATE_LABEL[state]} · <Elapsed since={it.active_run!.phase_started_at} />
                  </Text>
                </>
              ) : (
                <Text size="sm" c="dimmed">Idle</Text>
              )}
            </div>
          );
        })}
      </Group>

      <Group justify="space-between" align="baseline">
        <div className="t-title" style={{ fontSize: 28 }}>Work queue · line {data.line_code}</div>
        <Text c="dimmed">{items.length} open operation{items.length === 1 ? "" : "s"}</Text>
      </Group>

      <Stack gap={10}>
        {items.map((i) => {
          const planned = Number(i.planned_qty);
          const done = Number(i.yield_qty);
          const machine = data.machines.find((m) => m.id === (i.active_run?.equipment_id ?? i.planned_equipment_id));
          const state = i.active_run?.status;
          return (
            <div key={i.operation_id} className="t-card" data-state={state} onClick={() => onOpen(i.operation_id)} role="button">
              <div>
                <Group gap="md" align="baseline">
                  <span className="t-big" style={{ fontSize: 30 }}>{i.erp_order_number}</span>
                  <span className="t-big" style={{ fontSize: 24, color: "var(--mes-text-dim)" }}>/ {i.op_no}</span>
                  <Text size="xl" fw={500}>{i.op_description}</Text>
                </Group>
                <Text size="md" c="dimmed">
                  <span className="mono">{i.material_number}</span> {i.material_description}
                </Text>
                <Group gap="sm" mt={8} wrap="nowrap">
                  <Progress value={(done / planned) * 100} w={220} size={10} color="green" bg="dark.5" />
                  <Text size="md" className="mono">{qty(done)} / {qty(planned)} {i.uom}</Text>
                  {Number(i.scrap_qty) > 0 && <Text size="md" c="red.4" className="mono">scrap {qty(i.scrap_qty)}</Text>}
                </Group>
              </div>
              <Stack gap={4} align="flex-end" justify="center">
                {i.wo_status === "ON_HOLD" && (
                  <Text className="t-title" style={{ fontSize: 22, color: "var(--mes-red)" }}>QUALITY HOLD</Text>
                )}
                {state ? (
                  <Text className="t-title" style={{ fontSize: 24, color: state === "PAUSED" ? "var(--mes-amber)" : state === "SETUP" ? "var(--mes-cyan)" : "var(--mes-green)" }}>
                    {STATE_LABEL[state]}
                  </Text>
                ) : i.previous_op_status && i.previous_op_status !== "CONFIRMED" ? (
                  <Text size="md" c="dimmed">waiting for previous op</Text>
                ) : (
                  <Text size="md" c="dimmed">ready</Text>
                )}
                <Text size="md" className="mono">{machine?.code ?? "—"}</Text>
                <Text size="sm" c="dimmed">
                  {i.planned_start ? `plan ${dayjs(i.planned_start).format("ddd HH:mm")}` : "not scheduled"} · due {dayjs(i.due).format("DD MMM")}
                </Text>
                {i.pendingActions > 0 && (
                  <Group gap={4} c="amber.4"><IconCloudUpload size={16} /><Text size="sm">{i.pendingActions} not yet synced</Text></Group>
                )}
              </Stack>
            </div>
          );
        })}
        {items.length === 0 && (
          <Text size="xl" c="dimmed" ta="center" py={60}>
            Nothing dispatched to this line. The supervisor dispatches orders from the planning board.
          </Text>
        )}
      </Stack>
    </Stack>
  );
}
