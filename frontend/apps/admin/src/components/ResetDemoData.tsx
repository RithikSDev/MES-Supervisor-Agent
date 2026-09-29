import { Alert, Button, Group, List, Loader, Modal, Stack, Text, TextInput } from "@mantine/core";
import { agent, coreRaw, erp, itsm, mockSap, notify } from "@mes/api-client";
import { IconAlertTriangle, IconCheck, IconX } from "@tabler/icons-react";
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

type StepState = "waiting" | "running" | "done" | "failed";

// order matters: stop the AI and alerts first, empty MES before SAP orders are read in again
const STEPS: { label: string; run: () => Promise<unknown> }[] = [
  { label: "AI agents: runs, chats, proposals, policy, knowledge uploads", run: () => agent("/admin/reset", { method: "POST" }) },
  { label: "Alerts, on-call rota and the demo mailbox", run: () => notify("/admin/reset", { method: "POST" }) },
  { label: "ServiceNow / Jira sync log (connections kept)", run: () => itsm("/api/v1/itsm/admin/reset", { method: "POST" }) },
  { label: "MES data: orders, runs, stock, quality, maintenance, tickets, audit trail; demo plant re-created",
    run: () => coreRaw("/admin/reset-demo-data", { method: "POST", body: JSON.stringify({ confirm: "RESET" }) }) },
  { label: "Mock SAP: 40 new sample production orders", run: () => mockSap("/_mock/reset", { method: "POST", body: JSON.stringify({ seed_orders: 40 }) }) },
  { label: "SAP adapter: postings forgotten, orders read in again", run: () => erp("/api/v1/admin/reset", { method: "POST" }) },
];

/** Admin account menu -> Reset demo data: empty every service's data and start again from the demo setup. */
export function ResetDemoData({ opened, onClose }: { opened: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const [typed, setTyped] = useState("");
  const [states, setStates] = useState<StepState[] | null>(null);
  const [errors, setErrors] = useState<Record<number, string>>({});
  const running = states?.includes("running") ?? false;
  const finished = states !== null && !running;
  const failed = states?.includes("failed") ?? false;

  async function run() {
    const s: StepState[] = STEPS.map(() => "waiting");
    setStates([...s]);
    setErrors({});
    for (const [i, step] of STEPS.entries()) {
      s[i] = "running";
      setStates([...s]);
      try {
        await step.run();
        s[i] = "done";
      } catch (e) {
        s[i] = "failed";
        setErrors((x) => ({ ...x, [i]: e instanceof Error ? e.message : String(e) }));
      }
      setStates([...s]);
    }
  }

  function close() {
    if (running) return;
    if (finished) {
      qc.clear();
      window.location.assign("/");  // fresh plant, users and data: start the app again
      return;
    }
    setTyped("");
    setStates(null);
    onClose();
  }

  return (
    <Modal opened={opened} onClose={close} title={<Text fw={700}>Reset demo data</Text>} size="lg" centered
      closeOnClickOutside={!running} closeOnEscape={!running} withCloseButton={!running}>
      {states === null ? (
        <Stack>
          <Alert color="red" icon={<IconAlertTriangle />}>
            Everything goes back to a freshly installed MES. This can't be undone.
          </Alert>
          <div>
            <Text size="sm" fw={600}>Removed</Text>
            <List size="sm" spacing={2}>
              <List.Item>work orders, schedules, runs, confirmations, stock and movements</List.Item>
              <List.Item>quality records, genealogy, maintenance orders, downtime</List.Item>
              <List.Item>support tickets, the audit trail, AI runs and conversations, alerts</List.Item>
              <List.Item>enrolled terminals and changed PINs, AI policy changes, uploaded manuals, extra MCP servers</List.Item>
            </List>
            <Text size="sm" fw={600} mt="sm">Kept</Text>
            <List size="sm" spacing={2}>
              <List.Item>the ServiceNow and Jira connections (with their passwords / tokens)</List.Item>
              <List.Item>incidents and issues already in ServiceNow / Jira (MES no longer links to them)</List.Item>
              <List.Item>sign-in accounts (Keycloak) and the service logs</List.Item>
            </List>
            <Text size="sm" mt="sm" c="dimmed">Afterwards: the demo plant, machines, users, spare parts and preventive plans are
              set up again, and 40 new sample orders come in from the mock SAP.</Text>
          </div>
          <TextInput label="Type RESET to confirm" value={typed} onChange={(e) => setTyped(e.currentTarget.value)} autoComplete="off" />
          <Group justify="flex-end">
            <Button variant="default" onClick={close}>Cancel</Button>
            <Button color="red" disabled={typed !== "RESET"} onClick={run}>Reset all data</Button>
          </Group>
        </Stack>
      ) : (
        <Stack gap="xs">
          {STEPS.map((step, i) => (
            <div key={step.label}>
              <Group gap={10} wrap="nowrap">
                {states[i] === "running" ? <Loader size={16} /> : states[i] === "done" ? <IconCheck size={16} color="var(--mes-green)" />
                  : states[i] === "failed" ? <IconX size={16} color="var(--mes-red)" /> : <span style={{ width: 16 }} />}
                <Text size="sm" c={states[i] === "waiting" ? "dimmed" : undefined}>{step.label}</Text>
              </Group>
              {errors[i] && <Text size="xs" c="red.7" ml={26}>{errors[i]}</Text>}
            </div>
          ))}
          {finished && (
            <Alert mt="sm" color={failed ? "red" : "green"}>
              {failed ? "Some steps failed (see above); the others were reset. You can run the reset again."
                : "Done. The app restarts on the fresh data; sample orders arrive from SAP within a few seconds."}
            </Alert>
          )}
          <Group justify="flex-end">
            <Button disabled={running} onClick={close}>{finished ? "Start fresh" : "Resetting…"}</Button>
          </Group>
        </Stack>
      )}
    </Modal>
  );
}
