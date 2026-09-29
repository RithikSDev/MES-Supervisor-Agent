import { Badge, Button, Code, Collapse, Group, Loader, Stack, Text, TextInput, Timeline, UnstyledButton } from "@mantine/core";
import {
  type AgentProposal, type AgentRun, type AgentStep, type ChatEntry, type RootCause, agent,
} from "@mes/api-client";
import { IconChevronRight, IconHandStop, IconRobot, IconRefresh, IconThumbDown, IconThumbUp } from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { useCan } from "../auth";
import { fmtDateTime, fromNow } from "../lib/format";

const OUTCOME: Record<string, [string, string]> = {
  RESOLVED: ["resolved", "green"],
  FIXED_IN_CHAT: ["fixed in chat", "green"],
  DUPLICATE: ["duplicate", "gray"],
  ESCALATED: ["escalated", "blue"],
  RESOLVE_PROPOSED: ["resolve proposed", "amber"],
  ACTION_PROPOSED: ["action proposed", "amber"],
  TICKET_CREATED: ["ticket raised", "blue"],
  NOTED: ["diagnosed", "navy.6"],
  ROOT_CAUSE: ["root cause found", "grape"],
  NO_ACTION: ["no action", "gray"],
  HANDED_OVER: ["handed to human", "red"],
};

export function RunOutcome({ run }: { run: AgentRun }) {
  if (run.status === "QUEUED" || run.status === "RUNNING") return <Badge size="sm" variant="light" color="amber" leftSection={<Loader size={8} color="amber" />}>{run.status.toLowerCase()}</Badge>;
  if (run.status === "SKIPPED") return <Badge size="sm" variant="outline" color="gray">skipped</Badge>;
  const [label, color] = OUTCOME[run.outcome ?? ""] ?? [run.status.toLowerCase(), run.status === "FAILED" ? "red" : "gray"];
  return <Badge size="sm" variant="light" color={color}>{label}</Badge>;
}

export const usd = (v: string | number | null | undefined) => `$${Number(v ?? 0).toFixed(Number(v ?? 0) < 1 ? 3 : 2)}`;

const ACTION_LABEL: Record<string, string> = {
  resolve: "Resolve", set_severity: "Change severity", close_duplicate: "Close as duplicate", retry_sap: "Retry SAP posting",
  retry_itsm: "Retry ServiceNow sync", end_terminal_session: "End terminal session", machine_back_up: "Machine back up",
  add_note: "Add note", escalate: "Escalate", resync_order: "Re-read order from SAP",
};

export function ProposalCard({ p, showTicket = false }: { p: AgentProposal; showTicket?: boolean }) {
  const qc = useQueryClient();
  const canDecide = useCan("ticket:manage");
  const [note, setNote] = useState("");
  const decide = useMutation({
    mutationFn: (verb: "approve" | "reject") => agent<AgentProposal>(`/proposals/${p.id}/${verb}`, { method: "POST", body: JSON.stringify({ note: note || null }) }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["agent"] }); qc.invalidateQueries({ queryKey: ["ticket", p.ticket_id] }); },
  });
  const pending = p.status === "PENDING";
  return (
    <div className="panel" style={{ padding: 10, borderLeft: `3px solid var(--mes-${pending ? "amber" : p.status === "EXECUTED" ? "green" : p.status === "FAILED" ? "red" : "line-strong"})` }}>
      <Group justify="space-between" wrap="nowrap" align="flex-start">
        <div style={{ minWidth: 0 }}>
          <Group gap={6}>
            <Badge size="xs" variant="light" color="grape">{ACTION_LABEL[p.action] ?? p.action}</Badge>
            {showTicket && <Link to={`/tickets?ticket=${p.ticket_id}`} className="mono" style={{ fontSize: 12, color: "var(--mes-blue)" }}>{p.ticket_number}</Link>}
            <Text size="xs" c="dimmed">{fromNow(p.created_at)}</Text>
          </Group>
          <Text size="sm" mt={4}>{p.summary}</Text>
          {p.rationale && <Text size="xs" c="dimmed" mt={2}>Why: {p.rationale}</Text>}
          {!pending && (
            <Text size="xs" mt={4} c={p.status === "FAILED" ? "red.7" : p.status === "REJECTED" ? "dimmed" : "green.7"}>
              {p.status.toLowerCase()} by {p.decided_by} {fmtDateTime(p.decided_at)}{p.decision_note ? ` · ${p.decision_note}` : ""}{p.error ? ` · ${p.error}` : ""}
            </Text>
          )}
        </div>
        {pending && canDecide && (
          <Stack gap={4} style={{ flexShrink: 0 }}>
            <Group gap={4}>
              <Button size="xs" color="green" loading={decide.isPending && decide.variables === "approve"} onClick={() => decide.mutate("approve")}>Approve</Button>
              <Button size="xs" variant="default" loading={decide.isPending && decide.variables === "reject"} onClick={() => decide.mutate("reject")}>Reject</Button>
            </Group>
            <TextInput size="xs" placeholder="note (optional)" value={note} onChange={(e) => setNote(e.currentTarget.value)} w={180} />
          </Stack>
        )}
      </Group>
      {decide.error && <Text size="xs" c="red.7" mt={4}>{String(decide.error.message)}</Text>}
    </div>
  );
}

function StepBody({ s }: { s: AgentStep }) {
  const [open, setOpen] = useState(false);
  const out = s.output as Record<string, unknown> | string | null;
  if (s.kind === "MODEL") {
    const o = (out ?? {}) as { text?: string; tool_calls?: string[]; usage?: { input: number; output: number; cache_read: number } };
    return (
      <Stack gap={2}>
        {o.text && <Text size="sm" style={{ whiteSpace: "pre-wrap" }}>{o.text}</Text>}
        {o.tool_calls && o.tool_calls.length > 0 && <Text size="xs" c="dimmed">calls {o.tool_calls.join(", ")}</Text>}
        {o.usage && <Text size="10px" c="dimmed" className="mono">{o.usage.input} in · {o.usage.output} out · {o.usage.cache_read} cached</Text>}
      </Stack>
    );
  }
  return (
    <>
      <UnstyledButton onClick={() => setOpen((v) => !v)}>
        <Group gap={4}>
          <IconChevronRight size={12} style={{ transform: open ? "rotate(90deg)" : undefined }} />
          <Text size="xs" className="mono" c={s.is_error ? "red.7" : undefined}>
            {JSON.stringify(s.input ?? {}).slice(0, 140)}
          </Text>
        </Group>
      </UnstyledButton>
      <Collapse in={open}><Code block mt={4} style={{ maxHeight: 260, overflow: "auto" }}>{JSON.stringify(s.output, null, 2)}</Code></Collapse>
    </>
  );
}

export function RunSteps({ runId }: { runId: string }) {
  const q = useQuery({
    queryKey: ["agent", "run", runId],
    queryFn: () => agent<AgentRun & { trail: AgentStep[]; proposals: AgentProposal[];
      rating: { score: number; comment: string | null; rated_by: string } | null }>(`/runs/${runId}`),
    refetchInterval: (query) => (["QUEUED", "RUNNING"].includes(query.state.data?.status ?? "") ? 2000 : false),
  });
  const qc = useQueryClient();
  const canRate = useCan("ticket:manage");
  const rate = useMutation({
    mutationFn: (score: number) => agent(`/runs/${runId}/rating`, { method: "PUT", body: JSON.stringify({ score }) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["agent", "run", runId] }),
  });
  const r = q.data;
  if (!r) return <Loader size="sm" />;
  return (
    <Stack>
      <Group gap="xs">
        <RunOutcome run={r} />
        <Text size="xs" c="dimmed">{r.model} · {r.steps} steps · {usd(r.cost_usd)} · {(r.input_tokens + r.cache_read_tokens).toLocaleString()} tokens in ({r.cache_read_tokens.toLocaleString()} cached), {r.output_tokens.toLocaleString()} out</Text>
        {canRate && !["QUEUED", "RUNNING"].includes(r.status) && (
          <Group gap={2} ml="auto">
            <Text size="xs" c="dimmed" mr={4}>{r.rating ? `rated by ${r.rating.rated_by}` : "Was this right?"}</Text>
            <Button size="compact-xs" variant={r.rating?.score === 1 ? "light" : "subtle"} color="green" aria-label="helpful"
              onClick={() => rate.mutate(1)}><IconThumbUp size={14} /></Button>
            <Button size="compact-xs" variant={r.rating?.score === -1 ? "light" : "subtle"} color="red" aria-label="wrong"
              onClick={() => rate.mutate(-1)}><IconThumbDown size={14} /></Button>
          </Group>
        )}
      </Group>
      {r.summary && <Text size="sm">{r.summary}</Text>}
      {r.error && <Text size="sm" c="red.7">{r.error}</Text>}
      {r.proposals.map((p) => <ProposalCard key={p.id} p={p} />)}
      <Timeline bulletSize={14} lineWidth={2}>
        {r.trail.map((s) => (
          <Timeline.Item key={s.id} color={s.is_error ? "red" : s.kind === "MODEL" ? "grape" : s.kind === "ACTION" ? "green" : "navy.6"}
            title={<Group gap={6}><Text size="xs" fw={700}>{s.kind === "MODEL" ? "Claude" : s.name}</Text>
              <Text size="10px" c="dimmed" className="mono">{s.kind.toLowerCase()}{s.ms != null ? ` · ${s.ms} ms` : ""}</Text></Group>}>
            <StepBody s={s} />
          </Timeline.Item>
        ))}
      </Timeline>
    </Stack>
  );
}

export function Transcript({ entries }: { entries: ChatEntry[] }) {
  return (
    <Stack gap={6}>
      {entries.map((e, i) => (
        <div key={i} style={{ alignSelf: e.role === "operator" ? "flex-end" : "flex-start", maxWidth: "85%" }}>
          <div className="panel" style={{ padding: "6px 10px", background: e.role === "operator" ? "var(--mes-panel-2)" : undefined }}>
            <Text size="sm" style={{ whiteSpace: "pre-wrap" }}>{e.text || "…"}</Text>
            {e.choices && e.choices.length > 0 && <Group gap={4} mt={4}>{e.choices.map((c) => <Badge key={c} size="xs" variant="outline" color="navy.6" tt="none">{c}</Badge>)}</Group>}
            {e.ticket && <Badge size="xs" mt={4} color="blue" variant="light">{e.ticket}</Badge>}
          </div>
          <Text size="10px" c="dimmed" ta={e.role === "operator" ? "right" : "left"}>{e.role === "operator" ? "operator" : "assistant"} · {fmtDateTime(e.at)}</Text>
        </div>
      ))}
    </Stack>
  );
}

export function TierBadge({ tier }: { tier: string | null }) {
  return tier ? <Badge size="xs" variant="outline" color={tier === "L2" ? "grape" : "navy.6"}>AI {tier}</Badge> : null;
}

const CONFIDENCE_COLOR: Record<string, string> = { high: "green", medium: "amber", low: "red" };

export function RootCauseCard({ rca }: { rca: RootCause }) {
  return (
    <div className="panel" style={{ padding: 10 }} data-testid="root-cause">
      <Group gap={6} mb={4}>
        <Text className="label">Root cause</Text>
        <Badge size="xs" variant="light" color="grape">{rca.category.replace("_", " ").toLowerCase()}</Badge>
        <Badge size="xs" variant="outline" color={CONFIDENCE_COLOR[rca.confidence]}>{rca.confidence} confidence</Badge>
      </Group>
      <Text size="sm" fw={600}>{rca.root_cause}</Text>
      <Text size="xs" c="dimmed" mt={2}>Symptom: {rca.symptom}</Text>
      <Stack gap={0} mt={6}>{rca.evidence.map((e, i) => <Text key={i} size="xs" className="mono">· {e}</Text>)}</Stack>
      <Text size="sm" mt={6}><b>Fix:</b> {rca.fix}</Text>
      {rca.prevention && <Text size="xs" c="dimmed" mt={2}>Prevention: {rca.prevention}</Text>}
    </div>
  );
}

/** The AI agent's work on one ticket, for the ticket drawer. */
export function TicketAgentPanel({ ticketId }: { ticketId: string }) {
  const qc = useQueryClient();
  const canManage = useCan("ticket:manage");
  const [openRun, setOpenRun] = useState<string | null>(null);
  const q = useQuery({
    queryKey: ["agent", "ticket", ticketId],
    queryFn: () => agent<{ runs: AgentRun[]; proposals: AgentProposal[]; chat: { id: string; transcript: ChatEntry[] } | null;
      taken_over: { taken_by: string; note: string | null; taken_at: string } | null }>(`/tickets/${ticketId}`),
    refetchInterval: 4000,
    retry: false,
  });
  const rerun = useMutation({
    mutationFn: () => agent(`/tickets/${ticketId}/run`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["agent", "ticket", ticketId] }),
  });
  const [showChat, setShowChat] = useState(false);
  const takeover = useMutation({
    mutationFn: (take: boolean) => agent(`/tickets/${ticketId}/takeover`, take
      ? { method: "POST", body: JSON.stringify({ note: null }) } : { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["agent", "ticket", ticketId] }),
  });
  if (q.isError) return null; // agent service not reachable: the ticket works without it
  const d = q.data;
  const latest = d?.runs[0];
  const busy = latest && ["QUEUED", "RUNNING"].includes(latest.status);
  return (
    <div className="panel" style={{ padding: 12, borderLeft: "3px solid var(--mantine-color-grape-6)" }} data-testid="ticket-agent">
      <Group justify="space-between">
        <Group gap={6}><IconRobot size={16} /><Text className="label">AI support agent</Text>{latest && <TierBadge tier={latest.tier} />}{latest && <RunOutcome run={latest} />}</Group>
        <Group gap={4}>
          {d?.chat && <Button size="compact-xs" variant="subtle" onClick={() => setShowChat((v) => !v)}>{showChat ? "Hide" : "Show"} terminal chat</Button>}
          {canManage && !d?.taken_over && <Button size="compact-xs" variant="subtle" color="gray" leftSection={<IconHandStop size={12} />} loading={takeover.isPending} onClick={() => takeover.mutate(true)}>Take over</Button>}
          {canManage && <Button size="compact-xs" variant="subtle" leftSection={<IconRefresh size={12} />} disabled={busy} loading={rerun.isPending} onClick={() => rerun.mutate()}>{d?.taken_over ? "Hand back to AI" : "Run again"}</Button>}
        </Group>
      </Group>
      {!d ? <Loader size="xs" mt="xs" /> : (
        <Stack gap="xs" mt="xs">
          {d.taken_over && <Text size="sm" c="amber.8">{d.taken_over.taken_by} has taken this ticket over {fromNow(d.taken_over.taken_at)}: the AI does not act on it.</Text>}
          {!latest && <Text size="sm" c="dimmed">The agent has not looked at this ticket.</Text>}
          {(() => { const withRca = d.runs.find((r) => r.rca); return withRca?.rca ? <RootCauseCard rca={withRca.rca} /> : null; })()}
          {latest?.summary && <Text size="sm">{latest.summary}</Text>}
          {latest?.error && <Text size="xs" c="red.7">{latest.error}</Text>}
          {d.proposals.filter((p) => p.status === "PENDING").map((p) => <ProposalCard key={p.id} p={p} />)}
          {d.proposals.filter((p) => p.status !== "PENDING").slice(0, 3).map((p) => <ProposalCard key={p.id} p={p} />)}
          {showChat && d.chat && <Transcript entries={d.chat.transcript} />}
          {d.runs.length > 0 && (
            <Group gap={4}>
              {d.runs.slice(0, 5).map((r) => (
                <Button key={r.id} size="compact-xs" variant={openRun === r.id ? "light" : "subtle"} color="gray" onClick={() => setOpenRun(openRun === r.id ? null : r.id)}>
                  {r.tier ? `${r.tier} · ` : ""}{r.trigger.toLowerCase()} · {fromNow(r.created_at)}
                </Button>
              ))}
            </Group>
          )}
          {openRun && <RunSteps runId={openRun} />}
        </Stack>
      )}
    </div>
  );
}
