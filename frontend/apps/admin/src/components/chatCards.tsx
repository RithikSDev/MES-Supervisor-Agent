import { BarChart } from "@mantine/charts";
import { Anchor, Badge, Button, Group, Loader, SegmentedControl, SimpleGrid, Stack, Table, Text, UnstyledButton } from "@mantine/core";
import {
  type AgentProposal, type Alert, type ChatCard, type Ticket, type TicketFilter, agent, core, coreRaw, erp, itsm, notify,
  unwrap,
} from "@mes/api-client";
import {
  IconArrowUpRight, IconBell, IconChartBar, IconCheck, IconChecklist, IconClockExclamation, IconDownload, IconLifebuoy,
  IconPlugConnected, IconRobot, IconTrendingUp, IconTool, IconUsers, IconX, type Icon,
} from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type ReactNode, useState } from "react";
import { Link } from "react-router";
import { TicketDrawer, Clock, SeverityBadge, useSupportTeam } from "../pages/TicketsPage";
import { useMe } from "../auth";
import { type Cell, csvName, downloadCsv } from "../lib/csv";
import { fmtDateTime, fromNow, num } from "../lib/format";
import { ProposalCard, RunSteps } from "./agent";
import { usePlant } from "./plant";

const OPEN = ["NEW", "ACKNOWLEDGED", "IN_PROGRESS", "ON_HOLD"];
const SEVERITIES = ["P1", "P2", "P3", "P4"];

// --------------------------------------------------------------------------------------------- ticket filter
// the same rules as the assistant's side (support_agent/engineer.py), so a card shows what the assistant counted

function clockState(t: Ticket, c: Ticket["response"]): "breached" | "at_risk" | "ok" {
  if (c.breached) return "breached";
  if (c.met_at || !c.due || c.remaining_s == null) return "ok";
  const total = (new Date(c.due).getTime() - new Date(t.reported_at).getTime()) / 1000;
  return total > 0 && c.remaining_s <= total * 0.25 ? "at_risk" : "ok";
}

export function slaState(t: Ticket): "breached" | "at_risk" | "ok" {
  const s = [clockState(t, t.response), clockState(t, t.resolution)];
  return s.includes("breached") ? "breached" : s.includes("at_risk") ? "at_risk" : "ok";
}

export function matches(t: Ticket, f: TicketFilter): boolean {
  const scope = f.scope ?? "open";
  if (scope === "open" && !OPEN.includes(t.status)) return false;
  if (scope === "resolved" && !["RESOLVED", "CLOSED"].includes(t.status)) return false;
  if (f.severity?.length && !f.severity.includes(t.severity)) return false;
  if (f.tier?.length && !f.tier.includes(t.tier)) return false;
  if (f.status?.length && !f.status.includes(t.status)) return false;
  if (f.category?.length && !f.category.includes(t.category)) return false;
  if (f.sla && slaState(t) !== f.sla) return false;
  if (f.machine && (t.equipment_code ?? "").toUpperCase() !== f.machine.toUpperCase()) return false;
  if (f.line && (t.line_code ?? "").toUpperCase() !== f.line.toUpperCase()) return false;
  if (f.assignee && !assigneeMatches(t.assignee_name, f.assignee, f.me)) return false;
  if (f.text) {
    const hay = [t.number, t.title, t.description, t.sn_number, t.jira_key, t.equipment_code, t.line_code, t.erp_order_number]
      .filter(Boolean).join(" ").toLowerCase();
    if (!hay.includes(f.text.toLowerCase())) return false;
  }
  return true;
}

function assigneeMatches(name: string | null, want: string, me?: string): boolean {
  const n = (name ?? "").toLowerCase();
  const w = want.trim().toLowerCase();
  if (w === "unassigned") return !n;
  if (w === "me") return Boolean(n) && n === (me ?? "").toLowerCase();
  if (w === "others") return Boolean(n) && n !== (me ?? "").toLowerCase();
  return Boolean(n) && n.includes(w);
}

function describe(f: TicketFilter): string {
  const bits = [f.scope === "all" ? "all" : f.scope === "resolved" ? "resolved" : "open"];
  if (f.tier?.length) bits.push(f.tier.join("/"));
  if (f.status?.length) bits.push(f.status.map((s) => s.toLowerCase().replace("_", " ")).join(", "));
  if (f.category?.length) bits.push(f.category.map((c) => c.toLowerCase()).join(", "));
  if (f.sla) bits.push(f.sla === "breached" ? "SLA missed" : "SLA at risk");
  if (f.machine) bits.push(f.machine);
  if (f.line) bits.push(`line ${f.line}`);
  if (f.text) bits.push(`"${f.text}"`);
  if (f.assignee) bits.push(f.assignee === "others" ? "assigned to others" : f.assignee === "me" ? "assigned to me"
    : f.assignee === "unassigned" ? "unassigned" : `assigned to ${f.assignee}`);
  return bits.join(" · ");
}

// --------------------------------------------------------------------------------------------- frame

function Frame({ icon: I, title, sub, right, children }: { icon: Icon; title: string; sub?: ReactNode; right?: ReactNode; children: ReactNode }) {
  return (
    <div className="panel chat-card">
      <Group justify="space-between" wrap="nowrap" className="chat-card-head">
        <Group gap={10} wrap="nowrap" style={{ minWidth: 0 }}>
          <span className="chat-card-icon"><I size={15} stroke={1.8} /></span>
          <div style={{ minWidth: 0 }}>
            <div className="chat-card-title">{title}</div>
            {sub && <Text size="xs" c="dimmed" truncate>{sub}</Text>}
          </div>
        </Group>
        {right}
      </Group>
      <div className="chat-card-body">{children}</div>
    </div>
  );
}

const OpenLink = ({ to, label }: { to: string; label: string }) => (
  <Anchor component={Link} to={to} size="xs" style={{ whiteSpace: "nowrap" }}>{label} <IconArrowUpRight size={12} style={{ verticalAlign: -2 }} /></Anchor>
);

/** Downloads what the card shows (all rows, not only the visible ones) as a CSV file. */
function CsvButton({ name, rows, disabled }: { name: string; rows: () => Cell[][]; disabled?: boolean }) {
  return (
    <Button size="compact-xs" variant="light" leftSection={<IconDownload size={13} />} disabled={disabled}
      aria-label={`Download ${name} as CSV`} onClick={() => downloadCsv(csvName(name), rows())}>CSV</Button>
  );
}

const Actions = ({ children }: { children: ReactNode }) => <Group gap={10} wrap="nowrap">{children}</Group>;

const iso = (v: string | null | undefined) => (v ? new Date(v).toISOString().replace("T", " ").slice(0, 16) + " UTC" : "");

function useTicketMeta() {
  return useQuery({ queryKey: ["ticket-meta"], queryFn: () => unwrap(core.GET("/api/v1/tickets/meta")), staleTime: Infinity });
}

// --------------------------------------------------------------------------------------------- tickets

function TicketsCard({ card }: { card: ChatCard }) {
  const { plant } = usePlant();
  const me = useMe();
  const f0 = card.filter ?? { scope: "open" };
  // "me" / "others" are relative to whoever looks at the card
  const f = { ...f0, me: me.display_name ?? me.username };
  const openScope = (f.scope ?? "open") === "open";
  const [sev, setSev] = useState<string[]>(f.severity ?? []);
  const [all, setAll] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  const meta = useTicketMeta();
  const q = useQuery({
    queryKey: ["tickets", plant?.id, openScope ? "open" : "all"],
    queryFn: () => unwrap(core.GET("/api/v1/tickets", { params: { query: { plant_id: plant!.id, open_only: openScope, limit: 1000 } } })),
    enabled: Boolean(plant),
    refetchInterval: 10_000,
  });
  const base = (q.data ?? []).filter((t) => matches(t, { ...f, severity: undefined }));
  const rows = base.filter((t) => !sev.length || sev.includes(t.severity));
  const shown = all ? rows : rows.slice(0, 10);
  const toggle = (s: string) => setSev(sev.includes(s) ? sev.filter((x) => x !== s) : [...sev, s]);
  return (
    <Frame icon={IconLifebuoy} title={card.title || "Support tickets"} sub={`${rows.length} ${describe({ ...f, severity: sev })}`}
      right={<Actions>
        <CsvButton name={card.title || "tickets"} disabled={!rows.length} rows={() => [
          ["Ticket", "Severity", "Title", "Category", "Tier", "Status", "Assigned to", "Line", "Machine", "Order", "Reported at",
            "Reported by", "SLA", "Response due", "Response met", "Response breached", "Resolution due", "Resolved at",
            "Resolution breached", "ServiceNow", "Jira"],
          ...rows.map((t) => [t.number, t.severity, t.title, t.category, t.tier, t.status, t.assignee_name, t.line_code,
            t.equipment_code, t.erp_order_number, iso(t.reported_at), t.reported_by_name, slaState(t), iso(t.response.due),
            iso(t.response.met_at), t.response.breached, iso(t.resolution.due), iso(t.resolved_at), t.resolution.breached,
            t.sn_number, t.jira_key]),
        ]} />
        <OpenLink to="/tickets" label="Support tickets" />
      </Actions>}>
      <Group gap={6} mb={8}>
        {SEVERITIES.map((s) => {
          const n = base.filter((t) => t.severity === s).length;
          return (
            <UnstyledButton key={s} className={`chip small${sev.includes(s) ? " on" : ""}`} onClick={() => toggle(s)} aria-pressed={sev.includes(s)}>
              {s} · {n}
            </UnstyledButton>
          );
        })}
      </Group>
      {q.isPending ? <Loader size="sm" /> : rows.length === 0 ? <Text size="sm" c="dimmed">No tickets match.</Text> : (
        <Table highlightOnHover verticalSpacing={6} className="chat-table">
          <Table.Thead><Table.Tr><Table.Th>Ticket</Table.Th><Table.Th>Sev</Table.Th><Table.Th>Problem</Table.Th><Table.Th>Tier</Table.Th>
            <Table.Th>Status</Table.Th><Table.Th>Assigned to</Table.Th><Table.Th>SLA</Table.Th></Table.Tr></Table.Thead>
          <Table.Tbody>
            {shown.map((t) => (
              <Table.Tr key={t.id} style={{ cursor: "pointer" }} onClick={() => setOpenId(t.id)}
                className={slaState(t) === "breached" ? "row-late" : undefined}>
                <Table.Td className="mono">{t.number}</Table.Td>
                <Table.Td miw={42}><SeverityBadge s={t.severity} size="xs" /></Table.Td>
                <Table.Td maw={320}>
                  <Text size="sm" truncate>{t.title}</Text>
                  <Text size="xs" c="dimmed" truncate>{[t.line_code && `line ${t.line_code}`, t.equipment_code, t.category.toLowerCase().replace("_", " ")].filter(Boolean).join(" · ")}</Text>
                </Table.Td>
                <Table.Td><Badge size="xs" variant="outline" color={t.tier === "L3" ? "blue" : t.tier === "L2" ? "teal" : "navy.6"}>{t.tier}</Badge></Table.Td>
                <Table.Td><Text size="xs">{t.status.toLowerCase().replace("_", " ")}</Text></Table.Td>
                <Table.Td><Text size="xs" c={t.assignee_name ? undefined : "dimmed"} truncate maw={120}>{t.assignee_name ?? "unassigned"}</Text></Table.Td>
                <Table.Td miw={150}>{OPEN.includes(t.status) ? <Clock c={t.resolution} label="fix" /> : <Text size="xs" c="dimmed">{fmtDateTime(t.resolved_at)}</Text>}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
      {rows.length > 10 && <Button variant="subtle" size="compact-xs" mt={6} onClick={() => setAll(!all)}>{all ? "Show fewer" : `Show all ${rows.length}`}</Button>}
      <TicketDrawer id={openId} meta={meta.data} onClose={() => setOpenId(null)} />
    </Frame>
  );
}

function TicketCard({ card }: { card: ChatCard }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const meta = useTicketMeta();
  const q = useQuery({
    queryKey: ["ticket", card.ticket_id],
    queryFn: () => unwrap(core.GET("/api/v1/tickets/{ticket_id}", { params: { path: { ticket_id: card.ticket_id! } } })),
    refetchInterval: 10_000,
  });
  const ack = useMutation({
    mutationFn: () => unwrap(core.POST("/api/v1/tickets/{ticket_id}/status", { params: { path: { ticket_id: card.ticket_id! } }, body: { status: "ACKNOWLEDGED" } })),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["ticket", card.ticket_id] }); qc.invalidateQueries({ queryKey: ["tickets"] }); },
  });
  const t = q.data;
  return (
    <Frame icon={IconLifebuoy} title={card.title || card.number || "Ticket"} sub={t ? `${t.status.toLowerCase().replace("_", " ")} · reported ${fromNow(t.reported_at)} by ${t.reported_by_name ?? "—"}` : undefined}
      right={<Group gap={6}>
        {t?.status === "NEW" && <Button size="compact-xs" variant="light" loading={ack.isPending} onClick={() => ack.mutate()}>Acknowledge</Button>}
        <Button size="compact-xs" onClick={() => setOpen(true)}>Open ticket</Button>
      </Group>}>
      {!t ? <Loader size="sm" /> : (
        <Stack gap={8}>
          <Group gap={8} wrap="nowrap" align="flex-start">
            <SeverityBadge s={t.severity} />
            <Badge size="sm" variant="outline" color={t.tier === "L3" ? "blue" : t.tier === "L2" ? "teal" : "navy.6"}>{t.tier}</Badge>
            <Text size="sm" fw={600}>{t.title}</Text>
          </Group>
          {t.description && <Text size="sm" c="dimmed" lineClamp={3}>{t.description}</Text>}
          <SimpleGrid cols={{ base: 1, sm: 4 }} spacing="xs">
            <div><div className="label">Assigned to</div><Text size="xs" c={t.assignee_name ? undefined : "dimmed"}>{t.assignee_name ?? "unassigned"}</Text></div>
            <div><div className="label">Response</div><Clock c={t.response} label="respond" /></div>
            <div><div className="label">Resolution</div><Clock c={t.resolution} label="fix" /></div>
            <div><div className="label">Where</div><Text size="xs">{[t.line_code && `line ${t.line_code}`, t.equipment_code, t.terminal_code, t.erp_order_number].filter(Boolean).join(" · ") || "—"}</Text></div>
          </SimpleGrid>
          {t.activity.length > 0 && (
            <div>
              <div className="label" style={{ marginBottom: 4 }}>Latest activity</div>
              {t.activity.slice(-3).reverse().map((a) => (
                <Text key={a.id} size="xs" lineClamp={2}><Text span c="dimmed" size="xs">{fromNow(a.at)} · {a.origin} · {a.actor_name}: </Text>{a.text}</Text>
              ))}
            </div>
          )}
        </Stack>
      )}
      <TicketDrawer id={open ? card.ticket_id! : null} meta={meta.data} onClose={() => setOpen(false)} />
    </Frame>
  );
}

// --------------------------------------------------------------------------------------------- AI

function ApprovalsCard({ card }: { card: ChatCard }) {
  const q = useQuery({ queryKey: ["agent", "proposals", "PENDING"], queryFn: () => agent<AgentProposal[]>("/proposals?status=PENDING"), refetchInterval: 5000 });
  const rows = q.data ?? [];
  return (
    <Frame icon={IconChecklist} title={card.title || "Waiting for approval"} sub={`${rows.length} AI proposal${rows.length === 1 ? "" : "s"}`}
      right={<Actions>
        <CsvButton name="ai-approvals" disabled={!rows.length} rows={() => [
          ["Ticket", "Action", "Summary", "Rationale", "Proposed at"],
          ...rows.map((p) => [p.ticket_number, p.action, p.summary, p.rationale, iso(p.created_at)]),
        ]} />
        <OpenLink to="/ai-support?tab=approvals" label="Agent console" />
      </Actions>}>
      {q.isPending ? <Loader size="sm" /> : rows.length === 0 ? <Text size="sm" c="dimmed">Nothing is waiting for approval.</Text>
        : <Stack gap={6}>{rows.map((p) => <ProposalCard key={p.id} p={p} showTicket />)}</Stack>}
    </Frame>
  );
}

function AiRunCard({ card }: { card: ChatCard }) {
  return (
    <Frame icon={IconRobot} title={card.title || `AI run on ${card.number}`} sub="Every step the AI agent took, with the tools it used">
      {card.run_id ? <RunSteps runId={card.run_id} /> : <Text size="sm" c="dimmed">No run.</Text>}
    </Frame>
  );
}

// --------------------------------------------------------------------------------------------- SLA

type Analytics = {
  totals: { raised: number; resolved: number; open_now: number; response_met_pct: number | null; resolution_met_pct: number | null };
  by_severity: { severity: string; raised: number; response_met_pct: number | null; resolution_met_pct: number | null;
    mtta_min: number | null; mttr_h: number | null }[];
  resolved_by: { AI: number; PEOPLE: number; SERVICENOW_JIRA: number };
};
const pct = (v: number | null) => (v == null ? "—" : `${num(v, 1)}%`);

function SlaCard({ card }: { card: ChatCard }) {
  const { plant } = usePlant();
  const days = card.days ?? 7;
  const q = useQuery({
    queryKey: ["analytics", "tickets", plant?.id, days],
    queryFn: () => coreRaw<Analytics>(`/tickets/analytics?plant_id=${plant!.id}&days=${days}`),
    enabled: Boolean(plant),
  });
  const a = q.data;
  const tiles: [string, string | number][] = a ? [["Raised", a.totals.raised], ["Resolved", a.totals.resolved], ["Open now", a.totals.open_now],
    ["Response SLA met", pct(a.totals.response_met_pct)], ["Resolution SLA met", pct(a.totals.resolution_met_pct)],
    ["Resolved by AI", a.resolved_by.AI]] : [];
  return (
    <Frame icon={IconChartBar} title={card.title || `SLA performance · last ${days} days`} right={<Actions>
      <CsvButton name={`sla-performance-last-${days}-days`} disabled={!a} rows={() => !a ? [] : [
        ["Section", "Severity", "Raised", "Resolved", "Open now", "Response met %", "Resolution met %", "Time to respond (min)",
          "Time to resolve (h)", "Resolved by AI", "Resolved by people", "Resolved in ServiceNow/Jira"],
        ["Total", "all", a.totals.raised, a.totals.resolved, a.totals.open_now, a.totals.response_met_pct, a.totals.resolution_met_pct,
          "", "", a.resolved_by.AI, a.resolved_by.PEOPLE, a.resolved_by.SERVICENOW_JIRA],
        ...a.by_severity.map((x) => ["By severity", x.severity, x.raised, "", "", x.response_met_pct, x.resolution_met_pct,
          x.mtta_min, x.mttr_h, "", "", ""]),
      ]} />
      <OpenLink to="/support-analytics" label="Support analytics" />
    </Actions>}>
      {!a ? <Loader size="sm" /> : (
        <Stack gap="sm">
          <SimpleGrid cols={{ base: 2, sm: 3, lg: 6 }} spacing="xs">
            {tiles.map(([l, v]) => (
              <div key={l} className="chat-tile"><div className="label">{l}</div><div className="chat-tile-value">{v}</div></div>
            ))}
          </SimpleGrid>
          <Table verticalSpacing={5} className="chat-table">
            <Table.Thead><Table.Tr><Table.Th>Severity</Table.Th><Table.Th ta="right">Raised</Table.Th><Table.Th ta="right">Response met</Table.Th>
              <Table.Th ta="right">Resolution met</Table.Th><Table.Th ta="right">Time to respond</Table.Th><Table.Th ta="right">Time to resolve</Table.Th></Table.Tr></Table.Thead>
            <Table.Tbody>
              {a.by_severity.map((s) => (
                <Table.Tr key={s.severity}>
                  <Table.Td miw={42}><SeverityBadge s={s.severity} size="xs" /></Table.Td>
                  <Table.Td ta="right" className="mono">{s.raised}</Table.Td>
                  <Table.Td ta="right" className="mono">{pct(s.response_met_pct)}</Table.Td>
                  <Table.Td ta="right" className="mono">{pct(s.resolution_met_pct)}</Table.Td>
                  <Table.Td ta="right" className="mono">{s.mtta_min == null ? "—" : `${num(s.mtta_min, 1)} min`}</Table.Td>
                  <Table.Td ta="right" className="mono">{s.mttr_h == null ? "—" : `${num(s.mttr_h, 1)} h`}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Stack>
      )}
    </Frame>
  );
}

// --------------------------------------------------------------------------------------------- forecast

// the admin chart palette on white (see SupportAnalyticsPage): actual = blue, forecast = orange
const C_ACTUAL = "#2a78d6";
const C_FORECAST = "#eb6834";

type ForecastWeek = { week: number; start: string; end: string; days: number; expected: number; low: number; high: number };
type Forecast = {
  horizon_days: number; history_weeks: number; history_tickets: number; method: string; enough_history: boolean;
  granularity: "day" | "week";
  history: { day: string; count: number }[];
  weekly_history: { week_start: string; count: number }[];
  forecast: { day: string; weekday: string; expected: number; low: number; high: number }[];
  totals: { expected: number; low: number; high: number; per_week: ForecastWeek[]; last_7_days: number;
    change_vs_last_week_pct: number | null };
  trend_per_week: number; peak_day: { day: string; weekday: string; expected: number } | null;
  by_severity: { key: string; share_pct: number; expected: number }[]; expected_p1_p2: number;
  by_category: { key: string; share_pct: number; expected: number }[];
  backlog: { open_now: number; expected_resolved: number; projected_open: number };
  backtest: { week_start: string; predicted: number; low: number; high: number; actual: number; error_pct: number | null; within_range: boolean } | null;
};

const shortDay = (d: string) => new Date(`${d}T00:00:00`).toLocaleDateString("en-GB", { weekday: "short", day: "2-digit", month: "short" });
const dayMonth = (d: string) => new Date(`${d}T00:00:00`).toLocaleDateString("en-GB", { day: "2-digit", month: "short" });

/** "next week", "next two months", "next 45 days": the period the engineer asked for. */
export function horizonLabel(days: number): string {
  const named: Record<number, string> = { 7: "week", 14: "two weeks", 21: "three weeks", 28: "four weeks" };
  if (named[days]) return `next ${named[days]}`;
  const months = days / 30.4;
  if (days >= 28 && Math.abs(months - Math.round(months)) < 0.08) {
    const m = Math.round(months);
    return m === 3 ? "next quarter" : `next ${["", "month", "two months"][m] ?? `${m} months`}`;
  }
  return `next ${days} days`;
}

function ForecastCard({ card }: { card: ChatCard }) {
  const { plant } = usePlant();
  const horizon = card.horizon_days ?? 7; // the period the engineer asked for; the card only changes how it is shown
  const [view, setView] = useState<"day" | "week" | null>(null);
  const q = useQuery({
    queryKey: ["tickets", "forecast", plant?.id, horizon],
    queryFn: () => coreRaw<Forecast>(`/tickets/forecast?plant_id=${plant!.id}&horizon_days=${horizon}`),
    enabled: Boolean(plant),
  });
  const f = q.data;
  const by = view ?? f?.granularity ?? (horizon > 21 ? "week" : "day");
  type Bar = { label: string; Actual?: number; Forecast?: number; range?: string; note?: string };
  const data: Bar[] = !f ? [] : by === "day" ? [
    ...f.history.slice(-14).map((h) => ({ label: shortDay(h.day), Actual: h.count })),
    ...f.forecast.map((p) => ({ label: shortDay(p.day), Forecast: p.expected, range: `${p.low}–${p.high}` })),
  ] : [
    ...f.weekly_history.map((w) => ({ label: `wk ${dayMonth(w.week_start)}`, Actual: w.count })),
    ...f.totals.per_week.map((w) => ({ label: `wk ${dayMonth(w.start)}`, Forecast: w.expected, range: `${w.low}–${w.high}`,
      note: w.days < 7 ? `${w.days} days only` : undefined })),
  ];
  const fullWeeks = f?.totals.per_week.filter((w) => w.days === 7) ?? [];
  const busiestWeek = fullWeeks.length ? fullWeeks.reduce((x, y) => (y.expected > x.expected ? y : x)) : null;
  const change = f?.totals.change_vs_last_week_pct;
  const first = f?.forecast[0]?.day;
  const last = f?.forecast[f.forecast.length - 1]?.day;
  return (
    <Frame icon={IconTrendingUp} title={card.title || `Ticket forecast · ${horizonLabel(horizon)}`}
      sub={f && first && last ? `${dayMonth(first)} – ${dayMonth(last)} · from ${f.history_tickets} tickets over ${f.history_weeks} weeks · trend ${f.trend_per_week >= 0 ? "+" : ""}${num(f.trend_per_week, 1)} per week` : undefined}
      right={<Actions>
        <SegmentedControl size="xs" value={by} onChange={(v) => setView(v as "day" | "week")}
          data={[{ value: "day", label: "By day" }, { value: "week", label: "By week" }]} />
        <CsvButton name={`ticket-forecast-${horizonLabel(horizon)}`} disabled={!f} rows={() => !f ? [] : [
          ["Section", "Period", "From", "To", "Days", "Actual", "Expected", "Low (80%)", "High (80%)"],
          ["Total", horizonLabel(horizon), first, last, horizon, "", f.totals.expected, f.totals.low, f.totals.high],
          ...f.totals.per_week.map((w) => ["Forecast week", `week ${w.week}`, w.start, w.end, w.days, "", w.expected, w.low, w.high]),
          ...f.forecast.map((p) => ["Forecast day", p.weekday, p.day, p.day, 1, "", p.expected, p.low, p.high]),
          ...f.weekly_history.map((w) => ["History week", "", w.week_start, "", 7, w.count, "", "", ""]),
          ...f.history.map((h) => ["History day", "", h.day, h.day, 1, h.count, "", "", ""]),
          ...f.by_severity.map((x) => ["Severity mix", x.key, "", "", "", "", x.expected, "", ""]),
          ...f.by_category.map((x) => ["Category mix", x.key, "", "", "", "", x.expected, "", ""]),
          ["Backlog", "open now / expected resolved / projected open", "", "", "", f.backlog.open_now, f.backlog.expected_resolved,
            f.backlog.projected_open, ""],
          ...(f.backtest ? [["Backtest", "last week", f.backtest.week_start, "", 7, f.backtest.actual, f.backtest.predicted,
            f.backtest.low, f.backtest.high]] : []),
        ]} />
      </Actions>}>
      {!f ? <Loader size="sm" /> : (
        <Stack gap="sm">
          {!f.enough_history && <Text size="xs" c="amber.8">Little history yet: treat this forecast as a rough guide.</Text>}
          {horizon > 28 && <Text size="xs" c="dimmed">Beyond a few weeks this carries {f.history_weeks} weeks of history forward (trend damped): read the range, not the exact number.</Text>}
          <SimpleGrid cols={{ base: 2, sm: 4 }} spacing="xs">
            <div className="chat-tile"><div className="label">Expected tickets</div><div className="chat-tile-value">{num(f.totals.expected, 0)}</div>
              <Text size="xs" c="dimmed">range {f.totals.low}–{f.totals.high} (80%)</Text></div>
            <div className="chat-tile"><div className="label">P1 + P2 expected</div><div className="chat-tile-value">{num(f.expected_p1_p2, 1)}</div>
              <Text size="xs" c="dimmed">{f.by_severity.map((x) => `${x.key} ${num(x.expected, 1)}`).join(" · ")}</Text></div>
            {horizon <= 21 || !busiestWeek ? (
              <div className="chat-tile"><div className="label">Busiest day</div><div className="chat-tile-value">{f.peak_day ? shortDay(f.peak_day.day) : "—"}</div>
                <Text size="xs" c="dimmed">{f.peak_day ? `≈ ${num(f.peak_day.expected, 1)} tickets` : ""}</Text></div>
            ) : (
              <div className="chat-tile"><div className="label">Busiest week</div><div className="chat-tile-value">wk {dayMonth(busiestWeek.start)}</div>
                <Text size="xs" c="dimmed">≈ {num(busiestWeek.expected, 0)} tickets ({busiestWeek.low}–{busiestWeek.high})</Text></div>
            )}
            <div className="chat-tile"><div className="label">Backlog at the end</div><div className="chat-tile-value">{f.backlog.projected_open}</div>
              <Text size="xs" c="dimmed">open now {f.backlog.open_now} · ≈ {num(f.backlog.expected_resolved, 0)} resolved</Text></div>
          </SimpleGrid>
          <BarChart h={210} data={data} dataKey="label" withLegend legendProps={{ verticalAlign: "top", height: 28 }}
            series={[{ name: "Actual", color: C_ACTUAL }, { name: "Forecast", color: C_FORECAST }]} type="stacked"
            gridAxis="y" tickLine="none" barProps={{ radius: [4, 4, 0, 0] }} maxBarWidth={by === "week" ? 34 : 22}
            xAxisProps={{ interval: "preserveStartEnd" }}
            tooltipProps={{ content: ({ label, payload }) => {
              const row = payload?.[0]?.payload as Bar | undefined;
              if (!row) return null;
              return (
                <div className="chart-tip">
                  <Text size="xs" fw={600}>{label}{row.note ? ` (${row.note})` : ""}</Text>
                  {row.Actual != null && <Text size="xs">Actual: {row.Actual}</Text>}
                  {row.Forecast != null && <Text size="xs">Forecast: {num(row.Forecast, 1)} (range {row.range})</Text>}
                </div>
              );
            } }} />
          <Group gap="lg" wrap="wrap">
            {f.totals.per_week.length <= 3 && f.totals.per_week.map((w) => (
              <Text key={w.week} size="xs"><Text span c="dimmed" size="xs">Week {w.week}: </Text>≈ {num(w.expected, 0)} tickets</Text>
            ))}
            <Text size="xs"><Text span c="dimmed" size="xs">Last 7 days: </Text>{f.totals.last_7_days}
              {change != null && <Text span size="xs" c="dimmed"> ({change >= 0 ? "+" : ""}{num(change, 0)}% expected per week)</Text>}</Text>
            <Text size="xs"><Text span c="dimmed" size="xs">Top categories: </Text>
              {f.by_category.slice(0, 3).map((c) => `${c.key.toLowerCase().replace("_", " ")} ${num(c.share_pct, 0)}%`).join(" · ")}</Text>
          </Group>
          {f.backtest && (
            <Text size="xs" c="dimmed">
              Check on last week: forecast {num(f.backtest.predicted, 0)} (range {f.backtest.low}–{f.backtest.high}), actual {f.backtest.actual}
              {f.backtest.error_pct != null ? ` · off by ${num(f.backtest.error_pct, 0)}%` : ""}{f.backtest.within_range ? " · inside the range" : " · outside the range"}.
            </Text>
          )}
        </Stack>
      )}
    </Frame>
  );
}

// --------------------------------------------------------------------------------------------- SLA report

type SlaRow = { ticket_id: string; number: string; title: string; severity: string; tier: string; status: string; category: string;
  assignee: string | null; sn_number: string | null; reported_at: string };
type SlaReport = {
  since: string; until: string;
  summary: { breaches: number; response_breaches: number; resolution_breaches: number; breached_still_open: number; exits: number;
    resolved: number; cancelled: number; resolution_met_pct: number | null; response_met_pct: number | null; at_risk_now: number };
  by_severity: { severity: string; exits: number; resolution_met: number; resolution_missed: number; resolution_met_pct: number | null;
    breaches: number; avg_late_min: number | null }[];
  by_assignee: { assignee: string; breaches: number; still_open: number }[];
  breaches: (SlaRow & { which: string; due: string; met_at: string | null; still_open: boolean; late_min: number })[];
  exits: (SlaRow & { exit: string; exit_at: string; response_met: boolean | null; resolution_met: boolean | null;
    time_to_resolve_h: number; target_h: number | null })[];
  at_risk: (SlaRow & { which: string; due: string; minutes_left: number })[];
};

const late = (m: number) => (m < 90 ? `${m} min` : m < 60 * 48 ? `${num(m / 60, 1)} h` : `${num(m / 1440, 1)} d`);

function slaReportRows(r: SlaReport): Cell[][] {
  const s = r.summary;
  return [
    ["Section", "Ticket", "Severity", "Tier", "Status", "Assignee", "ServiceNow", "Title", "Target", "Due / exit", "Late (min) / outcome", "Still open / time to resolve (h)"],
    ["Period", "", "", "", "", "", "", `${iso(r.since)} to ${iso(r.until)}`, "", "", "", ""],
    ["Summary", "", "", "", "", "", "", `breaches ${s.breaches} (response ${s.response_breaches}, resolution ${s.resolution_breaches}); `
      + `still open ${s.breached_still_open}; exits ${s.exits} (resolved ${s.resolved}, cancelled ${s.cancelled}); resolution met `
      + `${s.resolution_met_pct ?? "-"}%; response met ${s.response_met_pct ?? "-"}%; at risk now ${s.at_risk_now}`, "", "", "", ""],
    ...r.by_severity.map((x) => ["By severity", "", x.severity, "", "", "", "", `exits ${x.exits}, met ${x.resolution_met}, missed `
      + `${x.resolution_missed}, met ${x.resolution_met_pct ?? "-"}%, breaches ${x.breaches}`, "", "", x.avg_late_min, ""]),
    ...r.by_assignee.map((x) => ["By assignee", "", "", "", "", x.assignee, "", `breaches ${x.breaches}`, "", "", "", x.still_open]),
    ...r.breaches.map((b) => ["Breach", b.number, b.severity, b.tier, b.status, b.assignee, b.sn_number, b.title, b.which, iso(b.due), b.late_min, b.still_open]),
    ...r.exits.map((e) => ["Exit", e.number, e.severity, e.tier, e.status, e.assignee, e.sn_number, e.title, e.exit,
      iso(e.exit_at), e.resolution_met == null ? "n/a" : e.resolution_met ? "met" : "missed", e.time_to_resolve_h]),
    ...r.at_risk.map((a) => ["At risk", a.number, a.severity, a.tier, a.status, a.assignee, a.sn_number, a.title, a.which, iso(a.due), `${a.minutes_left} min left`, ""]),
  ];
}

function SlaReportCard({ card }: { card: ChatCard }) {
  const { plant } = usePlant();
  const days = card.days ?? 7;
  const [tab, setTab] = useState<"breaches" | "exits" | "at_risk">("breaches");
  const [openId, setOpenId] = useState<string | null>(null);
  const [all, setAll] = useState(false);
  const meta = useTicketMeta();
  const q = useQuery({
    queryKey: ["tickets", "sla-report", plant?.id, days],
    queryFn: () => coreRaw<SlaReport>(`/tickets/sla-report?plant_id=${plant!.id}&days=${days}`),
    enabled: Boolean(plant),
    refetchInterval: 60_000,
  });
  const r = q.data;
  const s = r?.summary;
  const rows = r ? (tab === "breaches" ? r.breaches : tab === "exits" ? r.exits : r.at_risk) : [];
  const shown = all ? rows : rows.slice(0, 10);
  return (
    <Frame icon={IconClockExclamation} title={card.title || `SLA breach & exit report · last ${days} days`}
      sub={r ? `${fmtDateTime(r.since)} – ${fmtDateTime(r.until)}` : undefined}
      right={<CsvButton name={`sla-breach-exit-report-last-${days}-days`} disabled={!r} rows={() => (r ? slaReportRows(r) : [])} />}>
      {!r || !s ? <Loader size="sm" /> : (
        <Stack gap="sm">
          <SimpleGrid cols={{ base: 2, sm: 3, lg: 6 }} spacing="xs">
            {([["Breaches", s.breaches, `${s.response_breaches} response · ${s.resolution_breaches} resolution`],
              ["Breached, still open", s.breached_still_open, "need action now"],
              ["Exits", s.exits, `${s.resolved} resolved · ${s.cancelled} cancelled`],
              ["Resolution met", pct(s.resolution_met_pct), "of exits with a target"],
              ["Response met", pct(s.response_met_pct), "of exits"],
              ["At risk now", s.at_risk_now, "75% of a target used"]] as [string, string | number, string][]).map(([l, v, sub]) => (
              <div key={l} className="chat-tile"><div className="label">{l}</div><div className="chat-tile-value">{v}</div><Text size="xs" c="dimmed">{sub}</Text></div>
            ))}
          </SimpleGrid>
          <Table verticalSpacing={4} className="chat-table">
            <Table.Thead><Table.Tr><Table.Th>Severity</Table.Th><Table.Th ta="right">Exits</Table.Th><Table.Th ta="right">Resolution met</Table.Th>
              <Table.Th ta="right">Missed</Table.Th><Table.Th ta="right">Breaches</Table.Th><Table.Th ta="right">Avg. late</Table.Th></Table.Tr></Table.Thead>
            <Table.Tbody>
              {r.by_severity.map((x) => (
                <Table.Tr key={x.severity}>
                  <Table.Td miw={42}><SeverityBadge s={x.severity} size="xs" /></Table.Td>
                  <Table.Td ta="right" className="mono">{x.exits}</Table.Td>
                  <Table.Td ta="right" className="mono">{pct(x.resolution_met_pct)}</Table.Td>
                  <Table.Td ta="right" className="mono">{x.resolution_missed}</Table.Td>
                  <Table.Td ta="right" className="mono">{x.breaches}</Table.Td>
                  <Table.Td ta="right" className="mono">{x.avg_late_min == null ? "—" : late(x.avg_late_min)}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
          {r.by_assignee.length > 0 && (
            <Text size="xs"><Text span size="xs" c="dimmed">Breaches by assignee: </Text>
              {r.by_assignee.map((a) => `${a.assignee} ${a.breaches}${a.still_open ? ` (${a.still_open} open)` : ""}`).join(" · ")}</Text>
          )}
          <SegmentedControl size="xs" value={tab} onChange={(v) => { setTab(v as typeof tab); setAll(false); }}
            data={[{ value: "breaches", label: `Breaches · ${r.breaches.length}` }, { value: "exits", label: `Exits · ${r.exits.length}` },
              { value: "at_risk", label: `At risk now · ${r.at_risk.length}` }]} />
          {rows.length === 0 ? <Text size="sm" c="dimmed">Nothing here.</Text> : (
            <Table highlightOnHover verticalSpacing={5} className="chat-table">
              <Table.Tbody>
                {tab === "breaches" && (shown as SlaReport["breaches"]).map((b) => (
                  <Table.Tr key={`${b.ticket_id}${b.which}`} style={{ cursor: "pointer" }} onClick={() => setOpenId(b.ticket_id)} className={b.still_open ? "row-late" : undefined}>
                    <Table.Td className="mono">{b.number}</Table.Td>
                    <Table.Td miw={42}><SeverityBadge s={b.severity} size="xs" /></Table.Td>
                    <Table.Td maw={260}><Text size="sm" truncate>{b.title}</Text><Text size="xs" c="dimmed">{b.assignee ?? "unassigned"} · {b.tier}</Text></Table.Td>
                    <Table.Td><Text size="xs">{b.which} missed</Text><Text size="xs" c="dimmed">due {fmtDateTime(b.due)}</Text></Table.Td>
                    <Table.Td ta="right"><Text size="xs" c="red.7" fw={600}>{late(b.late_min)} late</Text>
                      <Text size="xs" c="dimmed">{b.still_open ? "still open" : `met ${fmtDateTime(b.met_at)}`}</Text></Table.Td>
                  </Table.Tr>
                ))}
                {tab === "exits" && (shown as SlaReport["exits"]).map((e) => (
                  <Table.Tr key={e.ticket_id} style={{ cursor: "pointer" }} onClick={() => setOpenId(e.ticket_id)}>
                    <Table.Td className="mono">{e.number}</Table.Td>
                    <Table.Td miw={42}><SeverityBadge s={e.severity} size="xs" /></Table.Td>
                    <Table.Td maw={260}><Text size="sm" truncate>{e.title}</Text><Text size="xs" c="dimmed">{e.assignee ?? "unassigned"} · {e.exit.toLowerCase()} {fmtDateTime(e.exit_at)}</Text></Table.Td>
                    <Table.Td><Text size="xs">{num(e.time_to_resolve_h, 1)} h{e.target_h ? ` / target ${e.target_h} h` : ""}</Text></Table.Td>
                    <Table.Td ta="right">{e.resolution_met == null ? <Text size="xs" c="dimmed">no target</Text>
                      : <Badge size="xs" variant="light" color={e.resolution_met ? "green" : "red"}>{e.resolution_met ? "met" : "missed"}</Badge>}</Table.Td>
                  </Table.Tr>
                ))}
                {tab === "at_risk" && (shown as SlaReport["at_risk"]).map((a) => (
                  <Table.Tr key={`${a.ticket_id}${a.which}`} style={{ cursor: "pointer" }} onClick={() => setOpenId(a.ticket_id)}>
                    <Table.Td className="mono">{a.number}</Table.Td>
                    <Table.Td miw={42}><SeverityBadge s={a.severity} size="xs" /></Table.Td>
                    <Table.Td maw={260}><Text size="sm" truncate>{a.title}</Text><Text size="xs" c="dimmed">{a.assignee ?? "unassigned"}</Text></Table.Td>
                    <Table.Td><Text size="xs">{a.which}</Text></Table.Td>
                    <Table.Td ta="right"><Text size="xs" c="amber.8" fw={600}>{a.minutes_left} min left</Text></Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          )}
          {rows.length > 10 && <Button variant="subtle" size="compact-xs" onClick={() => setAll(!all)}>{all ? "Show fewer" : `Show all ${rows.length}`}</Button>}
        </Stack>
      )}
      <TicketDrawer id={openId} meta={meta.data} onClose={() => setOpenId(null)} />
    </Frame>
  );
}

function TeamCard({ card }: { card: ChatCard }) {
  const { plant } = usePlant();
  const q = useSupportTeam(plant?.id);
  return (
    <Frame icon={IconUsers} title={card.title || "Support team"} sub="Open tickets per engineer" right={<Actions>
      <CsvButton name="support-team" disabled={!q.data?.length} rows={() => [
        ["Engineer", "User name", "E-mail", "Tier", "Open tickets"],
        ...(q.data ?? []).map((m) => [m.display_name, m.username, m.email, m.tier ?? "any", m.open_tickets]),
      ]} />
      <OpenLink to="/support-team" label="Support team" />
    </Actions>}>
      {q.isPending ? <Loader size="sm" /> : (
        <Table verticalSpacing={4} className="chat-table">
          <Table.Tbody>
            {(q.data ?? []).map((m) => (
              <Table.Tr key={m.id}>
                <Table.Td><Text size="sm">{m.display_name}</Text><Text size="xs" c="dimmed">{m.email ?? m.username}</Text></Table.Td>
                <Table.Td>{m.tier ? <Badge size="xs" variant="outline" color={m.tier === "L3" ? "blue" : m.tier === "L2" ? "teal" : "navy.6"}>{m.tier}</Badge> : <Text size="xs" c="dimmed">any tier</Text>}</Table.Td>
                <Table.Td ta="right" className="mono">{m.open_tickets} open</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
    </Frame>
  );
}

// --------------------------------------------------------------------------------------------- alerts

function AlertsCard({ card }: { card: ChatCard }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["notify", "alerts", true], queryFn: () => notify<Alert[]>("/notifications?open_only=true"), refetchInterval: 5000 });
  const ack = useMutation({
    mutationFn: (id: string) => notify(`/notifications/${id}/ack`, { method: "POST", body: "{}" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["notify"] }),
  });
  const rows = q.data ?? [];
  return (
    <Frame icon={IconBell} title={card.title || "Open alerts"} sub={`${rows.length} alert${rows.length === 1 ? "" : "s"} to the on-call rota`}
      right={<Actions>
        <CsvButton name="open-alerts" disabled={!rows.length} rows={() => [
          ["Raised at", "Rule", "Severity", "Title", "Roles", "Level", "Status"],
          ...rows.map((a) => [iso(a.created_at), a.rule, a.severity, a.title, a.roles.join(" "), a.level, a.status]),
        ]} />
        <OpenLink to="/notifications" label="Alerts & on-call" />
      </Actions>}>
      {q.isPending ? <Loader size="sm" /> : rows.length === 0 ? <Text size="sm" c="dimmed">No open alerts.</Text> : (
        <Table verticalSpacing={6} className="chat-table">
          <Table.Tbody>
            {rows.map((a) => (
              <Table.Tr key={a.id}>
                <Table.Td miw={42}>{a.severity ? <SeverityBadge s={a.severity} size="xs" /> : <Badge size="xs" variant="light" color="gray">{a.rule}</Badge>}</Table.Td>
                <Table.Td><Text size="sm">{a.title}</Text><Text size="xs" c="dimmed">{a.roles.join(", ")} · level {a.level} · {fromNow(a.created_at)}</Text></Table.Td>
                <Table.Td ta="right">{a.status === "ACKED" ? <Text size="xs" c="green.7">acknowledged</Text>
                  : <Button size="compact-xs" variant="light" loading={ack.isPending && ack.variables === a.id} onClick={() => ack.mutate(a.id)}>Acknowledge</Button>}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
    </Frame>
  );
}

// --------------------------------------------------------------------------------------------- machines, interfaces

const MACHINE_COLOR: Record<string, string> = { AVAILABLE: "green", DOWN: "red", MAINTENANCE: "amber" };

function MachinesCard({ card }: { card: ChatCard }) {
  const { plant } = usePlant();
  const q = useQuery({
    queryKey: ["equipment-health", plant?.id],
    queryFn: () => unwrap(core.GET("/api/v1/maintenance/equipment", { params: { query: { plant_id: plant!.id } } })),
    enabled: Boolean(plant),
    refetchInterval: 10_000,
  });
  const rows = (q.data ?? []).filter((m) => !card.machine || m.code === card.machine);
  return (
    <Frame icon={IconTool} title={card.title || (card.machine ? `Machine ${card.machine}` : "Machines")} right={<Actions>
      <CsvButton name={card.machine ? `machine-${card.machine}` : "machines"} disabled={!rows.length} rows={() => [
        ["Machine", "Name", "Line", "Status", "Open orders", "Failures", "MTBF (h)", "MTTR (min)"],
        ...rows.map((m) => [m.code, m.name, m.line_code, m.status, m.open_orders, m.reliability.failures, m.reliability.mtbf_h,
          m.reliability.mttr_h != null ? Math.round(m.reliability.mttr_h * 60) : null]),
      ]} />
      <OpenLink to="/maintenance" label="Maintenance" />
    </Actions>}>
      {q.isPending ? <Loader size="sm" /> : (
        <Table verticalSpacing={5} className="chat-table">
          <Table.Thead><Table.Tr><Table.Th>Machine</Table.Th><Table.Th>Status</Table.Th><Table.Th ta="right">Open orders</Table.Th>
            <Table.Th ta="right">Failures</Table.Th><Table.Th ta="right">MTBF</Table.Th><Table.Th ta="right">MTTR</Table.Th></Table.Tr></Table.Thead>
          <Table.Tbody>
            {rows.map((m) => (
              <Table.Tr key={m.id}>
                <Table.Td><Text size="sm" className="mono">{m.code}</Text><Text size="xs" c="dimmed">{m.name} · {m.line_code}</Text></Table.Td>
                <Table.Td><Badge size="sm" variant="outline" color={MACHINE_COLOR[m.status] ?? "gray"}>{m.status.toLowerCase()}</Badge></Table.Td>
                <Table.Td ta="right" className="mono">{m.open_orders}</Table.Td>
                <Table.Td ta="right" className="mono">{m.reliability.failures}</Table.Td>
                <Table.Td ta="right" className="mono">{m.reliability.mtbf_h != null ? `${num(m.reliability.mtbf_h, 1)} h` : "—"}</Table.Td>
                <Table.Td ta="right" className="mono">{m.reliability.mttr_h != null ? `${num(m.reliability.mttr_h * 60)} min` : "—"}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
    </Frame>
  );
}

type Outbound = { id: string; message_type: string; erp_key: string; status: string; attempts: number; last_error: string | null; created_at: string };
type ItsmMsg = { id: string; ticket_number: string; event_type: string; status: string; attempts: number; last_error: string | null; created_at: string };
const WAITING = ["RETRY_WAIT", "FAILED", "DEAD"];

function IntegrationCard({ card }: { card: ChatCard }) {
  const system = card.system ?? "both";
  const sap = useQuery({
    queryKey: ["erp", "outbound", "failed-chat"],
    queryFn: () => erp<Outbound[]>(`/api/v1/outbound?limit=100&${WAITING.map((s) => `status=${s}`).join("&")}`),
    enabled: system !== "itsm", refetchInterval: 10_000,
  });
  const sn = useQuery({
    queryKey: ["itsm-msgs"], queryFn: () => itsm<ItsmMsg[]>("/api/v1/itsm/messages?limit=100"),
    enabled: system !== "sap", refetchInterval: 10_000,
  });
  const rows = [
    ...(system !== "itsm" ? (sap.data ?? []).map((m) => ({ sys: "SAP", what: `${m.message_type.toLowerCase()} ${m.erp_key}`, ...m })) : []),
    ...(system !== "sap" ? (sn.data ?? []).filter((m) => WAITING.includes(m.status)).map((m) => ({ sys: "ServiceNow/Jira", what: `${m.ticket_number} ${m.event_type}`, ...m })) : []),
  ];
  return (
    <Frame icon={IconPlugConnected} title={card.title || "Interfaces: waiting or failed"} sub={`${rows.length} message${rows.length === 1 ? "" : "s"}`}
      right={<Actions>
        <CsvButton name="interfaces-waiting-or-failed" disabled={!rows.length} rows={() => [
          ["System", "Message", "Status", "Attempts", "Last error", "Created at"],
          ...rows.map((m) => [m.sys, m.what, m.status, m.attempts, m.last_error, iso(m.created_at)]),
        ]} />
        <OpenLink to="/integration" label="Integrations" />
      </Actions>}>
      {rows.length === 0 ? <Text size="sm" c="dimmed">{sap.isPending || sn.isPending ? "Loading…" : "Nothing waiting or failed."}</Text> : (
        <Table verticalSpacing={5} className="chat-table">
          <Table.Tbody>
            {rows.slice(0, 15).map((m) => (
              <Table.Tr key={m.id}>
                <Table.Td><Badge size="xs" variant="outline" color="navy.6">{m.sys}</Badge></Table.Td>
                <Table.Td><Text size="sm" className="mono">{m.what}</Text><Text size="xs" c="red.7" lineClamp={2}>{m.last_error ?? ""}</Text></Table.Td>
                <Table.Td><Badge size="xs" variant="light" color={m.status === "RETRY_WAIT" ? "amber" : "red"}>{m.status.toLowerCase().replace("_", " ")}</Badge></Table.Td>
                <Table.Td ta="right"><Text size="xs" c="dimmed">{m.attempts} tries · {fromNow(m.created_at)}</Text></Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
    </Frame>
  );
}

// --------------------------------------------------------------------------------------------- confirmation

const CONFIRM_TONE: Record<string, string> = { EXECUTED: "green", FAILED: "red", CANCELLED: "gray", RUNNING: "brand", PENDING: "amber" };
const ARG_LABEL: Record<string, string> = {
  title: "Title", description: "Description", category: "Category", impact: "Impact", machine: "Machine", order: "Order",
  note: "Note", text: "Note", summary: "Handover", severity: "Severity", reason: "Reason", resolution: "Resolution",
  duplicate_of: "Duplicate of", message_id: "Message", terminal: "Terminal", assignee: "Assign to",
};

export function ConfirmCard({ card, chatId, onDecided }: { card: ChatCard; chatId: string; onDecided: (c: ChatCard) => void }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const meta = useTicketMeta();
  const decide = useMutation({
    mutationFn: (confirm: boolean) => agent<ChatCard>(`/engineer/chats/${chatId}/actions/${card.action_id}`, { method: "POST", body: JSON.stringify({ confirm }) }),
    onSuccess: (c) => {
      onDecided(c);
      qc.invalidateQueries({ queryKey: ["tickets"] });
      qc.invalidateQueries({ queryKey: ["ticket", card.ticket_id] });
      qc.invalidateQueries({ queryKey: ["agent"] });
    },
    meta: { silent: true },
  });
  const status = card.status ?? "PENDING";
  const args = Object.entries(card.args ?? {}).filter(([k, v]) => ARG_LABEL[k] && !(k === "reason" && v === card.reason));
  return (
    <div className={`panel chat-card confirm st-${status}`}>
      <Group justify="space-between" wrap="nowrap" className="chat-card-head">
        <Group gap={10} wrap="nowrap">
          <span className="chat-card-icon"><IconCheck size={15} stroke={1.8} /></span>
          <div>
            <div className="chat-card-title">{card.label} · {card.ticket_id
              ? <Anchor component="button" size="sm" className="mono" onClick={() => setOpen(true)}>{card.number}</Anchor>
              : <Text span size="sm" c="dimmed">{card.number}</Text>}</div>
            <Text size="xs" c="dimmed">Prepared by the assistant · runs with your rights when you confirm</Text>
          </div>
        </Group>
        <Badge size="sm" variant="light" color={CONFIRM_TONE[status]}>{status === "PENDING" ? "waiting for you" : status.toLowerCase()}</Badge>
      </Group>
      <div className="chat-card-body">
        {args.length > 0 ? (
          <Stack gap={4}>
            {args.map(([k, v]) => (
              <Group key={k} gap={8} wrap="nowrap" align="flex-start">
                <Text size="xs" c="dimmed" w={96} style={{ flexShrink: 0 }}>{ARG_LABEL[k]}</Text>
                <Text size="sm" style={{ whiteSpace: "pre-wrap" }}>{String(v)}</Text>
              </Group>
            ))}
          </Stack>
        ) : <Text size="sm">{card.summary}</Text>}
        {card.tickets && card.tickets.length > 0 && (
          <Table verticalSpacing={3} mt={6} className="chat-table">
            <Table.Tbody>
              {card.tickets.map((t) => {
                const failed = card.result?.failed?.find((f) => f.number === t.number);
                return (
                  <Table.Tr key={t.ticket_id}>
                    <Table.Td className="mono">{t.number}</Table.Td>
                    <Table.Td miw={42}><SeverityBadge s={t.severity} size="xs" /></Table.Td>
                    <Table.Td maw={260}><Text size="xs" truncate>{t.title}</Text></Table.Td>
                    <Table.Td><Text size="xs" c="dimmed">{t.assignee ?? "unassigned"}</Text></Table.Td>
                    <Table.Td ta="right">{failed ? <Text size="xs" c="red.7">{failed.error}</Text>
                      : card.result?.done?.includes(t.number) ? <IconCheck size={13} color="var(--mes-green)" /> : null}</Table.Td>
                  </Table.Tr>
                );
              })}
            </Table.Tbody>
          </Table>
        )}
        {card.reason && <Text size="xs" c="dimmed" mt={6}>Why: {card.reason}</Text>}
        {status === "PENDING" && card.duplicates && card.duplicates.length > 0 && (
          <Text size="xs" c="amber.8" mt={6}>
            Open tickets that may be the same problem: {card.duplicates.map((d) => `${d.number} (${d.title})`).join(", ")}
          </Text>
        )}
        {status === "PENDING" ? (
          <Group gap={8} mt="sm">
            <Button size="xs" leftSection={<IconCheck size={14} />} loading={decide.isPending && decide.variables} onClick={() => decide.mutate(true)}>Confirm</Button>
            <Button size="xs" variant="default" leftSection={<IconX size={14} />} loading={decide.isPending && !decide.variables} onClick={() => decide.mutate(false)}>Cancel</Button>
          </Group>
        ) : (
          <Text size="xs" mt={6} c={status === "FAILED" ? "red.7" : status === "EXECUTED" ? "green.7" : "dimmed"}>
            {status === "EXECUTED" ? "Done" : status === "FAILED" ? `Failed: ${card.error}` : status === "CANCELLED" ? "Cancelled" : "Running…"}
            {card.decided_by ? ` · ${card.decided_by} ${fmtDateTime(card.decided_at)}` : ""}
          </Text>
        )}
        {decide.error && <Text size="xs" c="red.7" mt={4}>{decide.error.message}</Text>}
      </div>
      <TicketDrawer id={open && card.ticket_id ? card.ticket_id : null} meta={meta.data} onClose={() => setOpen(false)} />
    </div>
  );
}

// --------------------------------------------------------------------------------------------- dispatcher

export function ChatCardView({ card, chatId, onDecided }: { card: ChatCard; chatId: string; onDecided: (c: ChatCard) => void }) {
  switch (card.kind) {
    case "tickets": return <TicketsCard card={card} />;
    case "ticket": return <TicketCard card={card} />;
    case "approvals": return <ApprovalsCard card={card} />;
    case "sla": return <SlaCard card={card} />;
    case "sla_report": return <SlaReportCard card={card} />;
    case "forecast": return <ForecastCard card={card} />;
    case "team": return <TeamCard card={card} />;
    case "alerts": return <AlertsCard card={card} />;
    case "ai_run": return <AiRunCard card={card} />;
    case "machines": return <MachinesCard card={card} />;
    case "integration": return <IntegrationCard card={card} />;
    case "confirm": return <ConfirmCard card={card} chatId={chatId} onDecided={onDecided} />;
    default: return null;
  }
}
