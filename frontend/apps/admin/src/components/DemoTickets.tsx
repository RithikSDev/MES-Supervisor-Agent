import { Alert, Button, Group, List, Modal, Stack, Text } from "@mantine/core";
import { coreRaw } from "@mes/api-client";
import { IconAlertTriangle } from "@tabler/icons-react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { usePlant } from "./plant";

type Loaded = { history: number; open: number; from: string; l3: number };

/** Admin account menu -> Load demo tickets: eight weeks of ticket history plus open tickets (forecasts, SLA reports,
 * assignment demos). They are real MES tickets and go to ServiceNow / Jira like any other. */
export function DemoTickets({ opened, onClose }: { opened: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const { plant } = usePlant();
  const load = useMutation({
    mutationFn: () => coreRaw<Loaded>("/admin/demo-tickets", { method: "POST", body: JSON.stringify({ plant_id: plant!.id }) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tickets"] }),
    meta: { silent: true },
  });
  const close = () => { load.reset(); onClose(); };
  const r = load.data;
  return (
    <Modal opened={opened} onClose={close} title={<Text fw={700}>Load demo tickets</Text>} size="lg" centered>
      {r ? (
        <Stack>
          <Alert color="green">
            Loaded {r.history} history tickets (since {r.from}) and {r.open} open tickets. ServiceNow incidents
            {r.l3 ? ` and ${r.l3} Jira issues` : ""} are created in the background over the next few minutes.
          </Alert>
          <Group justify="flex-end"><Button onClick={close}>Close</Button></Group>
        </Stack>
      ) : (
        <Stack>
          <Text size="sm">Adds realistic support tickets to {plant ? `${plant.code} · ${plant.name}` : "this plant"} for demos of
            the AI Support Assistant:</Text>
          <List size="sm" spacing={2}>
            <List.Item>about 8 weeks of history (mostly closed): busier weekdays, a slowly rising volume, one SAP outage
              day, SLA targets met and missed, severity changes and escalations</List.Item>
            <List.Item>13 open tickets right now: P1 to P4, every status, breached and at-risk SLA clocks, assigned to
              different support engineers (and some unassigned)</List.Item>
          </List>
          <Alert color="amber" icon={<IconAlertTriangle />}>
            Every ticket is also sent to the connected ServiceNow (about 150 incidents, backdated) and the few L3 tickets
            to Jira. The AI agent leaves demo tickets alone until you hand one to it. Loading twice is refused; use
            Reset demo data first to start again.
          </Alert>
          {load.error && <Text size="sm" c="red.7">{load.error.message}</Text>}
          <Group justify="flex-end">
            <Button variant="default" onClick={close}>Cancel</Button>
            <Button loading={load.isPending} disabled={!plant} onClick={() => load.mutate()}>Load demo tickets</Button>
          </Group>
        </Stack>
      )}
    </Modal>
  );
}
