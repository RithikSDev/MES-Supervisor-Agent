import { Badge, Button, Code, Drawer, Group, SimpleGrid, Stack, Table, Text, Timeline } from "@mantine/core";
import { itsm } from "@mes/api-client";
import { IconPlugConnected, IconPlugConnectedX } from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { useCan, useMe } from "../auth";
import { fmtDateTime, fromNow } from "../lib/format";
import { OutboundStatus } from "./badges";
import { ConnectionDrawer, type System, useConnections } from "./ItsmConnections";

type Connection = { system: string; mode: string; url: string; configured: boolean; reachable: boolean; detail: string[]; error: string | null };
type Poll = { name: string; watermark: string; last_run_at: string | null; last_success_at: string | null; last_error: string | null; last_published: number };
type Status = { mode: string; counts: Record<string, number>; polls: Poll[]; connections: Connection[] };
type Msg = {
  id: string; ticket_id: string; ticket_number: string; event_type: string; changes: string[]; origin: string | null; status: string;
  attempts: number; max_attempts: number; last_error: string | null; result: Record<string, string | null> | null; created_at: string;
  completed_at: string | null; next_attempt_at: string;
};
type Call = { system: string; tool: string; args: Record<string, unknown>; outcome?: string; ms?: number; status?: number; error?: string };
type Detail = Msg & { payload: Record<string, unknown>; attempts_log: { attempt_no: number; started_at: string; outcome: string; error: string | null; calls: Call[] }[] };

export function ItsmPanel() {
  const qc = useQueryClient();
  const canRetry = useCan("work_order:manage");
  const [open, setOpen] = useState<string | null>(null);
  const [configure, setConfigure] = useState<System | null>(null);
  const isAdmin = useMe().roles.includes("admin");
  const conns = useConnections();
  const status = useQuery({ queryKey: ["itsm-status"], queryFn: () => itsm<Status>("/api/v1/itsm/status"), refetchInterval: 10_000 });
  const msgs = useQuery({ queryKey: ["itsm-msgs"], queryFn: () => itsm<Msg[]>("/api/v1/itsm/messages?limit=100"), refetchInterval: 3_000 });
  const poll = useMutation({ mutationFn: () => itsm<{ published: number }>("/api/v1/itsm/poll", { method: "POST" }), onSuccess: () => qc.invalidateQueries({ queryKey: ["itsm-status"] }) });
  return (
    <Stack>
      <SimpleGrid cols={2}>
        {(status.data?.connections ?? []).map((m) => (
          <div key={m.system} className="panel" style={{ padding: 12 }} data-testid={`itsm-conn-${m.system}`}>
            <Group justify="space-between">
              <Group gap={8}>
                {m.reachable ? <IconPlugConnected size={18} color="var(--mes-green)" />
                  : <IconPlugConnectedX size={18} color={m.configured ? "var(--mes-red)" : "var(--mes-amber)"} />}
                <Text fw={700}>{m.system === "servicenow" ? "ServiceNow" : "Jira Cloud"}</Text>
                <Badge size="xs" variant="outline" color="navy.6">{m.mode === "rest" ? (m.system === "servicenow" ? "Table API" : "REST v3") : "MCP"}</Badge>
                {!m.configured && <Badge size="xs" color="amber" variant="light">not configured</Badge>}
              </Group>
              <Group gap={8}>
                {m.url && <Text size="xs" c="dimmed" className="mono">{m.url}</Text>}
                {isAdmin && conns.data?.managed_here && (
                  <Button size="compact-xs" variant={m.configured ? "default" : "filled"} onClick={() => setConfigure(m.system as System)}>
                    {m.configured ? "Configure" : "Set up"}
                  </Button>
                )}
              </Group>
            </Group>
            {m.reachable ? (
              <Group gap={4} mt={8}>{m.detail.map((t) => <Badge key={t} size="xs" variant="outline" color="navy.6" tt="none" className="mono">{t}</Badge>)}</Group>
            ) : <Text size="xs" c={m.configured ? "red.7" : "dimmed"} mt={6}>
              {m.configured ? m.error : m.system === "servicenow"
                ? `${m.error && !m.error.startsWith("not configured") ? `${m.error}. ` : ""}Not connected: tickets stay in MES and are sent once ServiceNow is set up${isAdmin ? "" : " (by an admin)"}.`
                : `${m.error && !m.error.startsWith("not configured") ? `${m.error}. ` : ""}Not connected: L3 escalations stay in ServiceNow until Jira is set up.`}
            </Text>}
            {(() => {
              const p = status.data?.polls.find((x) => x.name === m.system);
              return m.configured && p && <Text size="xs" c={p.last_error ? "red.7" : "dimmed"} mt={8}>changes read {fromNow(p.last_success_at)} · {p.last_published} published last run{p.last_error ? ` · ${p.last_error}` : ""}</Text>;
            })()}
          </div>
        ))}
      </SimpleGrid>
      <Group justify="space-between">
        <Text size="sm" c="dimmed">Every MES ticket change is applied to ServiceNow (and to Jira at L3) {status.data?.mode === "mcp" ? "through MCP tool calls" : "over their REST APIs"}; engineers' changes are read back every few seconds. {Object.entries(status.data?.counts ?? {}).map(([k, v]) => `${v} ${k.toLowerCase()}`).join(" · ")}</Text>
        {canRetry && <Button size="xs" variant="default" loading={poll.isPending} onClick={() => poll.mutate()}>Read changes now</Button>}
      </Group>
      <Table className="panel" highlightOnHover>
        <Table.Thead><Table.Tr><Table.Th>When</Table.Th><Table.Th>Ticket</Table.Th><Table.Th>Change</Table.Th><Table.Th>Status</Table.Th><Table.Th>Attempts</Table.Th><Table.Th>Result</Table.Th></Table.Tr></Table.Thead>
        <Table.Tbody>
          {(msgs.data ?? []).map((m) => (
            <Table.Tr key={m.id} style={{ cursor: "pointer" }} onClick={() => setOpen(m.id)}>
              <Table.Td className="mono">{fmtDateTime(m.created_at)}</Table.Td>
              <Table.Td><Link to={`/tickets?ticket=${m.ticket_id}`} className="mono" style={{ color: "inherit" }} onClick={(e) => e.stopPropagation()}>{m.ticket_number}</Link></Table.Td>
              <Table.Td><Text size="xs">{m.event_type.endsWith("created") ? "ticket created" : m.changes.join(", ")}{m.origin && m.origin !== "MES" ? ` (from ${m.origin.toLowerCase()})` : ""}</Text></Table.Td>
              <Table.Td><OutboundStatus status={m.status} /></Table.Td>
              <Table.Td className="mono">{m.attempts}/{m.max_attempts}</Table.Td>
              <Table.Td><Text size="xs" className="mono">{m.result ? [m.result.sn_number, m.result.jira_key].filter(Boolean).join(" · ") : m.last_error?.slice(0, 80)}</Text></Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      <ItsmDrawer id={open} canRetry={canRetry} onClose={() => setOpen(null)} />
      <ConnectionDrawer system={configure} onClose={() => setConfigure(null)} />
    </Stack>
  );
}

function ItsmDrawer({ id, canRetry, onClose }: { id: string | null; canRetry: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["itsm-msg", id], queryFn: () => itsm<Detail>(`/api/v1/itsm/messages/${id}`), enabled: Boolean(id), refetchInterval: 3_000 });
  const retry = useMutation({ mutationFn: () => itsm(`/api/v1/itsm/messages/${id}/retry`, { method: "POST" }), onSuccess: () => qc.invalidateQueries({ queryKey: ["itsm-msg", id] }) });
  const d = q.data;
  return (
    <Drawer opened={Boolean(id)} onClose={onClose} position="right" size="xl" title={d && <Group gap="sm"><Text fw={700} className="mono">{d.ticket_number}</Text><OutboundStatus status={d.status} /></Group>}>
      {d && (
        <Stack>
          {d.last_error && <Text size="sm" c="red.7">{d.last_error}</Text>}
          {canRetry && ["FAILED", "DEAD", "RETRY_WAIT"].includes(d.status) && <Button size="xs" w="fit-content" onClick={() => retry.mutate()}>Retry now</Button>}
          <Timeline bulletSize={12}>
            {d.attempts_log.map((a) => (
              <Timeline.Item key={a.attempt_no} color={a.outcome === "SYNCED" ? "green" : a.outcome === "PERMANENT" ? "red" : "amber"}
                title={<Text size="sm" fw={600}>Attempt {a.attempt_no} · {a.outcome.toLowerCase()} <Text span size="xs" c="dimmed">{fmtDateTime(a.started_at)}</Text></Text>}>
                {a.calls.map((c, i) => (
                  <Text key={i} size="xs" className="mono" c={c.outcome === "OK" ? undefined : "red.7"}>
                    {c.tool.includes(" ") ? `${c.system} ${c.tool}${c.args.what ? ` (${c.args.what})` : ""}` : `${c.system}.${c.tool}(${Object.keys(c.args).join(", ")})`}{" "}
                    {c.status ? `${c.status} ` : ""}{c.outcome?.toLowerCase()} {c.ms != null ? `${c.ms} ms` : ""}{c.error ? ` · ${c.error}` : ""}
                  </Text>
                ))}
                {a.error && <Text size="xs" c="red.7">{a.error}</Text>}
              </Timeline.Item>
            ))}
          </Timeline>
          <Text className="label">Event payload</Text>
          <Code block>{JSON.stringify(d.payload, null, 2)}</Code>
        </Stack>
      )}
    </Drawer>
  );
}
