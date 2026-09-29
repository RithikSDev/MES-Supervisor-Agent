/**
 * "Get help" on the terminal: one entry point. The support assistant (AI chat) is offered first; the three
 * plain paths stay for when it is off, offline, or the operator prefers them:
 *  - Machine problem   -> maintenance breakdown notification (machine down: run paused, downtime starts)
 *  - MES / system      -> support ticket; the impact question sets the severity (P1 line stop .. P4 request)
 *  - Quality problem   -> the defect report of the open operation
 * Both reports go through the offline queue, so they are never lost.
 */
import { Group, Modal, SimpleGrid, Stack, Text, TextInput } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { type QueueOut, coreRaw } from "@mes/api-client";
import { IconAlertTriangle, IconDeviceDesktopExclamation, IconRobot, IconTool, IconZoomExclamation } from "@tabler/icons-react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { actionQueue } from "../state/queue";
import { AssistantChat, type ChatContext } from "./Assistant";

type Reasons = { downtime_reasons: { code: string; label: string }[] };
type Step = "choose" | "assistant" | "machine" | "system";

const SYSTEM_KINDS = [
  { category: "MES_APP", label: "Terminal / MES screen", hint: "frozen, errors, can't book" },
  { category: "HARDWARE", label: "Scanner or label printer", hint: "offline, wrong labels" },
  { category: "DATA", label: "Wrong data", hint: "stuck lot, quantities, report totals" },
  { category: "ACCESS", label: "Login / PIN / access", hint: "new operator, locked out" },
];
const IMPACTS = [
  { impact: "LINE_STOP", label: "The line is stopped", sev: "P1", color: "var(--mes-red)" },
  { impact: "DEGRADED", label: "Working around it, slower", sev: "P2", color: "var(--mes-amber)" },
  { impact: "MINOR", label: "Minor, production continues", sev: "P3", color: "var(--mes-cyan)" },
  { impact: "SERVICE_REQUEST", label: "A request, nothing broken", sev: "P4", color: "var(--mes-text-dim)" },
];

export function HelpDialog({ queue, plantId, preset, context, onClose, onQuality }: {
  queue: QueueOut;
  plantId: string;
  preset?: { equipmentId: string };
  context?: ChatContext; // what the operator is working on (open operation), for the assistant
  onClose: () => void;
  onQuality?: () => void;
}) {
  const [step, setStep] = useState<Step>(preset ? "machine" : "choose");
  const [machine, setMachine] = useState<string | null>(preset?.equipmentId ?? null);
  const [down, setDown] = useState<boolean | null>(preset ? true : null);
  const [reason, setReason] = useState<string | null>(null);
  const [kind, setKind] = useState<string | null>(null);
  const [impact, setImpact] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const reasons = useQuery({ queryKey: ["maint-meta"], queryFn: () => coreRaw<Reasons>("/maintenance/meta"), staleTime: Infinity });

  const submit = async (path: string, body: Record<string, unknown>, label: string, done: string) => {
    setBusy(true);
    try {
      const r = await actionQueue.submit(path.includes("tickets") ? "ticket" : "breakdown", path, body, label, { workOrderId: "" });
      notifications.show(r.queued
        ? { color: "amber", title: "Saved on this terminal", message: `${label}: sent when the connection is back.` }
        : { color: "green", title: done, message: label, autoClose: 8000 });
      onClose();
    } catch (e) {
      notifications.show({ color: "red", title: "Not accepted", message: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const eq = queue.machines.find((m) => m.id === machine);
  const title = step === "machine" ? "Machine problem" : step === "system" ? "MES or system problem"
    : step === "assistant" ? "Support assistant" : "Get help";
  const chatContext: ChatContext = { ...context, equipment_code: context?.equipment_code ?? eq?.code };

  return (
    <Modal opened onClose={onClose} size={1100} centered title={<span className="t-title" style={{ fontSize: 30 }}>{title}</span>}>
      {step === "choose" && (
        <button className="t-choice help-choice assistant" style={{ width: "100%", marginBottom: 14 }} onClick={() => setStep("assistant")}>
          <Group gap={18} wrap="nowrap">
            <IconRobot size={48} color="var(--mes-cyan)" />
            <div>
              <div className="t-title" style={{ fontSize: 28 }}>Talk to the support assistant</div>
              <Text c="dimmed" size="lg">Describe the problem: it checks the machine, order and systems, helps you fix it, or raises a ticket for you</Text>
            </div>
          </Group>
        </button>
      )}
      {step === "assistant" && (
        <AssistantChat context={chatContext} onClose={onClose} onFallback={() => setStep("system")} />
      )}
      {step === "choose" && (
        <SimpleGrid cols={3} spacing={14}>
          <button className="t-choice help-choice" onClick={() => setStep("machine")}>
            <IconTool size={44} color="var(--mes-amber)" /><div className="t-title" style={{ fontSize: 26 }}>Machine problem</div>
            <Text c="dimmed">breakdown, alarm, tooling: goes to maintenance</Text>
          </button>
          <button className="t-choice help-choice" onClick={() => setStep("system")}>
            <IconDeviceDesktopExclamation size={44} color="var(--mes-cyan)" /><div className="t-title" style={{ fontSize: 26 }}>MES / system problem</div>
            <Text c="dimmed">terminal, scanner, printer, data, access: goes to IT support</Text>
          </button>
          <button className="t-choice help-choice" disabled={!onQuality} onClick={() => { onClose(); onQuality?.(); }}>
            <IconZoomExclamation size={44} color="var(--mes-red)" /><div className="t-title" style={{ fontSize: 26 }}>Quality problem</div>
            <Text c="dimmed">{onQuality ? "defect on the parts: goes to quality" : "open the operation first"}</Text>
          </button>
        </SimpleGrid>
      )}

      {step === "machine" && (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20 }}>
          <Stack gap="sm">
            <Text className="label" style={{ fontSize: 14 }}>Which machine?</Text>
            <SimpleGrid cols={3} spacing={8}>
              {queue.machines.map((m) => (
                <button key={m.id} className={`t-choice${machine === m.id ? " selected" : ""}`} onClick={() => setMachine(m.id)}
                  disabled={m.status === "DOWN"} style={{ minHeight: 64, opacity: m.status === "DOWN" ? 0.45 : 1 }}>
                  <div className="t-big" style={{ fontSize: 20 }}>{m.code}</div>
                  <Text size="xs" c={m.status === "AVAILABLE" ? "dimmed" : "red.4"}>{m.status === "AVAILABLE" ? m.work_center_code : m.status.toLowerCase()}</Text>
                </button>
              ))}
            </SimpleGrid>
            <Text className="label" style={{ fontSize: 14 }}>Is the machine stopped?</Text>
            <SimpleGrid cols={2} spacing={8}>
              <button className={`t-choice${down === true ? " selected" : ""}`} onClick={() => setDown(true)}>Yes, it is down</button>
              <button className={`t-choice${down === false ? " selected" : ""}`} onClick={() => setDown(false)}>No, still running</button>
            </SimpleGrid>
            {down && <Text c="red.4" size="lg">The run on {eq?.code ?? "the machine"} is paused and the machine is blocked until maintenance releases it.</Text>}
          </Stack>
          <Stack gap="sm">
            <Text className="label" style={{ fontSize: 14 }}>What kind of problem?</Text>
            <SimpleGrid cols={2} spacing={8}>
              {(reasons.data?.downtime_reasons ?? []).map((r) => (
                <button key={r.code} className={`t-choice${reason === r.code ? " selected" : ""}`} onClick={() => setReason(r.code)} style={{ minHeight: 56, fontSize: 17 }}>
                  {r.label}
                </button>
              ))}
            </SimpleGrid>
            <TextInput size="lg" placeholder="Alarm number or what you see (optional)" value={text} onChange={(e) => setText(e.currentTarget.value)} />
            <button className="t-btn warn" disabled={!machine || down === null || !reason || busy}
              onClick={() => submit("/maintenance/breakdowns",
                { equipment_id: machine, reason_code: reason, description: text || (reasons.data?.downtime_reasons.find((r) => r.code === reason)?.label ?? ""), machine_down: down },
                `${eq?.code}: ${down ? "machine down" : "problem reported"}`, "Maintenance notified")}>
              <IconTool size={28} /> Call maintenance
            </button>
          </Stack>
        </div>
      )}

      {step === "system" && (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20 }}>
          <Stack gap="sm">
            <Text className="label" style={{ fontSize: 14 }}>What is affected?</Text>
            {SYSTEM_KINDS.map((k) => (
              <button key={k.category} className={`t-choice${kind === k.category ? " selected" : ""}`} onClick={() => setKind(k.category)}>
                <div style={{ fontWeight: 600 }}>{k.label}</div><Text size="sm" c="dimmed">{k.hint}</Text>
              </button>
            ))}
          </Stack>
          <Stack gap="sm">
            <Text className="label" style={{ fontSize: 14 }}>How bad is it for production?</Text>
            {IMPACTS.map((i) => (
              <button key={i.impact} className={`t-choice${impact === i.impact ? " selected" : ""}`} onClick={() => setImpact(i.impact)}>
                <Group justify="space-between" wrap="nowrap"><span>{i.label}</span>
                  <span className="t-big" style={{ color: i.color, fontSize: 22 }}>{i.sev}</span></Group>
              </button>
            ))}
            <TextInput size="lg" placeholder="What happened? (optional)" value={text} onChange={(e) => setText(e.currentTarget.value)} />
            {impact === "LINE_STOP" && <Group gap={8}><IconAlertTriangle color="var(--mes-red)" /><Text c="red.4" size="lg">P1: support responds within 15 minutes.</Text></Group>}
            <button className="t-btn primary" disabled={!kind || !impact || busy}
              onClick={() => {
                const k = SYSTEM_KINDS.find((x) => x.category === kind)!;
                submit("/tickets", { plant_id: plantId, title: `${k.label}: ${text || k.hint}`.slice(0, 160), description: text || k.hint,
                  category: kind, impact }, `Support ticket (${IMPACTS.find((x) => x.impact === impact)!.sev})`, "Support ticket raised");
              }}>
              Send to support
            </button>
          </Stack>
        </div>
      )}
    </Modal>
  );
}
