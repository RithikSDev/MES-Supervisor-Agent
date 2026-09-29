import { Badge, Group, SimpleGrid, Text } from "@mantine/core";
import { type AgentProposal, type Ticket } from "@mes/api-client";
import { IconArrowRight } from "@tabler/icons-react";
import dayjs from "dayjs";
import { Link } from "react-router";
import { useMe } from "../auth";
import { usePlant } from "../components/plant";
import { useModuleCounts } from "../components/ModuleGrid";
import { fromNow } from "../lib/format";
import { GROUP_DESC, GROUP_ICON, GROUP_ROUTE, GROUPS, visibleModules } from "../modules";

type Attention = { key: string; tag: string; tone: "red" | "amber" | "blue" | "grey"; what: string; at: string; to: string };

const SEV_RANK: Record<string, number> = { P1: 0, P2: 1, P3: 2, P4: 3 };

function attentionItems(tickets: Ticket[], proposals: AgentProposal[]): Attention[] {
  const breached = (t: Ticket) => t.response.breached || t.resolution.breached;
  const items: Attention[] = [...tickets]
    .sort((a, b) => Number(breached(b)) - Number(breached(a)) || SEV_RANK[a.severity] - SEV_RANK[b.severity]
      || b.reported_at.localeCompare(a.reported_at))
    .map((t) => ({
      key: t.id,
      tag: breached(t) ? "SLA missed" : t.severity,
      tone: breached(t) || t.severity === "P1" ? "red" : t.severity === "P2" ? "amber" : "grey",
      what: `${t.number} · ${t.title}`,
      at: t.reported_at,
      to: `/tickets?ticket=${t.id}`,
    }));
  const approvals: Attention[] = proposals.map((p) => ({
    key: p.id, tag: "Approve", tone: "blue", what: `${p.ticket_number} · ${p.summary}`, at: p.created_at, to: "/ai-support?tab=approvals",
  }));
  return [...approvals.slice(0, 2), ...items].slice(0, 8);
}
const ATTENTION_SHOWN = 4;

/** Same "last word italic blue" split as module-card titles and PageHeader. */
function groupTitle(g: string) {
  const i = g.lastIndexOf(" ");
  return i > 0 ? <>{g.slice(0, i)} <em>{g.slice(i + 1)}</em></> : <em>{g}</em>;
}

export function HomePage() {
  const me = useMe();
  const { plant } = usePlant();
  const canTickets = me.permissions.includes("ticket:read");

  const { counts, openTickets, proposals, ticketsPending, alertsEscalated } = useModuleCounts();
  const modules = visibleModules(me.permissions);
  const attention = attentionItems(openTickets, proposals);
  const first = (me.display_name ?? me.username).split(" ")[0];
  const today = plant?.timezone
    ? new Date().toLocaleDateString("en-GB", { timeZone: plant.timezone, weekday: "long", day: "2-digit", month: "long", year: "numeric" })
    : dayjs().format("dddd, DD MMMM YYYY");

  return (
    <div style={{ paddingTop: 36 }}>
      <Group align="flex-start" justify="space-between" gap={48} wrap="wrap" mb={64}>
        <div style={{ flex: "1 1 520px", maxWidth: 820 }}>
          <div className="eyebrow">Control room · {today}</div>
          <h1 className="display" style={{ fontSize: "clamp(2.6rem, 5vw, 4.4rem)", marginTop: 26 }}>
            Good shift, <em>{first}.</em>
          </h1>
          <p className="lede" style={{ marginTop: 22 }}>
            Everything below is live — production, quality and traceability read straight off the shop floor.
            Support tickets get worked by AI engineers under your approval, and every change leaves a trail.
          </p>
        </div>
        {canTickets && (
          <div className="panel activity" style={{ flex: "0 1 560px", minWidth: 320 }}>
            <div className="head">
              <span className="label" style={{ color: "var(--mes-navy)" }}>● Needs attention · open now</span>
              <Link to="/tickets" className="label">View all <IconArrowRight size={12} style={{ verticalAlign: -2 }} /></Link>
            </div>
            {attention.length === 0 && (
              <Text size="sm" c="dimmed" py="md">{ticketsPending ? "Loading…" : "Nothing open. All support tickets are resolved."}</Text>
            )}
            {attention.slice(0, ATTENTION_SHOWN).map((a) => (
              <Link key={a.key} to={a.to} className="row">
                <span className={`tag ${a.tone}`}>{a.tag}</span>
                <span className="what">{a.what}</span>
                <span className="when">{fromNow(a.at)}</span>
              </Link>
            ))}
            {attention.length > ATTENTION_SHOWN && (
              <Link to="/tickets" className="label" style={{ display: "block", paddingTop: 10 }}>
                +{attention.length - ATTENTION_SHOWN} more
              </Link>
            )}
          </div>
        )}
      </Group>

      <Group gap={18} align="baseline" mb={22}>
        <h2 className="display" style={{ fontSize: "1.9rem", fontWeight: 700 }}>Pick a <em>lane</em></h2>
        <span className="label" style={{ letterSpacing: "0.3em" }}>{modules.length} modules</span>
      </Group>

      <SimpleGrid cols={{ base: 1, sm: 2, lg: 4 }} spacing={22} className="rise">
        {GROUPS.filter((g) => modules.some((m) => m.group === g)).map((g) => {
          const items = modules.filter((m) => m.group === g);
          const Icon = GROUP_ICON[g];
          const count = g === "Support" ? counts["/tickets"] : undefined;
          return (
            <Link key={g} to={GROUP_ROUTE[g]} className="module-card">
              <span className="icon"><Icon size={22} stroke={1.7} /></span>
              <span className="num">{items.length} MODULES</span>
              {count !== undefined && (
                <Badge className="count" size="sm" variant="light" color={alertsEscalated ? "red" : "brand"}>{count} open</Badge>
              )}
              <div className="title">{groupTitle(g)}</div>
              <div className="desc">{GROUP_DESC[g]}</div>
            </Link>
          );
        })}
      </SimpleGrid>
    </div>
  );
}
