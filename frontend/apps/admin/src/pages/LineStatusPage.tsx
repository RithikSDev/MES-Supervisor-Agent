import { Badge, Group, Progress, SimpleGrid, Stack, Text, Tooltip } from "@mantine/core";
import { type MachineState, type WorkCenterWip, core, streamEvents, unwrap } from "@mes/api-client";
import {
  IconAdjustmentsHorizontal, IconBroadcast, IconBroadcastOff, IconCircleDashed, IconPlayerPause, IconPlayerPlay,
  type Icon,
} from "@tabler/icons-react";
import { useQuery } from "@tanstack/react-query";
import dayjs from "dayjs";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import { PageHeader } from "../components/Layout";
import { usePlant } from "../components/plant";
import { num } from "../lib/format";

const STATE_COLOR: Record<string, string> = {
  RUNNING: "var(--mes-green)",
  SETUP: "var(--mes-cyan)",
  PAUSED: "var(--mes-amber)",
  IDLE: "var(--mes-line-strong)",
};
const STATE_ICON: Record<string, Icon> = {
  RUNNING: IconPlayerPlay, SETUP: IconAdjustmentsHorizontal, PAUSED: IconPlayerPause, IDLE: IconCircleDashed,
};

/** Quiet placeholder when the plant has no live machines (yet, or after a state filter). */
function EmptyState({ icon: Icon, title, sub }: { icon: Icon; title: string; sub?: string }) {
  return (
    <div className="panel" style={{ padding: "36px 20px", textAlign: "center" }}>
      <div style={{
        width: 40, height: 40, borderRadius: 10, background: "var(--mes-panel-2)", display: "grid", placeItems: "center",
        margin: "0 auto 10px", color: "var(--mes-text-dim)",
      }}>
        <Icon size={20} stroke={1.6} />
      </div>
      <Text fw={600} size="sm">{title}</Text>
      {sub && <Text size="xs" c="dimmed" mt={4} maw={440} mx="auto">{sub}</Text>}
    </div>
  );
}
const PAUSE_LABEL: Record<string, string> = {
  MATERIAL_SHORTAGE: "Material shortage",
  MACHINE_ISSUE: "Machine issue",
  QUALITY_CHECK: "Quality check",
  WAITING_INSTRUCTIONS: "Waiting for instructions",
  TOOL_CHANGE: "Tool change",
  BREAK: "Break",
  OTHER: "Other",
};

function Since({ at }: { at: string }) {
  const [, tick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => tick((x) => x + 1), 1000);
    return () => clearInterval(t);
  }, []);
  const s = Math.max(0, dayjs().diff(dayjs(at), "second"));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return <>{h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m ${String(s % 60).padStart(2, "0")}s`}</>;
}

export function LineStatusPage() {
  const { plant } = usePlant();
  const [machines, setMachines] = useState<Record<string, MachineState>>({});
  const [connected, setConnected] = useState(false);
  const [lastEvent, setLastEvent] = useState<string | null>(null);
  const [filter, setFilter] = useState<string | null>(null);

  const snapshot = useQuery({
    queryKey: ["live", plant?.id],
    queryFn: () => unwrap(core.GET("/api/v1/execution/live", { params: { query: { plant_id: plant!.id } } })),
    enabled: Boolean(plant),
    refetchInterval: 60_000, // safety net; changes arrive over the stream
  });
  const wip = useQuery({
    queryKey: ["wip", plant?.id],
    queryFn: () => unwrap(core.GET("/api/v1/execution/wip", { params: { query: { plant_id: plant!.id } } })),
    enabled: Boolean(plant),
    refetchInterval: 10_000,
  });

  useEffect(() => {
    if (snapshot.data) setMachines(Object.fromEntries(snapshot.data.machines.map((m) => [m.equipment_id, m])));
  }, [snapshot.data]);

  useEffect(() => {
    if (!plant) return;
    return streamEvents(
      `/execution/live/stream?plant_id=${plant.id}`,
      (event, data) => {
        if (event !== "machine") return;
        const m = JSON.parse(data) as MachineState;
        setMachines((prev) => ({ ...prev, [m.equipment_id]: m }));
        setLastEvent(new Date().toISOString());
      },
      setConnected,
    );
  }, [plant]);

  const byLine = useMemo(() => {
    const lines = new Map<string, Map<string, MachineState[]>>();
    Object.values(machines)
      .filter((m) => !filter || m.state === filter)
      .sort((a, b) => `${a.line_code}${a.work_center_code}${a.equipment_code}`.localeCompare(`${b.line_code}${b.work_center_code}${b.equipment_code}`))
      .forEach((m) => {
        const wcs = lines.get(m.line_code) ?? new Map<string, MachineState[]>();
        wcs.set(m.work_center_code, [...(wcs.get(m.work_center_code) ?? []), m]);
        lines.set(m.line_code, wcs);
      });
    return lines;
  }, [machines, filter]);

  const counts = Object.values(machines).reduce<Record<string, number>>((acc, m) => ({ ...acc, [m.state]: (acc[m.state] ?? 0) + 1 }), {});
  const wipByWc = new Map((wip.data ?? []).map((w: WorkCenterWip) => [w.work_center_code, w]));

  return (
    <>
      <PageHeader
        title="Line status"
        sub="Live from the shop-floor terminals"
        right={
          <Tooltip label={lastEvent ? `last update ${dayjs(lastEvent).format("HH:mm:ss")}` : "waiting for events"}>
            <Badge size="lg" variant={connected ? "outline" : "filled"} color={connected ? "green" : "amber"}
              leftSection={connected ? <IconBroadcast size={14} /> : <IconBroadcastOff size={14} />}>
              {connected ? "live" : "reconnecting"}
            </Badge>
          </Tooltip>
        }
      />
      <SimpleGrid cols={4} spacing={8} mb="md" className="rise">
        {(["RUNNING", "SETUP", "PAUSED", "IDLE"] as const).map((s) => {
          const Icon = STATE_ICON[s];
          return (
            <div key={s} className={`panel kpi wo-kpi${filter === s ? " selected" : ""}`} style={{ borderTop: `3px solid ${STATE_COLOR[s]}` }}
              onClick={() => setFilter((f) => (f === s ? null : s))} role="button" tabIndex={0}
              onKeyDown={(e) => e.key === "Enter" && setFilter((f) => (f === s ? null : s))}>
              <div className="wo-kpi-top"><span className="wo-kpi-icon" style={{ background: STATE_COLOR[s] }}><Icon size={13} stroke={2.2} /></span></div>
              <div className="label">{s.toLowerCase()}</div>
              <div className="value" style={{ color: s === "PAUSED" && counts[s] ? "var(--mes-amber)" : undefined }}>{counts[s] ?? 0}</div>
            </div>
          );
        })}
      </SimpleGrid>

      {byLine.size === 0 && (
        snapshot.isPending
          ? <EmptyState icon={IconBroadcast} title="Loading live machine states…" />
          : <EmptyState icon={IconBroadcastOff} title={filter ? `No machines are ${filter.toLowerCase()} right now` : "No machines reporting"}
            sub={filter ? "Clear the filter above to see every machine." : "Nothing is live for this plant yet."} />
      )}
      <Stack gap="lg">
        {[...byLine.entries()].map(([line, wcs]) => (
          <div key={line}>
            <Text className="label" mb={6} style={{ fontSize: 13 }}>Line {line}</Text>
            <Group align="stretch" gap="md" wrap="wrap">
              {[...wcs.entries()].map(([wc, list]) => {
                const w = wipByWc.get(wc);
                return (
                  <div key={wc} className="panel" style={{ padding: 10, minWidth: 260 }}>
                    <Group justify="space-between" mb={8}>
                      <Text ff="var(--mes-serif)" fw={600} size="lg" style={{ letterSpacing: "0.05em" }}>{wc}</Text>
                      <Tooltip label={w?.waiting_orders.length ? `waiting: ${w.waiting_orders.join(", ")}` : "nothing waiting"}>
                        <Text size="xs" c={w && Number(w.waiting_qty) > 0 ? "amber.8" : "dimmed"} className="mono">
                          WIP waiting {num(w?.waiting_qty ?? 0)}
                        </Text>
                      </Tooltip>
                    </Group>
                    <Stack gap={8}>
                      {list.map((m) => <MachineTile key={m.equipment_id} m={m} />)}
                    </Stack>
                  </div>
                );
              })}
            </Group>
          </div>
        ))}
      </Stack>
    </>
  );
}

function MachineTile({ m }: { m: MachineState }) {
  const planned = Number(m.op_planned_qty ?? 0);
  const good = Number(m.op_yield_qty ?? 0);
  return (
    <div
      style={{
        width: 280,
        padding: "8px 10px",
        background: "var(--mes-panel-2)",
        borderLeft: `4px solid ${STATE_COLOR[m.state] ?? STATE_COLOR.IDLE}`,
        backgroundImage: m.state === "PAUSED" ? "repeating-linear-gradient(-45deg, rgba(179,127,0,0.10) 0 10px, transparent 10px 20px)" : undefined,
      }}
    >
      <Group justify="space-between" wrap="nowrap">
        <Text className="mono" fw={600}>{m.equipment_code}</Text>
        <Text size="xs" fw={600} c={m.state === "IDLE" ? "dimmed" : undefined} style={{ color: m.state !== "IDLE" ? STATE_COLOR[m.state] : undefined }}>
          {m.state}{m.since && m.state !== "IDLE" ? " · " : ""}{m.since && m.state !== "IDLE" && <Since at={m.since} />}
        </Text>
      </Group>
      {m.state === "IDLE" ? (
        <Text size="xs" c="dimmed">{m.equipment_name}</Text>
      ) : (
        <>
          <Text size="sm">
            <Link to={`/work-orders/${m.work_order_id}`} className="mono" style={{ color: "inherit" }}>{m.erp_order_number}/{m.op_no}</Link>{" "}
            <Text span size="xs" c="dimmed">{m.material_number}</Text>
          </Text>
          {m.state === "PAUSED" && <Text size="xs" c="amber.8">{PAUSE_LABEL[m.pause_reason ?? ""] ?? m.pause_reason}</Text>}
          <Group gap={6} mt={4} wrap="nowrap">
            <Progress value={planned ? (good / planned) * 100 : 0} size={5} style={{ flex: 1 }} color="green" bg="navy.1" />
            <Text size="xs" className="mono">{num(good)}/{num(planned)}</Text>
          </Group>
          <Text size="xs" c="dimmed">{m.operator_name}</Text>
        </>
      )}
    </div>
  );
}
