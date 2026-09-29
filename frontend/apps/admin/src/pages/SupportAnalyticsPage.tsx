import { BarChart, LineChart } from "@mantine/charts";
import { ActionIcon, Group, SegmentedControl, SimpleGrid, Stack, Table, Text } from "@mantine/core";
import { agent, coreRaw } from "@mes/api-client";
import {
  IconChartBar, IconChecklist, IconCircleCheck, IconClockCheck, IconDownload, IconHourglass, IconMessages,
  IconMoodEmpty, IconRobot, IconThumbUp, IconTicket, type Icon,
} from "@tabler/icons-react";
import { useQuery } from "@tanstack/react-query";
import dayjs from "dayjs";
import { type ReactNode, useState } from "react";
import { useNavigate } from "react-router";
import { PageHeader } from "../components/Layout";
import { usePlant } from "../components/plant";
import { usd } from "../components/agent";
import { type Cell, csvName, downloadCsv } from "../lib/csv";

// Categorical slots (light steps of the reference palette), validated against the white panel surface: all pairs
// pass CVD (worst 9.2) and normal-vision (24.0) separation. Aqua is 2.8:1 on white, so every chart keeps its legend
// and tooltip (relief). Max three series.
const C1 = "#2a78d6";
const C2 = "#eb6834";
const C3 = "#1baf7a";
const GRID = "#e2e7ef"; // --mes-line: recessive grid
const AXIS = "#5d6b80"; // --mes-text-dim

type Kv = { key: string; count: number };
type Tickets = {
  totals: { raised: number; resolved: number; open_now: number; response_met_pct: number | null; resolution_met_pct: number | null;
    ai_touched_pct: number | null };
  by_day: { day: string; raised: number; resolved: number }[];
  by_severity: { severity: string; raised: number; response_met_pct: number | null; resolution_met_pct: number | null;
    mtta_min: number | null; mttr_h: number | null }[];
  by_category: Kv[]; by_tier: Kv[]; by_source: Kv[]; top_equipment: Kv[];
  resolved_by: { AI: number; PEOPLE: number; SERVICENOW_JIRA: number };
};
type Agent = {
  runs: { kind: string; tier: string | null; outcome: string | null; status: string; count: number; cost_usd: number; avg_seconds: number | null }[];
  proposals: { action: string; status: string; count: number }[];
  proposal_wait_min: number | null; chats: Record<string, number>; ratings: { helpful: number; wrong: number };
  cost_per_day: { day: string; kind: string; cost_usd: number; runs: number }[];
  tickets_worked: number; cost_per_ticket_usd: number | null;
};

const CATEGORY: Record<string, string> = { MES_APP: "MES / terminal", HARDWARE: "Hardware", INTEGRATION: "SAP / interface",
  DATA: "Data", ACCESS: "Access / PIN", CHANGE_REQUEST: "Change request", OTHER: "Other" };
const OUTCOME: Record<string, string> = { RESOLVED: "Resolved", RESOLVE_PROPOSED: "Resolve proposed", ACTION_PROPOSED: "Fix proposed",
  ESCALATED: "Escalated", DUPLICATE: "Duplicate", ROOT_CAUSE: "Root cause found", NOTED: "Diagnosed", NO_ACTION: "No action",
  HANDED_OVER: "Handed to people", FIXED_IN_CHAT: "Fixed in chat", TICKET_CREATED: "Ticket raised" };
const pct = (v: number | null | undefined) => (v == null ? "—" : `${v}%`);

/** Clickable KPI tile: icon + colour-accented top border + hover lift (same look as Work Orders/Quality/Tickets). */
function KpiTile({ icon: IconC, label, value, sub, color, hot, onClick }: {
  icon: Icon; label: string; value: ReactNode; sub?: string; color: string; hot?: boolean; onClick?: () => void;
}) {
  return (
    <div className="panel kpi wo-kpi" style={{ borderTop: `3px solid ${color}`, cursor: onClick ? "pointer" : "default" }}
      onClick={onClick} role={onClick ? "button" : undefined} tabIndex={onClick ? 0 : undefined}
      onKeyDown={onClick ? (e) => e.key === "Enter" && onClick() : undefined}>
      <div className="wo-kpi-top"><span className="wo-kpi-icon" style={{ background: color }}><IconC size={13} stroke={2.2} /></span></div>
      <div className="label">{label}</div>
      <div className="value" style={{ color: hot ? color : undefined }}>{value}</div>
      {sub && <Text size="xs" c="dimmed" mt={2}>{sub}</Text>}
    </div>
  );
}

function Panel({ title, sub, children, id }: { title: string; sub?: string; children: ReactNode; id?: string }) {
  return (
    <div className="panel" id={id} style={{ padding: 14, scrollMarginTop: 16 }}>
      <Text className="label">{title}</Text>
      {sub && <Text size="xs" c="dimmed" mb={6}>{sub}</Text>}
      {children}
    </div>
  );
}

/** Small centered icon placeholder, height-matched to its sibling chart so an empty panel doesn't sit as a
 * tall void beside a full one in the same row. */
function ChartEmpty({ text, h = 220 }: { text: string; h?: number }) {
  return (
    <div style={{ height: h, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center" }}>
      <div style={{
        width: 36, height: 36, borderRadius: 10, background: "var(--mes-panel-2)", display: "grid", placeItems: "center",
        marginBottom: 8, color: "var(--mes-text-dim)",
      }}>
        <IconMoodEmpty size={18} stroke={1.6} />
      </div>
      <Text size="sm" c="dimmed">{text}</Text>
    </div>
  );
}

const axis = { tick: { fill: AXIS, fontSize: 11 } };

function HBars({ data, label, color = C1, onBarClick }: {
  data: (Kv & { code?: string })[]; label: string; color?: string; onBarClick?: (code: string) => void;
}) {
  if (!data.length) return <ChartEmpty text="No data in this period." h={Math.max(120, 4 * 30)} />;
  return (
    <BarChart h={Math.max(120, data.length * 30)} data={data} dataKey="key" orientation="vertical" series={[{ name: "count", label, color }]}
      gridColor={GRID} textColor={AXIS} tickLine="none" gridAxis="x" withLegend={false} yAxisProps={{ width: 130, interval: 0, ...axis }}
      xAxisProps={{ allowDecimals: false, ...axis }}
      barProps={{
        radius: [0, 4, 4, 0],
        ...(onBarClick ? {
          cursor: "pointer",
          // recharts' onClick payload shape isn't precisely typed by @mantine/charts; read defensively.
          onClick: (entry: unknown) => {
            const payload = (entry as { payload?: { code?: string; key?: string } } | undefined)?.payload;
            const code = payload?.code ?? payload?.key;
            if (code) onBarClick(code);
          },
        } : {}),
      }} maxBarWidth={18} />
  );
}

const scrollTo = (id: string) => document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });

export function SupportAnalyticsPage() {
  const navigate = useNavigate();
  const { plant } = usePlant();
  const [days, setDays] = useState("30");
  const t = useQuery({
    queryKey: ["analytics", "tickets", plant?.id, days],
    queryFn: () => coreRaw<Tickets>(`/tickets/analytics?plant_id=${plant!.id}&days=${days}`),
    enabled: Boolean(plant),
  });
  const a = useQuery({ queryKey: ["analytics", "agent", days], queryFn: () => agent<Agent>(`/analytics?days=${days}`), retry: false });
  const d = t.data;
  const g = a.data;

  const ticketRuns = (g?.runs ?? []).filter((r) => r.kind === "TICKET" && r.outcome);
  const outcomes = Object.values(ticketRuns.reduce<Record<string, { key: string; L1: number; L2: number }>>((acc, r) => {
    const k = OUTCOME[r.outcome!] ?? r.outcome!;
    acc[k] ??= { key: k, L1: 0, L2: 0 };
    acc[k][r.tier === "L2" ? "L2" : "L1"] += r.count;
    return acc;
  }, {})).sort((x, y) => y.L1 + y.L2 - (x.L1 + x.L2));
  const approved = (g?.proposals ?? []).filter((p) => p.status === "EXECUTED").reduce((s, p) => s + p.count, 0);
  const rejected = (g?.proposals ?? []).filter((p) => p.status === "REJECTED").reduce((s, p) => s + p.count, 0);
  const pending = (g?.proposals ?? []).filter((p) => p.status === "PENDING").reduce((s, p) => s + p.count, 0);
  const costDays = Object.values((g?.cost_per_day ?? []).reduce<Record<string, { day: string; Tickets: number; Chats: number }>>((acc, r) => {
    acc[r.day] ??= { day: r.day, Tickets: 0, Chats: 0 };
    acc[r.day][r.kind === "CHAT" ? "Chats" : "Tickets"] += Math.round(r.cost_usd * 100) / 100;
    return acc;
  }, {}));
  const chats = g?.chats ?? {};
  const chatTotal = Object.values(chats).reduce((s, n) => s + n, 0);
  const resolvedTotal = d ? d.resolved_by.AI + d.resolved_by.PEOPLE + d.resolved_by.SERVICENOW_JIRA : 0;

  const exportCsv = () => {
    if (!d) return;
    const rows: Cell[][] = [
      ["Section", "Key", "Metric", "Value"],
      ["Totals", "", "raised", d.totals.raised], ["Totals", "", "resolved", d.totals.resolved],
      ["Totals", "", "open now", d.totals.open_now], ["Totals", "", "response SLA met %", d.totals.response_met_pct],
      ["Totals", "", "resolution SLA met %", d.totals.resolution_met_pct], ["Totals", "", "worked by AI %", d.totals.ai_touched_pct],
      ["Totals", "", "resolved by AI", d.resolved_by.AI], ["Totals", "", "resolved by people", d.resolved_by.PEOPLE],
      ["Totals", "", "resolved by ServiceNow/Jira", d.resolved_by.SERVICENOW_JIRA],
      ...d.by_day.map((x): Cell[] => ["By day", x.day, "raised", x.raised]),
      ...d.by_day.map((x): Cell[] => ["By day", x.day, "resolved", x.resolved]),
      ...d.by_severity.flatMap((x): Cell[][] => [
        ["By severity", x.severity, "raised", x.raised], ["By severity", x.severity, "response met %", x.response_met_pct],
        ["By severity", x.severity, "resolution met %", x.resolution_met_pct], ["By severity", x.severity, "time to respond (min)", x.mtta_min],
        ["By severity", x.severity, "time to resolve (h)", x.mttr_h],
      ]),
      ...d.by_category.map((x): Cell[] => ["By category", CATEGORY[x.key] ?? x.key, "tickets", x.count]),
      ...d.by_tier.map((x): Cell[] => ["By tier", x.key, "tickets", x.count]),
      ...d.by_source.map((x): Cell[] => ["By source", x.key, "tickets", x.count]),
      ...d.top_equipment.map((x): Cell[] => ["Recurring machines", x.key, "tickets", x.count]),
      ...(g ? [
        ["AI", "", "tickets worked", g.tickets_worked] as Cell[], ["AI", "", "Claude cost per ticket", g.cost_per_ticket_usd] as Cell[],
        ["AI", "", "proposals approved", approved] as Cell[], ["AI", "", "proposals rejected", rejected] as Cell[],
        ["AI", "", "proposals waiting", pending] as Cell[], ["AI", "", "approval wait (min)", g.proposal_wait_min] as Cell[],
        ["AI", "", "operator chats", chatTotal] as Cell[], ["AI", "", "fixed in chat", chats.FIXED ?? 0] as Cell[],
        ["AI", "", "raised a ticket in chat", chats.TICKET_CREATED ?? 0] as Cell[],
        ["AI", "", "ratings helpful", g.ratings.helpful] as Cell[], ["AI", "", "ratings wrong", g.ratings.wrong] as Cell[],
        ...outcomes.map((o): Cell[] => ["AI outcomes", o.key, "L1 + L2", o.L1 + o.L2]),
      ] : []),
    ];
    downloadCsv(csvName(`support-analytics-${days}d`), rows);
  };

  return (
    <>
      <PageHeader title="Support analytics" sub="How support is performing: SLA, speed, recurring problems, and what the AI support engineers contribute" />
      <Group mb="md" justify="space-between">
        <Group>
          <SegmentedControl size="xs" value={days} onChange={setDays} data={[{ value: "7", label: "7 days" }, { value: "30", label: "30 days" }, { value: "90", label: "90 days" }]} />
          {d && <Text size="xs" c="dimmed">{dayjs(d.by_day[0]?.day).format("DD MMM")} – today</Text>}
        </Group>
        <ActionIcon variant="light" size="input-xs" disabled={!d} onClick={exportCsv} aria-label="Download CSV"><IconDownload size={14} /></ActionIcon>
      </Group>
      {d && (
        <Stack>
          <SimpleGrid cols={{ base: 2, md: 6 }} className="rise">
            <KpiTile icon={IconTicket} label="Tickets raised" value={d.totals.raised} sub={`${d.totals.open_now} open now`}
              color="var(--mes-blue)" onClick={() => scrollTo("chart-by-day")} />
            <KpiTile icon={IconCircleCheck} label="Resolved" value={d.totals.resolved} color="var(--mes-green)" onClick={() => scrollTo("chart-by-day")} />
            <KpiTile icon={IconClockCheck} label="Response SLA met" value={pct(d.totals.response_met_pct)} sub="of tickets whose target came due"
              color="var(--mes-amber)" hot={(d.totals.response_met_pct ?? 100) < 85} onClick={() => scrollTo("chart-sla")} />
            <KpiTile icon={IconChecklist} label="Resolution SLA met" value={pct(d.totals.resolution_met_pct)}
              color="var(--mes-amber)" hot={(d.totals.resolution_met_pct ?? 100) < 85} onClick={() => scrollTo("chart-sla")} />
            <KpiTile icon={IconRobot} label="Resolved by AI" value={resolvedTotal ? `${Math.round((100 * d.resolved_by.AI) / resolvedTotal)}%` : "—"}
              sub={`${d.resolved_by.AI} AI (incl. approved proposals) · ${d.resolved_by.PEOPLE} people · ${d.resolved_by.SERVICENOW_JIRA} ServiceNow/Jira`}
              color="var(--mes-blue)" onClick={g ? () => scrollTo("ai-section") : undefined} />
            <KpiTile icon={IconChartBar} label="Worked by AI" value={pct(d.totals.ai_touched_pct)} sub="tickets with AI diagnosis or action"
              color="var(--mes-blue)" onClick={g ? () => scrollTo("ai-section") : undefined} />
          </SimpleGrid>
          <SimpleGrid cols={{ base: 1, lg: 2 }}>
            <Panel id="chart-by-day" title="Tickets raised and resolved per day">
              <LineChart h={220} data={d.by_day.map((x) => ({ ...x, day: dayjs(x.day).format("DD MMM") }))} dataKey="day" curveType="linear"
                series={[{ name: "raised", label: "Raised", color: C1 }, { name: "resolved", label: "Resolved", color: C2 }]}
                strokeWidth={2} dotProps={{ r: 4, strokeWidth: 2, stroke: "#ffffff" }} withLegend legendProps={{ verticalAlign: "top", height: 30 }}
                gridColor={GRID} textColor={AXIS} tickLine="none" gridAxis="y" yAxisProps={{ allowDecimals: false, ...axis }} xAxisProps={axis} />
            </Panel>
            <Panel id="chart-sla" title="SLA met by severity" sub="Share of tickets that met the target, of those whose target came due">
              <BarChart h={220} data={d.by_severity.map((x) => ({ key: x.severity, Response: x.response_met_pct ?? 0, Resolution: x.resolution_met_pct ?? 0 }))}
                dataKey="key" series={[{ name: "Response", color: C1 }, { name: "Resolution", color: C2 }]} withLegend legendProps={{ verticalAlign: "top", height: 30 }}
                valueFormatter={(v) => `${v}%`} yAxisProps={{ domain: [0, 100], ...axis }} xAxisProps={axis} gridColor={GRID} textColor={AXIS}
                tickLine="none" gridAxis="y" barProps={{ radius: [4, 4, 0, 0] }} maxBarWidth={26} barChartProps={{ barGap: 2 }} />
            </Panel>
          </SimpleGrid>
          <SimpleGrid cols={{ base: 1, lg: 3 }}>
            <Panel title="Speed by severity">
              <Table>
                <Table.Thead><Table.Tr><Table.Th>Severity</Table.Th><Table.Th ta="right">Raised</Table.Th><Table.Th ta="right">Time to respond</Table.Th><Table.Th ta="right">Time to resolve</Table.Th></Table.Tr></Table.Thead>
                <Table.Tbody>
                  {d.by_severity.map((x) => (
                    <Table.Tr key={x.severity}>
                      <Table.Td fw={600}>{x.severity}</Table.Td>
                      <Table.Td ta="right" className="mono">{x.raised}</Table.Td>
                      <Table.Td ta="right" className="mono">{x.mtta_min == null ? "—" : `${x.mtta_min} min`}</Table.Td>
                      <Table.Td ta="right" className="mono">{x.mttr_h == null ? "—" : `${x.mttr_h} h`}</Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
              <Text size="xs" c="dimmed" mt={6}>Averages; time on hold is excluded from time to resolve.</Text>
            </Panel>
            <Panel title="What breaks: by category">
              <HBars data={d.by_category.map((c) => ({ key: CATEGORY[c.key] ?? c.key, code: c.key, count: c.count }))} label="Tickets"
                onBarClick={(code) => navigate(`/tickets?category=${code}`)} />
            </Panel>
            <Panel title="Recurring machines" sub="Machines with the most tickets">
              <HBars data={d.top_equipment.map((e) => ({ ...e, code: e.key }))} label="Tickets"
                onBarClick={(code) => navigate(`/tickets?q=${encodeURIComponent(code)}`)} />
            </Panel>
          </SimpleGrid>

          <Text className="label" mt="md" id="ai-section" style={{ scrollMarginTop: 16 }}>AI support engineers</Text>
          {g ? (
            <>
              <SimpleGrid cols={{ base: 2, md: 6 }} className="rise">
                <KpiTile icon={IconRobot} label="Tickets worked" value={g.tickets_worked} sub={`${usd(g.cost_per_ticket_usd)} Claude cost per ticket`}
                  color="var(--mes-blue)" onClick={() => scrollTo("panel-ai-outcomes")} />
                <KpiTile icon={IconThumbUp} label="Proposals approved" value={approved + rejected ? `${Math.round((100 * approved) / (approved + rejected))}%` : "—"}
                  sub={`${approved} approved · ${rejected} rejected · ${pending} waiting`} color="var(--mes-green)"
                  hot={pending > 0} onClick={() => navigate("/ai-support?tab=approvals")} />
                <KpiTile icon={IconHourglass} label="Approval wait" value={g.proposal_wait_min == null ? "—" : `${g.proposal_wait_min} min`}
                  sub="proposal to decision" color="var(--mes-amber)" onClick={() => navigate("/ai-support?tab=approvals")} />
                <KpiTile icon={IconMessages} label="Operator chats" value={chatTotal} sub={`${chats.FIXED ?? 0} fixed in chat · ${chats.TICKET_CREATED ?? 0} raised a ticket`}
                  color="var(--mes-blue)" onClick={() => navigate("/ai-support?tab=chats")} />
                <KpiTile icon={IconCircleCheck} label="Fixed in chat" value={chatTotal ? `${Math.round((100 * (chats.FIXED ?? 0)) / chatTotal)}%` : "—"}
                  sub="no support ticket work needed" color="var(--mes-green)" onClick={() => navigate("/ai-support?tab=chats")} />
                <KpiTile icon={IconThumbUp} label="Supervisor ratings" value={`${g.ratings.helpful} / ${g.ratings.wrong}`} sub="helpful / wrong"
                  color="var(--mes-navy)" onClick={() => navigate("/ai-support")} />
              </SimpleGrid>
              <SimpleGrid cols={{ base: 1, lg: 2 }}>
                <Panel id="panel-ai-outcomes" title="What the AI did with tickets" sub="Latest outcome per run, first line (L1) and root cause (L2)">
                  {outcomes.length ? (
                    <BarChart h={Math.max(140, outcomes.length * 30)} data={outcomes} dataKey="key" orientation="vertical" type="stacked"
                      series={[{ name: "L1", label: "AI L1", color: C1 }, { name: "L2", label: "AI L2", color: C3 }]} withLegend
                      legendProps={{ verticalAlign: "top", height: 30 }} gridColor={GRID} textColor={AXIS} tickLine="none" gridAxis="x"
                      yAxisProps={{ width: 130, interval: 0, ...axis }} xAxisProps={{ allowDecimals: false, ...axis }} maxBarWidth={18}
                      barProps={{ stroke: "#ffffff", strokeWidth: 2 }} />
                  ) : <ChartEmpty text="No AI ticket runs in this period." />}
                </Panel>
                <Panel title="Claude cost per day" sub="Tickets (L1 + L2) and operator chats">
                  {costDays.length ? (
                    <BarChart h={220} data={costDays.map((x) => ({ ...x, day: dayjs(x.day).format("DD MMM") }))} dataKey="day" type="stacked"
                      series={[{ name: "Tickets", color: C1 }, { name: "Chats", color: C2 }]} withLegend legendProps={{ verticalAlign: "top", height: 30 }}
                      valueFormatter={(v) => `$${v.toFixed(2)}`} gridColor={GRID} textColor={AXIS} tickLine="none" gridAxis="y"
                      yAxisProps={axis} xAxisProps={axis} maxBarWidth={26} barProps={{ stroke: "#ffffff", strokeWidth: 2 }} />
                  ) : <ChartEmpty text="No cost in this period." />}
                </Panel>
              </SimpleGrid>
            </>
          ) : <Text size="sm" c="dimmed">The AI support agent's figures are not available.</Text>}
        </Stack>
      )}
    </>
  );
}
