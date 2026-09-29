import { Center, Group, Loader, Menu, Stack, Switch, Text } from "@mantine/core";
import { useIdle } from "@mantine/hooks";
import {
  ApiError,
  type LoginOut,
  NetworkError,
  type QueueOut,
  type ReasonCodes,
  type TerminalInfo,
  coreRaw,
  isSimulatedOutage,
  setSimulatedOutage,
} from "@mes/api-client";
import { IconAlertTriangle, IconCloudOff, IconDots, IconHelpOctagon, IconLogout, IconWifi, IconX } from "@tabler/icons-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import dayjs from "dayjs";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Clock } from "./components/ui";
import { Enroll } from "./screens/Enroll";
import { HelpDialog } from "./screens/Help";
import { MaintenanceScreen } from "./screens/Maintenance";
import { Operation } from "./screens/Operation";
import { PinPad } from "./screens/PinPad";
import { Work } from "./screens/Work";
import { applyPending } from "./state/optimistic";
import { actionQueue, isOnline, useActionQueue } from "./state/queue";
import {
  type Session,
  cachedTerminalInfo,
  consumeEnrollFragment,
  loadSession,
  readDeviceToken,
  saveDevice,
  saveSession,
  saveTerminalInfo,
} from "./state/session";
import { read, write } from "./state/storage";

const IDLE_LOGOUT_MS = 10 * 60 * 1000;

export function App() {
  const [device, setDevice] = useState<string | null>(() => {
    const fromLink = consumeEnrollFragment();
    if (fromLink) saveDevice(fromLink);
    return fromLink ?? readDeviceToken();
  });
  const [info, setInfo] = useState<TerminalInfo | null>(cachedTerminalInfo());
  const [session, setSession] = useState<Session | null>(loadSession());

  useEffect(() => {
    if (!device) return;
    coreRaw<TerminalInfo>("/terminal/device", { headers: { "X-Terminal-Device": device } })
      .then((i) => {
        setInfo(i);
        saveTerminalInfo(i);
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) {
          saveDevice(null);
          setDevice(null);
          setInfo(null);
        } // offline: keep the cached terminal info
      });
  }, [device]);

  const logout = useCallback(async (callServer = true) => {
    if (callServer) await coreRaw("/terminal/logout", { method: "POST" }).catch(() => undefined);
    saveSession(null);
    setSession(null);
  }, []);

  if (!device) {
    return (
      <Enroll
        onEnrolled={(token, i) => {
          saveDevice(token, i);
          setDevice(token);
          setInfo(i);
        }}
      />
    );
  }
  if (!info) {
    return <Center h="100vh"><Loader color="green" type="bars" /></Center>;
  }
  if (!session) {
    return (
      <PinPad
        device={device}
        info={info}
        onLogin={(s: LoginOut) => {
          saveSession(s);
          setSession(s);
        }}
      />
    );
  }
  return <Shell session={session} info={info} onLogout={logout} />;
}

function Shell({ session, info, onLogout }: { session: Session; info: TerminalInfo; onLogout: (callServer?: boolean) => void }) {
  const qc = useQueryClient();
  const actions = useActionQueue();
  const [online, setOnline] = useState(isOnline());
  const [simulated, setSimulated] = useState(isSimulatedOutage());
  const [openOp, setOpenOp] = useState<string | null>(null);
  const [help, setHelp] = useState<{ equipmentId: string } | true | null>(null);
  const [defectSignal, setDefectSignal] = useState(0);
  const technician = (session.roles ?? []).includes("maintenance_tech");
  const [mode, setMode] = useState<"production" | "maintenance">(technician ? "maintenance" : "production");
  const idle = useIdle(IDLE_LOGOUT_MS, { initialState: false });

  useEffect(() => {
    if (idle) onLogout();
  }, [idle, onLogout]);

  // replay queued actions whenever we can
  useEffect(() => {
    const tick = () => {
      setOnline(isOnline());
      void actionQueue.flush().then(() => qc.invalidateQueries({ queryKey: ["queue"] }));
    };
    tick();
    const t = setInterval(tick, 4000);
    window.addEventListener("online", tick);
    window.addEventListener("offline", tick);
    return () => {
      clearInterval(t);
      window.removeEventListener("online", tick);
      window.removeEventListener("offline", tick);
    };
  }, [qc, simulated]);

  const queue = useQuery({
    queryKey: ["queue"],
    queryFn: () => coreRaw<QueueOut>("/execution/queue"),
    refetchInterval: 5000,
    retry: false,
  });
  useEffect(() => {
    if (queue.error instanceof ApiError && queue.error.status === 401) onLogout(false);
    if (queue.data) write("local", "mes.terminal.lastQueue", queue.data);
  }, [queue.error, queue.data, onLogout]);

  const reasons = useQuery({
    queryKey: ["reasons"],
    queryFn: async () => {
      const r = await coreRaw<ReasonCodes>("/execution/reason-codes");
      write("local", "mes.terminal.reasons", r);
      return r;
    },
    initialData: read<ReasonCodes>("local", "mes.terminal.reasons") ?? undefined,
    staleTime: 3600_000,
  });

  const data = queue.data ?? read<QueueOut>("local", "mes.terminal.lastQueue");
  const view = useMemo(() => (data ? applyPending(data.items, actions) : []), [data, actions]);
  const pending = actions.filter((a) => a.status === "pending");
  const failed = actions.filter((a) => a.status === "failed");
  const opItem = openOp ? view.find((v) => v.operation_id === openOp) : undefined;
  const stale = queue.isError && queue.error instanceof NetworkError;

  return (
    <div className="t-shell">
      <header className="t-header">
        <Stack gap={0}>
          <span className="t-title" style={{ fontSize: 24 }}>{info.name}</span>
          <Text size="sm" c="dimmed">{info.plant_code} · line {info.line_code}</Text>
        </Stack>
        <span className="t-big" style={{ fontSize: 34, marginLeft: 12 }}><Clock tz={info.plant_timezone} /></span>
        <div style={{ flex: 1 }} />
        {data && (
          <button className="t-btn warn small" style={{ width: 190 }} onClick={() => setHelp(true)}>
            <IconHelpOctagon /> Get help
          </button>
        )}
        <span className={`t-pill ${online ? "online" : "offline"}`}>
          {online ? <IconWifi size={18} /> : <IconCloudOff size={18} />}
          {online ? "online" : "offline"}
          {pending.length > 0 && ` · ${pending.length} to sync`}
        </span>
        <Stack gap={0} align="flex-end">
          <Text size="lg" fw={600}>{session.operator.display_name ?? session.operator.username}</Text>
          <Text size="sm" c="dimmed">{technician ? "maintenance technician" : "operator"}</Text>
        </Stack>
        <button className="t-btn ghost small" style={{ width: 150 }} onClick={() => onLogout()}>
          <IconLogout /> Log out
        </button>
        <Menu position="bottom-end" width={320}>
          <Menu.Target>
            <button className="t-btn ghost small" style={{ width: 64 }} aria-label="terminal menu"><IconDots /></button>
          </Menu.Target>
          <Menu.Dropdown>
            <Menu.Label>Demo</Menu.Label>
            <div style={{ padding: "6px 12px 12px" }}>
              <Switch
                size="lg"
                label="Simulate network outage"
                checked={simulated}
                onChange={(e) => {
                  setSimulatedOutage(e.currentTarget.checked);
                  setSimulated(e.currentTarget.checked);
                  setOnline(isOnline());
                }}
              />
            </div>
          </Menu.Dropdown>
        </Menu>
      </header>

      <main className="t-main">
        {(stale || !online) && (
          <Group gap="sm" mb="md" p="sm" style={{ background: "rgba(242,169,0,0.12)", border: "1px solid rgba(242,169,0,0.5)" }}>
            <IconCloudOff color="var(--mes-amber)" />
            <Text size="lg">
              No connection. Keep working: actions are saved on this terminal and sent automatically when the connection is back.
              {queue.dataUpdatedAt ? ` Showing data from ${dayjs(queue.dataUpdatedAt).format("HH:mm:ss")}.` : ""}
            </Text>
          </Group>
        )}
        {failed.map((f) => (
          <Group key={f.id} gap="sm" mb="sm" p="sm" wrap="nowrap" style={{ background: "rgba(229,72,77,0.12)", border: "1px solid rgba(229,72,77,0.5)" }}>
            <IconAlertTriangle color="var(--mes-red)" />
            <Text size="lg" style={{ flex: 1 }}>
              Not accepted after reconnecting: <b>{f.label}</b> ({dayjs(f.createdAt).format("HH:mm")}): {f.error}
            </Text>
            <button className="t-btn ghost small" style={{ width: 150 }} onClick={() => actionQueue.dismiss(f.id)}><IconX /> Dismiss</button>
          </Group>
        ))}
        {technician && (
          <Group gap={8} mb="md">
            {(["maintenance", "production"] as const).map((m) => (
              <button key={m} className={`t-choice${mode === m ? " selected" : ""}`} style={{ minHeight: 56, width: 220, textAlign: "center" }}
                onClick={() => { setMode(m); setOpenOp(null); }}>
                {m === "maintenance" ? "Maintenance" : "Production"}
              </button>
            ))}
          </Group>
        )}
        {!data ? (
          <Center py={80}><Loader color="green" /></Center>
        ) : mode === "maintenance" && technician ? (
          <MaintenanceScreen plantId={info.plant_id} lineCode={info.line_code} lineId={info.line_id} />
        ) : opItem ? (
          <Operation key={opItem.operation_id} item={opItem} queue={{ ...data, items: view }} reasons={reasons.data}
            onBack={() => setOpenOp(null)} onHelp={(equipmentId) => setHelp(equipmentId ? { equipmentId } : true)}
            defectSignal={defectSignal} />
        ) : (
          <Work data={data} items={view} onOpen={setOpenOp} />
        )}
        {help && data && (
          <HelpDialog queue={data} plantId={info.plant_id} preset={help === true ? undefined : help} onClose={() => setHelp(null)}
            context={opItem ? { erp_order_number: opItem.erp_order_number, op_no: opItem.op_no, screen: "operation",
              equipment_code: data.machines.find((m) => m.id === (opItem.active_run?.equipment_id ?? opItem.planned_equipment_id))?.code } : { screen: "work queue" }}
            onQuality={opItem ? () => setDefectSignal((n) => n + 1) : undefined} />
        )}
      </main>
    </div>
  );
}
