import {
  ActionIcon, Alert, Badge, Button, Drawer, Group, NumberInput, SegmentedControl, SimpleGrid, Stack, Switch, Table, Tabs,
  Text, TextInput, UnstyledButton,
} from "@mantine/core";
import { type AgentProposal, type AgentRun, type ChatSession, agent } from "@mes/api-client";
import {
  IconAlertTriangle, IconArrowsSort, IconCheck, IconChevronDown, IconChevronUp, IconCoin, IconFlask, IconHourglass,
  IconMessages, IconRobot, IconSearch, IconTicket, IconX, type Icon,
} from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { useMe } from "../auth";
import { PageHeader } from "../components/Layout";
import { ProposalCard, RunOutcome, RunSteps, TierBadge, Transcript, usd } from "../components/agent";
import { fmtDateTime, fromNow } from "../lib/format";
import { KnowledgePanel } from "../components/KnowledgePanel";
import { AssistantChat } from "./AssistantChat";

type Status = {
  health: { ok: boolean; error: string | null; at: string | null };
  model: string; key_configured: boolean; kill_switch: boolean; active: boolean; pending_proposals: number;
  last_24h: { ticket_outcomes: Record<string, number>; ticket_runs: Record<string, number>; chats: Record<string, number>;
    cost_usd: number; avg_ticket_minutes: number | null };
};
type PolicyRow = { action: string; label: string; mode: string; modes: string[]; scope: "ticket" | "chat"; updated_by: string | null; updated_at: string | null };
type Policies = { actions: PolicyRow[]; kill_switch: boolean; max_cost_per_ticket_usd: number; max_cost_per_chat_usd: number;
  max_cost_per_l2_usd: number };

/** Clickable KPI tile: icon + colour-accented top border + hover lift (same look as Work Orders/Maintenance/Quality). */
function ConsoleKpi({
  icon: Icon, label: lbl, value, sub, color, hot, onClick,
}: { icon: Icon; label: string; value: string | number; sub?: string; color: string; hot?: boolean; onClick?: () => void }) {
  return (
    <div className="panel kpi wo-kpi" style={{ borderTop: `3px solid ${color}`, cursor: onClick ? "pointer" : "default" }}
      onClick={onClick} role={onClick ? "button" : undefined} tabIndex={onClick ? 0 : undefined}
      onKeyDown={onClick ? (e) => e.key === "Enter" && onClick() : undefined}>
      <div className="wo-kpi-top"><span className="wo-kpi-icon" style={{ background: color }}><Icon size={13} stroke={2.2} /></span></div>
      <div className="label">{lbl}</div>
      <div className="value" style={{ color: hot ? color : undefined }}>{value}</div>
      {sub && <Text size="xs" c="dimmed" mt={2}>{sub}</Text>}
    </div>
  );
}

/** Quiet placeholder for a table/list with nothing in it yet, instead of dim text floating in empty space. */
function EmptyState({ icon: Icon, title, sub }: { icon: Icon; title: string; sub?: React.ReactNode }) {
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

/** Generic sortable column header, shared by the runs and chats tables below. Undecorated until a sort is picked. */
function SortTh<K extends string>({
  id, sort, onSort, align, children,
}: { id: K; sort: { key: K; dir: 1 | -1 } | null; onSort: (k: K) => void; align?: "right"; children: React.ReactNode }) {
  const active = sort?.key === id;
  return (
    <Table.Th style={align === "right" ? { textAlign: "right" } : undefined}>
      <UnstyledButton onClick={() => onSort(id)} className="sortable-th">
        <Group gap={4} wrap="nowrap" justify={align === "right" ? "flex-end" : "flex-start"}>
          <span>{children}</span>
          {active ? (sort!.dir === 1 ? <IconChevronUp size={12} /> : <IconChevronDown size={12} />) : <IconArrowsSort size={12} opacity={0.35} />}
        </Group>
      </UnstyledButton>
    </Table.Th>
  );
}
function toggleSort<K extends string>(setSort: (fn: (s: { key: K; dir: 1 | -1 } | null) => { key: K; dir: 1 | -1 }) => void, key: K) {
  setSort((s) => (s?.key === key ? { key, dir: s.dir === 1 ? -1 : 1 } : { key, dir: 1 }));
}
function sortRows<T, K extends string>(rows: T[], sort: { key: K; dir: 1 | -1 } | null, valueOf: Record<K, (r: T) => number | string>): T[] {
  if (!sort) return rows;
  const val = valueOf[sort.key];
  return [...rows].sort((a, b) => {
    const va = val(a);
    const vb = val(b);
    const cmp = typeof va === "number" && typeof vb === "number" ? va - vb : String(va).localeCompare(String(vb));
    return cmp * sort.dir;
  });
}

/** The AI support assistant (a chat for support engineers) and, one click away, the console of the AI agents. */
export function AiSupportPage() {
  const [params, setParams] = useSearchParams();
  const view = params.get("tab") || params.get("view") === "console" ? "console" : "assistant";
  if (view === "assistant") return <AssistantChat />;  // full screen (see Layout)
  return (
    <>
      <PageHeader title="AI Support Assistant"
        sub="AI support engineers: L1 works every new ticket and the terminal chat, L2 finds root causes and proposes fixes"
        right={<SegmentedControl value={view} onChange={(v) => setParams(v === "console" ? { view: "console" } : {})}
          data={[{ value: "assistant", label: "Assistant" }, { value: "console", label: "Agent console" }]} />} />
      <AgentConsole />
    </>
  );
}

function AgentConsole() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "activity";
  const goTab = (v: string) => setParams(v === "activity" ? { view: "console" } : { tab: v });
  const status = useQuery({ queryKey: ["agent", "status"], queryFn: () => agent<Status>("/status"), refetchInterval: 5000 });
  const s = status.data;
  const o = s?.last_24h.ticket_outcomes ?? {};
  const handled = Object.values(o).reduce((a, b) => a + b, 0);
  const chats = s?.last_24h.chats ?? {};
  return (
    <>
      {status.isError && <Alert color="red" icon={<IconAlertTriangle />} mb="md">The support agent service is not reachable.</Alert>}
      {s && s.active && !s.health.ok && (
        <Alert color="red" icon={<IconAlertTriangle />} mb="md" title={s.health.error?.startsWith("config:") ? "Claude API rejected the agent: paused" : "Claude API unavailable: retrying"}>
          {s.health.error?.replace(/^(config|transient): /, "")} · since {fromNow(s.health.at)}. Tickets are left untouched and are worked once it recovers.
        </Alert>
      )}
      {s && !s.active && (
        <Alert color="amber" icon={<IconRobot />} mb="md">
          {s.key_configured ? "The kill switch is on: the agent does nothing, tickets go to people." : "No ANTHROPIC_API_KEY configured: the agent is idle."}
        </Alert>
      )}
      <SimpleGrid cols={{ base: 2, md: 5 }} mb="md" className="rise">
        <ConsoleKpi icon={IconRobot} label="Agent" value={s ? (s.active ? "active" : "off") : "…"} sub={s?.model}
          color={s?.active ? "var(--mes-green)" : "var(--mes-amber)"} hot onClick={() => goTab("policy")} />
        <ConsoleKpi icon={IconTicket} label="Tickets worked (24 h)" value={handled} color="var(--mes-blue)"
          sub={`${o.RESOLVED ?? 0} resolved · ${o.ESCALATED ?? 0} escalated · ${o.HANDED_OVER ?? 0} handed over`} onClick={() => goTab("activity")} />
        <ConsoleKpi icon={IconHourglass} label="Awaiting approval" value={s?.pending_proposals ?? 0} color="var(--mes-amber)"
          hot={Boolean(s?.pending_proposals)} sub="proposed actions" onClick={() => goTab("approvals")} />
        <ConsoleKpi icon={IconMessages} label="Operator chats (24 h)" value={Object.values(chats).reduce((a, b) => a + b, 0)} color="var(--mes-blue)"
          sub={`${chats.FIXED ?? 0} fixed in chat · ${chats.TICKET_CREATED ?? 0} raised a ticket`} onClick={() => goTab("chats")} />
        <ConsoleKpi icon={IconCoin} label="Claude cost (24 h)" value={usd(s?.last_24h.cost_usd)} color="var(--mes-navy)"
          sub={s?.last_24h.avg_ticket_minutes != null ? `avg ${s.last_24h.avg_ticket_minutes} min per ticket` : undefined} />
      </SimpleGrid>
      <Tabs value={tab} onChange={(v) => goTab(v ?? "activity")}>
        <Tabs.List mb="md" className="console-tabs-list">
          <div className="console-tab-group">
            <span className="console-tab-group-label">Monitor</span>
            <div className="console-tab-group-tabs">
              <Tabs.Tab value="activity">Agent activity</Tabs.Tab>
              <Tabs.Tab value="approvals" rightSection={s?.pending_proposals ? <Badge size="xs" color="amber">{s.pending_proposals}</Badge> : null}>Approvals</Tabs.Tab>
              <Tabs.Tab value="chats">Operator chats</Tabs.Tab>
            </div>
          </div>
          <div className="console-tab-divider" />
          <div className="console-tab-group">
            <span className="console-tab-group-label">Configure</span>
            <div className="console-tab-group-tabs">
              <Tabs.Tab value="policy">Policy</Tabs.Tab>
              <Tabs.Tab value="evals">Evaluation</Tabs.Tab>
              <Tabs.Tab value="knowledge">Knowledge</Tabs.Tab>
            </div>
          </div>
        </Tabs.List>
        <Tabs.Panel value="activity"><Runs /></Tabs.Panel>
        <Tabs.Panel value="approvals"><Approvals /></Tabs.Panel>
        <Tabs.Panel value="chats"><Chats /></Tabs.Panel>
        <Tabs.Panel value="policy"><PolicyEditor /></Tabs.Panel>
        <Tabs.Panel value="evals"><Evals /></Tabs.Panel>
        <Tabs.Panel value="knowledge"><KnowledgePanel /></Tabs.Panel>
      </Tabs>
    </>
  );
}

type RunSortKey = "when" | "ticket" | "outcome" | "steps" | "cost";
const RUN_SORT_VALUE: Record<RunSortKey, (r: AgentRun) => number | string> = {
  when: (r) => r.created_at,
  ticket: (r) => r.ticket_number ?? r.chat_session_id ?? "",
  outcome: (r) => r.outcome ?? r.status,
  steps: (r) => r.steps,
  cost: (r) => Number(r.cost_usd ?? 0),
};

function Runs() {
  const [open, setOpen] = useState<AgentRun | null>(null);
  const [kind, setKind] = useState("TICKET");
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<{ key: RunSortKey; dir: 1 | -1 } | null>(null);
  const q = useQuery({ queryKey: ["agent", "runs", kind], queryFn: () => agent<AgentRun[]>(`/runs?kind=${kind}&limit=100`), refetchInterval: 4000 });
  const rows = useMemo(() => {
    const s = search.trim().toLowerCase();
    const filtered = (q.data ?? []).filter((r) => !s
      || (r.ticket_number ?? "").toLowerCase().includes(s) || (r.chat_session_id ?? "").toLowerCase().includes(s)
      || (r.summary ?? "").toLowerCase().includes(s) || (r.error ?? "").toLowerCase().includes(s));
    return sortRows(filtered, sort, RUN_SORT_VALUE);
  }, [q.data, search, sort]);
  return (
    <Stack>
      <Group justify="space-between" wrap="wrap">
        <SegmentedControl w={260} size="xs" value={kind} onChange={setKind} data={[{ value: "TICKET", label: "Ticket runs" }, { value: "CHAT", label: "Chat turns" }]} />
        <TextInput size="xs" w={280} leftSection={<IconSearch size={14} />}
          rightSection={search ? (
            <ActionIcon variant="subtle" color="gray" size="xs" onClick={() => setSearch("")} aria-label="Clear search"><IconX size={12} /></ActionIcon>
          ) : undefined}
          placeholder="Ticket, chat, summary…" value={search} onChange={(e) => setSearch(e.currentTarget.value)} />
      </Group>
      <Table className="panel" highlightOnHover>
        <Table.Thead>
          <Table.Tr>
            <SortTh id="when" sort={sort} onSort={(k) => toggleSort(setSort, k)}>When</SortTh>
            <SortTh id="ticket" sort={sort} onSort={(k) => toggleSort(setSort, k)}>{kind === "TICKET" ? "Ticket" : "Chat"}</SortTh>
            <SortTh id="outcome" sort={sort} onSort={(k) => toggleSort(setSort, k)}>Outcome</SortTh>
            <Table.Th>Summary</Table.Th>
            <SortTh id="steps" align="right" sort={sort} onSort={(k) => toggleSort(setSort, k)}>Steps</SortTh>
            <SortTh id="cost" align="right" sort={sort} onSort={(k) => toggleSort(setSort, k)}>Cost</SortTh>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {rows.map((r) => (
            <Table.Tr key={r.id} style={{ cursor: "pointer" }} onClick={() => setOpen(r)}>
              <Table.Td className="mono">{fmtDateTime(r.created_at)}</Table.Td>
              <Table.Td>
                {r.ticket_number ? <Link to={`/tickets?ticket=${r.ticket_id}`} className="mono" style={{ color: "inherit" }} onClick={(e) => e.stopPropagation()}>{r.ticket_number}</Link>
                  : <Text size="xs" c="dimmed" className="mono">{r.chat_session_id?.slice(-8)}</Text>}
                <Group gap={4}><TierBadge tier={r.tier} /><Text size="10px" c="dimmed">{r.trigger.toLowerCase()}</Text></Group>
              </Table.Td>
              <Table.Td><RunOutcome run={r} /></Table.Td>
              <Table.Td><Text size="xs" lineClamp={2} maw={560}>{r.summary ?? r.error ?? ""}</Text></Table.Td>
              <Table.Td className="mono" style={{ textAlign: "right" }}>{r.steps}</Table.Td>
              <Table.Td className="mono" style={{ textAlign: "right" }}>{usd(r.cost_usd)}</Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      {q.data?.length === 0 && <EmptyState icon={IconRobot} title="Nothing yet" sub="New tickets are picked up within seconds." />}
      {Boolean(q.data?.length) && rows.length === 0 && <Text c="dimmed" size="sm" ta="center" py="md">No runs match your search.</Text>}
      <Drawer opened={Boolean(open)} onClose={() => setOpen(null)} position="right" size="xl"
        title={open && (
          <Group gap="sm">
            <IconRobot size={18} />
            {open.ticket_number ? <Link to={`/tickets?ticket=${open.ticket_id}`} className="mono" style={{ color: "var(--mes-blue)", fontWeight: 700 }}>{open.ticket_number}</Link>
              : <Text fw={700}>Chat turn</Text>}
            <Text size="xs" c="dimmed">{fmtDateTime(open.created_at)}</Text>
          </Group>
        )}>
        {open && <RunSteps runId={open.id} />}
      </Drawer>
    </Stack>
  );
}

function Approvals() {
  const pending = useQuery({ queryKey: ["agent", "proposals", "PENDING"], queryFn: () => agent<AgentProposal[]>("/proposals?status=PENDING"), refetchInterval: 4000 });
  const decided = useQuery({ queryKey: ["agent", "proposals", "decided"], queryFn: () => agent<AgentProposal[]>("/proposals?status=EXECUTED&status=FAILED&status=REJECTED&limit=30"), refetchInterval: 10000 });
  const [decidedSearch, setDecidedSearch] = useState("");
  const decidedRows = useMemo(() => {
    const s = decidedSearch.trim().toLowerCase();
    return (decided.data ?? []).filter((p) => !s
      || p.ticket_number.toLowerCase().includes(s) || p.summary.toLowerCase().includes(s)
      || p.action.toLowerCase().includes(s) || (p.decided_by ?? "").toLowerCase().includes(s));
  }, [decided.data, decidedSearch]);
  return (
    <SimpleGrid cols={{ base: 1, lg: 2 }}>
      <Stack gap="xs">
        <Text className="label">Waiting for a decision</Text>
        {(pending.data ?? []).map((p) => <ProposalCard key={p.id} p={p} showTicket />)}
        {pending.data?.length === 0 && <EmptyState icon={IconCheck} title="No proposals waiting" sub='Actions the policy marks "suggest" appear here for a supervisor to approve or reject.' />}
      </Stack>
      <Stack gap="xs">
        <Group justify="space-between" wrap="nowrap">
          <Text className="label">Decided</Text>
          <TextInput size="xs" w={200} leftSection={<IconSearch size={14} />}
            rightSection={decidedSearch ? (
              <ActionIcon variant="subtle" color="gray" size="xs" onClick={() => setDecidedSearch("")} aria-label="Clear search"><IconX size={12} /></ActionIcon>
            ) : undefined}
            placeholder="Ticket, action, summary…" value={decidedSearch} onChange={(e) => setDecidedSearch(e.currentTarget.value)} />
        </Group>
        {decidedRows.map((p) => <ProposalCard key={p.id} p={p} showTicket />)}
        {decided.data?.length === 0 && <Text size="sm" c="dimmed">Nothing decided yet.</Text>}
        {Boolean(decided.data?.length) && decidedRows.length === 0 && <Text size="sm" c="dimmed">No decisions match your search.</Text>}
      </Stack>
    </SimpleGrid>
  );
}

const CHAT_COLOR: Record<string, string> = { FIXED: "green", TICKET_CREATED: "blue", OPEN: "amber", CLOSED: "gray" };
const CHAT_FILTERS = [
  { value: "all", label: "All" }, { value: "OPEN", label: "Open" }, { value: "FIXED", label: "Fixed" },
  { value: "TICKET_CREATED", label: "Ticket raised" }, { value: "CLOSED", label: "Closed" },
];
type ChatSortKey = "started" | "operator" | "terminal" | "cost";
const CHAT_SORT_VALUE: Record<ChatSortKey, (c: ChatSession) => number | string> = {
  started: (c) => c.started_at,
  operator: (c) => c.user_name ?? "",
  terminal: (c) => c.terminal_code ?? "",
  cost: (c) => Number(c.cost_usd ?? 0),
};

function Chats() {
  const [open, setOpen] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");
  const [sort, setSort] = useState<{ key: ChatSortKey; dir: 1 | -1 } | null>(null);
  const q = useQuery({ queryKey: ["agent", "chats"], queryFn: () => agent<ChatSession[]>("/chat/sessions?limit=100"), refetchInterval: 5000 });
  const rows = useMemo(() => {
    const s = search.trim().toLowerCase();
    const filtered = (q.data ?? []).filter((c) => (filter === "all" || c.status === filter) && (!s
      || (c.user_name ?? "").toLowerCase().includes(s) || (c.terminal_code ?? "").toLowerCase().includes(s) || (c.line_code ?? "").toLowerCase().includes(s)
      || (c.ticket_number ?? "").toLowerCase().includes(s)
      || (c.transcript.find((e) => e.role === "operator")?.text ?? "").toLowerCase().includes(s)));
    return sortRows(filtered, sort, CHAT_SORT_VALUE);
  }, [q.data, search, filter, sort]);
  const current = q.data?.find((c) => c.id === open);
  return (
    <Stack>
      <Group justify="space-between" wrap="wrap">
        <SegmentedControl size="xs" value={filter} onChange={setFilter} data={CHAT_FILTERS} />
        <TextInput size="xs" w={280} leftSection={<IconSearch size={14} />}
          rightSection={search ? (
            <ActionIcon variant="subtle" color="gray" size="xs" onClick={() => setSearch("")} aria-label="Clear search"><IconX size={12} /></ActionIcon>
          ) : undefined}
          placeholder="Operator, terminal, ticket…" value={search} onChange={(e) => setSearch(e.currentTarget.value)} />
      </Group>
      <Table className="panel" highlightOnHover>
        <Table.Thead>
          <Table.Tr>
            <SortTh id="started" sort={sort} onSort={(k) => toggleSort(setSort, k)}>Started</SortTh>
            <SortTh id="operator" sort={sort} onSort={(k) => toggleSort(setSort, k)}>Operator</SortTh>
            <SortTh id="terminal" sort={sort} onSort={(k) => toggleSort(setSort, k)}>Terminal</SortTh>
            <Table.Th>First message</Table.Th>
            <Table.Th>Result</Table.Th>
            <SortTh id="cost" align="right" sort={sort} onSort={(k) => toggleSort(setSort, k)}>Cost</SortTh>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {rows.map((c) => (
            <Table.Tr key={c.id} style={{ cursor: "pointer" }} onClick={() => setOpen(c.id)}>
              <Table.Td className="mono">{fmtDateTime(c.started_at)}</Table.Td>
              <Table.Td>{c.user_name}</Table.Td>
              <Table.Td className="mono">{c.terminal_code} · {c.line_code}</Table.Td>
              <Table.Td><Text size="xs" lineClamp={1} maw={380}>{c.transcript.find((e) => e.role === "operator")?.text ?? "—"}</Text></Table.Td>
              <Table.Td>
                <Group gap={4}>
                  <Badge size="sm" variant="light" color={CHAT_COLOR[c.status]}>{c.status.replace("_", " ").toLowerCase()}</Badge>
                  {c.ticket_number && <Link to={`/tickets?ticket=${c.ticket_id}`} className="mono" style={{ color: "inherit" }} onClick={(e) => e.stopPropagation()}>{c.ticket_number}</Link>}
                </Group>
              </Table.Td>
              <Table.Td className="mono" style={{ textAlign: "right" }}>{usd(c.cost_usd)}</Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      {q.data?.length === 0 && <EmptyState icon={IconMessages} title="No operator chats yet" sub="Chats started from the terminal's Get help screen appear here." />}
      {Boolean(q.data?.length) && rows.length === 0 && <Text c="dimmed" size="sm" ta="center" py="md">No chats match this filter.</Text>}
      <Drawer opened={Boolean(current)} onClose={() => setOpen(null)} position="right" size="lg"
        title={current && (
          <Group gap="sm">
            <Text fw={700}>{current.user_name}</Text>
            <Text size="xs" c="dimmed">{current.terminal_code} · {fromNow(current.started_at)}</Text>
            {current.ticket_number && <Link to={`/tickets?ticket=${current.ticket_id}`} className="mono" style={{ color: "var(--mes-blue)" }}>{current.ticket_number}</Link>}
          </Group>
        )}>
        {current && <Transcript entries={current.transcript} />}
      </Drawer>
    </Stack>
  );
}

const MODE_HELP: Record<string, string> = { off: "never", suggest: "a supervisor approves", auto: "agent acts on its own" };

/** One scope's actions (ticket-run or terminal-chat), grouped so the repeated "where" badge isn't needed per row. */
function ActionsTable({ rows, isAdmin, onSet }: { rows: PolicyRow[]; isAdmin: boolean; onSet: (action: string, mode: string) => void }) {
  return (
    <Table className="panel">
      <Table.Thead><Table.Tr><Table.Th>Action</Table.Th><Table.Th>Mode</Table.Th><Table.Th>Last changed</Table.Th></Table.Tr></Table.Thead>
      <Table.Tbody>
        {rows.map((a) => (
          <Table.Tr key={a.action}>
            <Table.Td><Text size="sm">{a.label}</Text><Text size="10px" c="dimmed" className="mono">{a.action}</Text></Table.Td>
            <Table.Td>
              <SegmentedControl size="xs" disabled={!isAdmin} value={a.mode} onChange={(m) => onSet(a.action, m)}
                data={a.modes.map((m) => ({ value: m, label: m }))} />
              <Text size="10px" c="dimmed">{MODE_HELP[a.mode]}</Text>
            </Table.Td>
            <Table.Td><Text size="xs" c="dimmed">{a.updated_by === "default" ? "default" : `${a.updated_by ?? ""} ${fromNow(a.updated_at)}`}</Text></Table.Td>
          </Table.Tr>
        ))}
      </Table.Tbody>
    </Table>
  );
}

function PolicyEditor() {
  const qc = useQueryClient();
  const me = useMe();
  const isAdmin = me.roles.includes("admin");
  const q = useQuery({ queryKey: ["agent", "policies"], queryFn: () => agent<Policies>("/policies") });
  const [draft, setDraft] = useState<Policies | null>(null);
  useEffect(() => { if (q.data) setDraft(q.data); }, [q.data]);
  const save = useMutation({
    mutationFn: (p: Policies) => agent<Policies>("/policies", {
      method: "PUT",
      body: JSON.stringify({ actions: Object.fromEntries(p.actions.map((a) => [a.action, a.mode])), kill_switch: p.kill_switch,
        max_cost_per_ticket_usd: p.max_cost_per_ticket_usd, max_cost_per_chat_usd: p.max_cost_per_chat_usd,
        max_cost_per_l2_usd: p.max_cost_per_l2_usd }),
    }),
    onSuccess: (p) => { qc.setQueryData(["agent", "policies"], p); qc.invalidateQueries({ queryKey: ["agent", "status"] }); },
  });
  if (!draft) return null;
  const dirty = JSON.stringify(draft) !== JSON.stringify(q.data);
  const set = (action: string, mode: string) => setDraft({ ...draft, actions: draft.actions.map((a) => (a.action === action ? { ...a, mode } : a)) });
  const ticketActions = draft.actions.filter((a) => a.scope === "ticket");
  const chatActions = draft.actions.filter((a) => a.scope === "chat");
  return (
    <Stack maw={980}>
      {!isAdmin && <Text size="sm" c="dimmed">Only administrators can change the policy.</Text>}
      <div className="panel" style={{ padding: 16, borderLeft: `3px solid var(--mes-${draft.kill_switch ? "red" : "line-strong"})` }}>
        <Group justify="space-between" align="flex-start" wrap="nowrap">
          <div>
            <Text fw={700} size="sm" mb={2}>Kill switch</Text>
            <Text size="xs" c="dimmed" maw={520}>Stops the AI agent completely: tickets go to people, the terminal chat is offered as the plain form.</Text>
          </div>
          <Switch checked={draft.kill_switch} disabled={!isAdmin} color="red" size="md"
            onChange={(e) => setDraft({ ...draft, kill_switch: e.currentTarget.checked })} />
        </Group>
        <SimpleGrid cols={3} mt="md" maw={640}>
          <NumberInput label="Cost limit per ticket ($)" size="xs" min={0.05} step={0.25} decimalScale={2} disabled={!isAdmin}
            value={draft.max_cost_per_ticket_usd} onChange={(v) => setDraft({ ...draft, max_cost_per_ticket_usd: Number(v) })} />
          <NumberInput label="Cost limit per L2 analysis ($)" size="xs" min={0.05} step={0.25} decimalScale={2} disabled={!isAdmin}
            value={draft.max_cost_per_l2_usd} onChange={(v) => setDraft({ ...draft, max_cost_per_l2_usd: Number(v) })} />
          <NumberInput label="Cost limit per chat ($)" size="xs" min={0.05} step={0.1} decimalScale={2} disabled={!isAdmin}
            value={draft.max_cost_per_chat_usd} onChange={(v) => setDraft({ ...draft, max_cost_per_chat_usd: Number(v) })} />
        </SimpleGrid>
      </div>
      <Text className="label">Ticket actions — L1 / L2 agents working a ticket</Text>
      <ActionsTable rows={ticketActions} isAdmin={isAdmin} onSet={set} />
      <Text className="label" mt="xs">Terminal chat actions — the operator-facing chat</Text>
      <ActionsTable rows={chatActions} isAdmin={isAdmin} onSet={set} />
      {isAdmin && (
        <Group>
          <Button disabled={!dirty} loading={save.isPending} onClick={() => save.mutate(draft)}>Save policy</Button>
          {dirty && <Button variant="subtle" onClick={() => setDraft(q.data ?? null)}>Discard</Button>}
          {save.error && <Text size="sm" c="red.7">{String(save.error.message)}</Text>}
        </Group>
      )}
    </Stack>
  );
}

type EvalResult = { id: string; title: string; tier: string; passed: boolean; checks: { check: string; ok: boolean; detail: string }[];
  actions: string[]; tools: string[]; cost_usd: number; seconds: number; summary: string; error: string | null };
type EvalRun = { id: string; model: string; started_at: string; total: number; passed: number; cost_usd: string; results: EvalResult[] };

function Evals() {
  const q = useQuery({ queryKey: ["agent", "evals"], queryFn: () => agent<EvalRun[]>("/evals") });
  const [openId, setOpenId] = useState<string | null>(null);
  const runs = q.data ?? [];
  const current = runs.find((r) => r.id === openId) ?? runs[0];
  if (!runs.length) {
    return (
      <EmptyState icon={IconFlask} title="No evaluation yet" sub={
        <>Run <Text span className="mono">docker compose exec support-agent-worker python -m support_agent.evals</Text> (real Claude calls, about $0.15-0.30 per scenario).</>
      } />
    );
  }
  return (
    <Stack>
      <Group gap="xs">
        {runs.map((r) => (
          <Button key={r.id} size="compact-xs" variant={r.id === current?.id ? "light" : "subtle"} color={r.passed === r.total ? "green" : "amber"}
            onClick={() => setOpenId(r.id)}>{fmtDateTime(r.started_at)} · {r.passed}/{r.total}</Button>
        ))}
      </Group>
      {current && (
        <>
          <Text size="sm" c="dimmed">{current.model} · {current.passed} of {current.total} scenarios passed · {usd(current.cost_usd)}</Text>
          <Table className="panel">
            <Table.Thead><Table.Tr><Table.Th>Scenario</Table.Th><Table.Th>Tier</Table.Th><Table.Th>Result</Table.Th><Table.Th>What the agent did</Table.Th><Table.Th>Cost</Table.Th></Table.Tr></Table.Thead>
            <Table.Tbody>
              {current.results.map((r) => (
                <Table.Tr key={r.id}>
                  <Table.Td><Text size="sm">{r.title}</Text><Text size="10px" c="dimmed" className="mono">{r.id}</Text></Table.Td>
                  <Table.Td><Badge size="xs" variant="outline" color="navy.6">{r.tier}</Badge></Table.Td>
                  <Table.Td>
                    <Badge size="sm" variant="light" color={r.passed ? "green" : "red"}>{r.passed ? "pass" : "fail"}</Badge>
                    {r.checks.filter((c) => !c.ok).map((c) => <Text key={c.check} size="10px" c="red.7">{c.check}: {c.detail}</Text>)}
                  </Table.Td>
                  <Table.Td><Text size="xs" className="mono">{r.actions.join(", ") || "—"}</Text><Text size="xs" c="dimmed" lineClamp={2}>{r.summary}</Text></Table.Td>
                  <Table.Td className="mono">{usd(r.cost_usd)}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </>
      )}
    </Stack>
  );
}
