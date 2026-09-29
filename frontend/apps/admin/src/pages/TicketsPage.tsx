import {
  ActionIcon, Anchor, Badge, Button, Drawer, Group, Loader, Modal, Progress, SegmentedControl, Select, SimpleGrid, Stack,
  Table, Text, TextInput, Textarea, Timeline, Tooltip, UnstyledButton,
} from "@mantine/core";
import { type Ticket, type TicketDetail, type TicketMeta, core, unwrap } from "@mes/api-client";
import {
  IconAlertCircle, IconAlertTriangle, IconArrowUpRight, IconArrowsSort, IconBug, IconChevronDown, IconChevronUp,
  IconDownload, IconExternalLink, IconFlame, IconHeadset, IconInfoCircle, IconPlus, IconServer2, IconX, type Icon,
} from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router";
import { useCan } from "../auth";
import { TicketAgentPanel } from "../components/agent";
import { ReasonModal } from "../components/dialogs";
import { PageHeader } from "../components/Layout";
import { usePlant } from "../components/plant";
import { type Cell, csvName, downloadCsv } from "../lib/csv";
import { fmtDateTime, fromNow } from "../lib/format";

type SortKey = "number" | "severity" | "tier" | "status" | "sla" | "reported";
const SEV_ORDER: Record<string, number> = { P1: 1, P2: 2, P3: 3, P4: 4 };
const SORT_VALUE: Record<SortKey, (t: Ticket) => number | string> = {
  number: (t) => t.number,
  severity: (t) => SEV_ORDER[t.severity] ?? 9,
  tier: (t) => t.tier,
  status: (t) => t.status,
  sla: (t) => t.resolution.remaining_s ?? t.response.remaining_s ?? 0,
  reported: (t) => t.reported_at,
};

/** Sortable column header; undecorated (no arrows) until the user picks a sort, preserving the backend's own order. */
function SortTh({
  id, sort, onSort, miw, children,
}: { id: SortKey; sort: { key: SortKey; dir: 1 | -1 } | null; onSort: (id: SortKey) => void; miw?: number; children: React.ReactNode }) {
  const active = sort?.key === id;
  return (
    <Table.Th miw={miw}>
      <UnstyledButton onClick={() => onSort(id)} className="sortable-th">
        <Group gap={4} wrap="nowrap">
          <span>{children}</span>
          {active ? (sort!.dir === 1 ? <IconChevronUp size={12} /> : <IconChevronDown size={12} />) : <IconArrowsSort size={12} opacity={0.35} />}
        </Group>
      </UnstyledButton>
    </Table.Th>
  );
}

const SEV_COLOR: Record<string, string> = { P1: "red", P2: "orange", P3: "yellow", P4: "gray" };
const ORIGIN_COLOR: Record<string, string> = { MES: "navy.6", SERVICENOW: "teal", JIRA: "blue", SYSTEM: "amber", AI: "grape" };
// Same colour identity the severity/tier tiles have always used (formerly a left-stripe box-shadow); now the
// icon-tile top border, matching the wo-kpi style used on every other dashboard in the app.
const SEV_TILE_COLOR: Record<string, string> = { P1: "var(--mes-red)", P2: "#e8590c", P3: "#d99a00", P4: "var(--mes-line-strong)" };
const SEV_TILE_ICON: Record<string, Icon> = { P1: IconFlame, P2: IconAlertTriangle, P3: IconAlertCircle, P4: IconInfoCircle };
const TIER_TILE_COLOR: Record<string, string> = { L1: "var(--mes-navy)", L2: "var(--mes-cyan)", L3: "var(--mes-blue)" };
const TIER_TILE_ICON: Record<string, Icon> = { L1: IconHeadset, L2: IconServer2, L3: IconBug };

export function SeverityBadge({ s, size = "sm" }: { s: string; size?: "xs" | "sm" | "md" | "lg" }) {
  return <Badge size={size} color={SEV_COLOR[s]} variant={s === "P1" ? "filled" : "light"}>{s}</Badge>;
}

function dur(s: number | null | undefined): string {
  if (s === null || s === undefined) return "—";
  const neg = s < 0;
  const m = Math.round(Math.abs(s) / 60);
  const txt = m < 60 ? `${m} min` : m < 48 * 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${Math.round(m / 1440)} d`;
  return neg ? `${txt} over` : `${txt} left`;
}

export function Clock({ c, label }: { c: Ticket["response"]; label: string }) {
  if (c.met_at) return <Text size="xs" c={c.breached ? "red.7" : "green.7"}>{label} {c.breached ? "late" : "met"} {fmtDateTime(c.met_at)}</Text>;
  if (!c.due) return <Text size="xs" c="dimmed">{label}: scheduled</Text>;
  return (
    <Text size="xs" c={c.breached ? "red.7" : (c.remaining_s ?? 0) < 900 ? "amber.8" : undefined} fw={c.breached ? 700 : 400}>
      {label} {c.paused ? "paused · " : ""}{dur(c.remaining_s)}
    </Text>
  );
}

function slaPct(t: Ticket): number {
  const c = t.resolution;
  if (!c.due || c.met_at) return 0;
  const total = new Date(c.due).getTime() - new Date(t.reported_at).getTime();
  return Math.min(100, Math.max(0, 100 - ((c.remaining_s ?? 0) * 1000 * 100) / total));
}

export function TicketsPage() {
  const { plant } = usePlant();
  const pid = plant?.id;
  const [params, setParams] = useSearchParams();
  const [scope, setScope] = useState<"open" | "all">("open");
  const [sev, setSev] = useState<string | null>(null);
  const [tier, setTier] = useState<string | null>(null);
  const [category, setCategory] = useState<string | null>(() => params.get("category"));
  const [search, setSearch] = useState(() => params.get("q") ?? "");
  const [creating, setCreating] = useState(false);
  const canCreate = useCan("ticket:create");
  const meta = useQuery({ queryKey: ["ticket-meta"], queryFn: () => unwrap(core.GET("/api/v1/tickets/meta")), staleTime: Infinity });
  const tickets = useQuery({
    queryKey: ["tickets", pid, scope],
    queryFn: () => unwrap(core.GET("/api/v1/tickets", { params: { query: { plant_id: pid!, open_only: scope === "open", limit: 1000 } } })),
    enabled: Boolean(pid),
    refetchInterval: 5_000,
  });
  const all = tickets.data ?? [];
  const open = all.filter((t) => !["RESOLVED", "CLOSED", "CANCELLED"].includes(t.status));
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 } | null>(null);
  const toggleSort = (key: SortKey) => setSort((s) => (s?.key === key ? { key, dir: s.dir === 1 ? -1 : 1 } : { key, dir: 1 }));
  const rows = useMemo(() => {
    const s = search.trim().toLowerCase();
    const filtered = all.filter((t) => (!sev || t.severity === sev) && (!tier || t.tier === tier) && (!category || t.category === category)
      && (!s || [t.number, t.title, t.sn_number, t.jira_key, t.equipment_code, t.line_code].some((x) => x?.toLowerCase().includes(s))));
    if (!sort) return filtered;
    const val = SORT_VALUE[sort.key];
    return [...filtered].sort((a, b) => {
      const va = val(a);
      const vb = val(b);
      const cmp = typeof va === "number" && typeof vb === "number" ? va - vb : String(va).localeCompare(String(vb));
      return cmp * sort.dir;
    });
  }, [all, sev, tier, category, search, sort]);
  const openId = params.get("ticket");
  const exportCsv = () => {
    const header: Cell[] = ["Ticket", "Severity", "Tier", "Status", "Title", "Assignee", "ServiceNow", "Jira", "Response due", "Response breached", "Resolution due", "Resolution breached", "Reported by", "Reported at"];
    const body: Cell[][] = rows.map((t) => [
      t.number, t.severity, t.tier, t.status, t.title, t.assignee_name, t.sn_number, t.jira_key,
      t.response.due, t.response.breached, t.resolution.due, t.resolution.breached, t.reported_by_name, t.reported_at,
    ]);
    downloadCsv(csvName("tickets"), [header, ...body]);
  };

  return (
    <>
      <PageHeader title="Support tickets" sub="MES support by shop-floor impact. L1/L2 work in ServiceNow, L3 in Jira; status and notes sync both ways."
        right={canCreate && <Button leftSection={<IconPlus size={14} />} onClick={() => setCreating(true)}>New ticket</Button>} />
      <SimpleGrid cols={{ base: 2, sm: 4, lg: 7 }} mb="md" className="rise">
        {(meta.data?.severities ?? []).map((s) => {
          const n = open.filter((t) => t.severity === s.code);
          const late = n.filter((t) => t.response.breached || t.resolution.breached).length;
          const SevIcon = SEV_TILE_ICON[s.code] ?? IconAlertCircle;
          const color = SEV_TILE_COLOR[s.code] ?? "var(--mes-line-strong)";
          return (
            <div key={s.code} className={`panel kpi wo-kpi${sev === s.code ? " selected" : ""}`} style={{ borderTop: `3px solid ${color}` }}
              onClick={() => setSev(sev === s.code ? null : s.code)}>
              <Group justify="space-between" align="flex-start" wrap="nowrap" className="wo-kpi-top">
                <span className="wo-kpi-icon" style={{ background: color }}><SevIcon size={13} stroke={2.2} /></span>
                {late > 0 && <Badge size="xs" color="red" variant="filled">{late} late</Badge>}
              </Group>
              <Text className="label" truncate>{s.code} · {s.label.split(":")[0]}</Text>
              <div className="value">{n.length}</div>
              <Text size="xs" c="dimmed" mt={2} truncate>
                respond &lt; {s.response_min < 60 ? `${s.response_min} min` : `${s.response_min / 60} h`} · fix {s.resolution_h ? `< ${s.resolution_h} h` : "scheduled"}
              </Text>
            </div>
          );
        })}
        {(meta.data?.tiers ?? []).map((t) => {
          const TierIcon = TIER_TILE_ICON[t.code] ?? IconHeadset;
          const color = TIER_TILE_COLOR[t.code] ?? "var(--mes-navy)";
          return (
            <div key={t.code} className={`panel kpi wo-kpi${tier === t.code ? " selected" : ""}`} style={{ borderTop: `3px solid ${color}` }}
              onClick={() => setTier(tier === t.code ? null : t.code)}>
              <div className="wo-kpi-top"><span className="wo-kpi-icon" style={{ background: color }}><TierIcon size={13} stroke={2.2} /></span></div>
              <Text className="label" truncate>{t.code} · {t.team}</Text>
              <div className="value">{open.filter((x) => x.tier === t.code).length}</div>
              <Text size="xs" c="dimmed" mt={2} truncate>{t.target} · {t.code === "L3" ? "Jira" : "ServiceNow"}</Text>
            </div>
          );
        })}
      </SimpleGrid>
      <Group mb="sm" justify="space-between">
        <Group gap="xs">
          <SegmentedControl size="xs" value={scope} onChange={(v) => setScope(v as "open" | "all")} data={[{ value: "open", label: "Open" }, { value: "all", label: "All" }]} />
          <TextInput size="xs" w={260} placeholder="Search number, INC, Jira key, machine"
            rightSection={search ? (
              <ActionIcon variant="subtle" color="gray" size="xs" onClick={() => setSearch("")} aria-label="Clear search"><IconX size={12} /></ActionIcon>
            ) : undefined}
            value={search} onChange={(e) => setSearch(e.currentTarget.value)} />
          {category && (
            <Badge size="lg" variant="light" color="brand" style={{ textTransform: "none" }} rightSection={
              <ActionIcon size="xs" variant="transparent" color="navy.6" onClick={() => setCategory(null)} aria-label="Clear category filter"><IconX size={10} /></ActionIcon>
            }>{meta.data?.categories.find((c) => c.code === category)?.label ?? category}</Badge>
          )}
          {(sev || tier || category) && <Button size="xs" variant="subtle" onClick={() => { setSev(null); setTier(null); setCategory(null); }}>Clear filters</Button>}
        </Group>
        <ActionIcon variant="light" size="input-xs" disabled={!rows.length} onClick={exportCsv} aria-label="Download CSV"><IconDownload size={14} /></ActionIcon>
      </Group>
      <Table className="panel" highlightOnHover>
        <Table.Thead>
          <Table.Tr>
            <SortTh id="number" sort={sort} onSort={toggleSort}>Ticket</SortTh>
            <SortTh id="severity" sort={sort} onSort={toggleSort}>Sev</SortTh>
            <Table.Th>Problem</Table.Th>
            <SortTh id="tier" sort={sort} onSort={toggleSort}>Tier</SortTh>
            <SortTh id="status" sort={sort} onSort={toggleSort}>Status</SortTh>
            <SortTh id="sla" miw={190} sort={sort} onSort={toggleSort}>SLA</SortTh>
            <Table.Th>ServiceNow / Jira</Table.Th>
            <SortTh id="reported" sort={sort} onSort={toggleSort}>Reported</SortTh>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {rows.map((t) => (
            <Table.Tr key={t.id} style={{ cursor: "pointer" }} className={t.response.breached || t.resolution.breached ? "row-late" : undefined}
              onClick={() => setParams({ ticket: t.id })}>
              <Table.Td className="mono">{t.number}</Table.Td>
              <Table.Td><SeverityBadge s={t.severity} /></Table.Td>
              <Table.Td><Text size="sm" fw={500} lineClamp={1} maw={360}>{t.title}</Text>
                <Text size="xs" c="dimmed">{[t.line_code && `line ${t.line_code}`, t.equipment_code, t.erp_order_number, t.terminal_code, t.category.toLowerCase().replace("_", " ")].filter(Boolean).join(" · ")}</Text></Table.Td>
              <Table.Td><Badge size="sm" variant="outline" color={t.tier === "L3" ? "blue" : t.tier === "L2" ? "teal" : "navy.6"}>{t.tier}</Badge></Table.Td>
              <Table.Td><Text size="xs">{t.status.replace("_", " ").toLowerCase()}</Text>
                <Text size="10px" c={t.assignee_name ? undefined : "dimmed"}>{t.assignee_name ?? "unassigned"}</Text></Table.Td>
              <Table.Td>
                <Clock c={t.response} label="response" />
                <Clock c={t.resolution} label="fix" />
                {t.resolution.due && !t.resolution.met_at && <Progress mt={3} size={4} value={slaPct(t)} color={t.resolution.breached ? "red" : slaPct(t) > 75 ? "amber" : "green"} bg="navy.1" />}
              </Table.Td>
              <Table.Td>
                <Text size="xs" className="mono" c={t.itsm_sync_status === "FAILED" ? "red.7" : undefined}>{t.sn_number ?? (t.itsm_sync_status === "PENDING" ? "syncing…" : "—")}</Text>
                {t.jira_key && <Text size="xs" className="mono" c="blue.3">{t.jira_key} {t.jira_status ? `· ${t.jira_status}` : ""}</Text>}
              </Table.Td>
              <Table.Td><Text size="xs">{t.reported_by_name}</Text><Text size="10px" c="dimmed">{fromNow(t.reported_at)} · {t.source.toLowerCase()}</Text></Table.Td>
            </Table.Tr>
          ))}
          {rows.length === 0 && <Table.Tr><Table.Td colSpan={8}><Text c="dimmed" size="sm">No tickets.</Text></Table.Td></Table.Tr>}
        </Table.Tbody>
      </Table>
      <TicketDrawer id={openId} meta={meta.data} onClose={() => setParams({})} />
      {creating && pid && <NewTicketModal plantId={pid} meta={meta.data} onClose={() => setCreating(false)} onCreated={(t) => { setCreating(false); setParams({ ticket: t.id }); }} />}
    </>
  );
}

export function TicketDrawer({ id, meta, onClose }: { id: string | null; meta?: TicketMeta; onClose: () => void }) {
  const qc = useQueryClient();
  const canManage = useCan("ticket:manage");
  const canComment = useCan("ticket:create");
  const q = useQuery({
    queryKey: ["ticket", id],
    queryFn: () => unwrap(core.GET("/api/v1/tickets/{ticket_id}", { params: { path: { ticket_id: id! } } })),
    enabled: Boolean(id),
    refetchInterval: 4_000,
  });
  const [comment, setComment] = useState("");
  const [modal, setModal] = useState<null | { kind: "status"; status: string } | { kind: "escalate" } | { kind: "severity"; severity: string }>(null);
  useEffect(() => { setComment(""); setModal(null); }, [id]);
  // a finished action refreshes the ticket; only the dialog's own submission closes the dialog (another action
  // finishing meanwhile must not close it under the user's hands)
  const done = (t: TicketDetail) => { qc.setQueryData(["ticket", id], t); qc.invalidateQueries({ queryKey: ["tickets"] }); };
  const act = useMutation({
    mutationFn: ({ path, body }: { path: "status" | "escalate" | "severity" | "comments" | "assign"; body: Record<string, unknown> }) =>
      unwrap(core.POST(`/api/v1/tickets/{ticket_id}/${path}` as "/api/v1/tickets/{ticket_id}/status", { params: { path: { ticket_id: id! } }, body: body as never })),
    onSuccess: done,
  });
  const t = q.data;
  const next = (t && meta?.transitions[t.status]) ?? [];
  const label: Record<string, string> = { ACKNOWLEDGED: "Acknowledge", IN_PROGRESS: t?.status === "RESOLVED" ? "Reopen" : "Start work",
    ON_HOLD: "On hold", RESOLVED: "Resolve…", CLOSED: "Close", CANCELLED: "Cancel" };

  return (
    <Drawer opened={Boolean(id)} onClose={onClose} position="right" size="xl"
      title={t && <Group gap="sm"><Text fw={700} className="mono">{t.number}</Text><SeverityBadge s={t.severity} /><Badge variant="outline">{t.tier}</Badge>
        <Text size="sm" c="dimmed">{t.status.replace("_", " ").toLowerCase()}</Text></Group>}>
      {!t ? <Loader size="sm" /> : (
        <Stack>
          <div className="panel" style={{ padding: 12 }}>
            <Text fw={600} size="lg">{t.title}</Text>
            <Text size="sm" mt={4} style={{ whiteSpace: "pre-wrap" }}>{t.description}</Text>
            <Group gap="xl" mt="sm" align="flex-start">
              <div><div className="label">Impact</div><Text size="sm">{meta?.impacts.find((i) => i.code === t.impact)?.label ?? t.impact}</Text></div>
              <div><div className="label">Category</div><Text size="sm">{meta?.categories.find((i) => i.code === t.category)?.label ?? t.category}</Text></div>
              <div><div className="label">Where</div><Text size="sm" className="mono">{[t.line_code && `L: ${t.line_code}`, t.equipment_code, t.erp_order_number, t.terminal_code].filter(Boolean).join(" · ") || "—"}</Text></div>
              <div><div className="label">Reported</div><Text size="sm">{t.reported_by_name} · {t.source.toLowerCase()}</Text><Text size="xs" c="dimmed">{fmtDateTime(t.reported_at)}</Text></div>
              <div>
                <div className="label">Assigned to</div>
                {canManage && OPEN.includes(t.status)
                  ? <AssigneeSelect plantId={t.plant_id} value={t.assignee_name} busy={act.isPending}
                      onChange={(who) => act.mutate({ path: "assign", body: { assignee: who } })} />
                  : <Text size="sm" c={t.assignee_name ? undefined : "dimmed"}>{t.assignee_name ?? "unassigned"}</Text>}
              </div>
            </Group>
            {t.severity !== t.suggested_severity && <Text size="xs" c="amber.8" mt={6}>Severity set to {t.severity} (suggested from impact: {t.suggested_severity}).</Text>}
          </div>
          <SimpleGrid cols={3}>
            <div className="panel" style={{ padding: 10 }}><div className="label">Response target</div><Clock c={t.response} label="response" /><Text size="10px" c="dimmed">due {fmtDateTime(t.response.due)}</Text></div>
            <div className="panel" style={{ padding: 10 }}><div className="label">Resolution target</div><Clock c={t.resolution} label="fix" /><Text size="10px" c="dimmed">{t.resolution.due ? `due ${fmtDateTime(t.resolution.due)}` : "scheduled (P4)"}</Text></div>
            <div className="panel" style={{ padding: 10 }}>
              <div className="label">External</div>
              {t.sn_number ? <ExternalRef label={t.sn_number} url={t.sn_url} />
                : <Text size="sm" c={t.itsm_sync_status === "FAILED" ? "red.7" : "dimmed"}>{t.itsm_sync_status === "FAILED" ? "sync failed" : "queued for ServiceNow"}</Text>}
              {t.sn_group && <Text size="10px" c="dimmed">{t.sn_group}</Text>}
              {t.jira_key && <><br /><ExternalRef label={t.jira_key} url={t.jira_url} /><Text span size="10px" c="dimmed"> {t.jira_status}</Text></>}
            </div>
          </SimpleGrid>
          {t.resolution_note && <div className="panel" style={{ padding: 10 }}><div className="label">Resolution</div><Text size="sm">{t.resolution_note}</Text></div>}
          <TicketAgentPanel ticketId={t.id} />
          {canManage && (
            <Group gap="xs">
              {next.map((s) => (
                <Button key={s} size="xs" variant={s === "RESOLVED" ? "filled" : s === "CANCELLED" ? "subtle" : "light"} color={s === "CANCELLED" ? "gray" : undefined}
                  onClick={() => (["RESOLVED", "ON_HOLD", "CANCELLED"].includes(s) ? setModal({ kind: "status", status: s }) : act.mutate({ path: "status", body: { status: s } }))}>
                  {label[s] ?? s}
                </Button>
              ))}
              {t.tier !== "L3" && !["RESOLVED", "CLOSED", "CANCELLED"].includes(t.status) && (
                <Button size="xs" color="blue" variant="light" leftSection={<IconArrowUpRight size={14} />} onClick={() => setModal({ kind: "escalate" })}>
                  Escalate to {t.tier === "L1" ? "L2" : "L3 (Jira)"}
                </Button>
              )}
              {!["RESOLVED", "CLOSED", "CANCELLED"].includes(t.status) && (
                <Select size="xs" w={110} placeholder="Severity" data={["P1", "P2", "P3", "P4"].filter((x) => x !== t.severity)} value={null}
                  onChange={(v) => v && setModal({ kind: "severity", severity: v })} />
              )}
            </Group>
          )}
          {canComment && (
            <Group align="flex-end" wrap="nowrap">
              <Textarea style={{ flex: 1 }} autosize minRows={1} placeholder="Comment (becomes a ServiceNow work note; a Jira comment at L3)" value={comment} onChange={(e) => setComment(e.currentTarget.value)} />
              <Button disabled={!comment.trim()} loading={act.isPending} onClick={() => act.mutate({ path: "comments", body: { text: comment } }, { onSuccess: () => setComment("") })}>Comment</Button>
            </Group>
          )}
          <div className="panel" style={{ padding: 12 }}>
            <Text className="label" mb="xs">Activity</Text>
            <Timeline bulletSize={12} lineWidth={2}>
              {[...t.activity].reverse().map((a) => (
                <Timeline.Item key={a.id} color={ORIGIN_COLOR[a.origin] ?? "gray"}
                  title={<Group gap={6}><Badge size="xs" variant="light" color={ORIGIN_COLOR[a.origin]}>{a.origin === "SERVICENOW" ? "ServiceNow" : a.origin === "JIRA" ? "Jira" : a.origin}</Badge>
                    <Text size="sm" fw={600}>{a.kind.replace("_", " ").toLowerCase()}</Text><Text size="xs" c="dimmed">{a.actor_name}</Text></Group>}>
                  {a.text && <Text size="sm" style={{ whiteSpace: "pre-wrap" }}>{a.text}</Text>}
                  <Text size="10px" c="dimmed" className="mono">{fmtDateTime(a.at)}</Text>
                </Timeline.Item>
              ))}
            </Timeline>
          </div>
          <ReasonModal
            opened={modal !== null}
            title={modal?.kind === "escalate" ? `Escalate ${t.number} to ${t.tier === "L1" ? "L2" : "L3"}` : modal?.kind === "severity" ? `Change severity to ${modal.severity}`
              : modal?.kind === "status" && modal.status === "RESOLVED" ? "Resolve ticket" : `Set ${modal?.kind === "status" ? modal.status.replace("_", " ").toLowerCase() : ""}`}
            label={modal?.kind === "status" && modal.status === "RESOLVED" ? "What fixed it?" : "Reason"}
            busy={act.isPending}
            onClose={() => setModal(null)}
            onSubmit={(reason) => {
              if (!modal) return;
              const close = { onSuccess: () => setModal(null) };
              if (modal.kind === "escalate") act.mutate({ path: "escalate", body: { reason } }, close);
              else if (modal.kind === "severity") act.mutate({ path: "severity", body: { severity: modal.severity, reason } }, close);
              else act.mutate({ path: "status", body: { status: modal.status, note: reason } }, close);
            }}
          />
        </Stack>
      )}
    </Drawer>
  );
}

const OPEN = ["NEW", "ACKNOWLEDGED", "IN_PROGRESS", "ON_HOLD"];
export type SupportMember = { id: string; username: string; display_name: string; email: string | null; tier: string | null; active: boolean; open_tickets: number };

export function useSupportTeam(plantId?: string) {
  return useQuery({
    queryKey: ["support-team", plantId],
    queryFn: () => unwrap(core.GET("/api/v1/tickets/support-team", { params: { query: { plant_id: plantId! } } })) as Promise<SupportMember[]>,
    enabled: Boolean(plantId),
    staleTime: 30_000,
  });
}

/** Support engineer picker (ServiceNow "Assigned to" follows). */
function AssigneeSelect({ plantId, value, busy, onChange }: { plantId: string; value: string | null; busy: boolean; onChange: (who: string | null) => void }) {
  const team = useSupportTeam(plantId);
  const data = (team.data ?? []).map((m) => ({ value: m.username, label: `${m.display_name}${m.tier ? ` · ${m.tier}` : ""}` }));
  const current = (team.data ?? []).find((m) => m.display_name === value)?.username ?? null;
  return (
    <Group gap={6} wrap="nowrap">
      <Select size="xs" w={190} placeholder={value ?? "unassigned"} data={data} value={current} clearable searchable disabled={busy}
        aria-label="Assigned to" onChange={(v) => v !== current && onChange(v)} />
      {value && !current && <Text size="xs" c="dimmed">{value}</Text>}
    </Group>
  );
}

function NewTicketModal({ plantId, meta, onClose, onCreated }: { plantId: string; meta?: TicketMeta; onClose: () => void; onCreated: (t: TicketDetail) => void }) {
  const [title, setTitle] = useState("");
  const [desc, setDesc] = useState("");
  const [category, setCategory] = useState<string | null>("MES_APP");
  const [impact, setImpact] = useState("MINOR");
  const [severity, setSeverity] = useState<string | null>(null);
  const [machine, setMachine] = useState<string | null>(null);
  const eqs = useQuery({ queryKey: ["equipment-health", plantId], queryFn: () => unwrap(core.GET("/api/v1/maintenance/equipment", { params: { query: { plant_id: plantId } } })) });
  const suggested = category && ["ACCESS", "CHANGE_REQUEST"].includes(category) && impact !== "LINE_STOP" ? "P4" : meta?.impact_severity[impact];
  const m = useMutation({
    mutationFn: () => unwrap(core.POST("/api/v1/tickets", { body: { plant_id: plantId, title, description: desc, category: category as "MES_APP",
      impact: impact as "MINOR", severity: severity as "P1" | null, equipment_id: machine, client_event_id: crypto.randomUUID() } })),
    onSuccess: onCreated,
  });
  return (
    <Modal opened onClose={onClose} size="lg" centered title={<Text fw={600}>New support ticket</Text>}>
      <Stack>
        <TextInput label="Title" value={title} onChange={(e) => setTitle(e.currentTarget.value)} placeholder="e.g. Label printer at L2 end of line prints blank labels" data-autofocus />
        <Textarea label="Description" autosize minRows={2} value={desc} onChange={(e) => setDesc(e.currentTarget.value)} />
        <Group grow>
          <Select label="Category" data={(meta?.categories ?? []).map((c) => ({ value: c.code, label: c.label }))} value={category} onChange={setCategory} />
          <Select label="Machine (optional)" clearable searchable data={(eqs.data ?? []).map((e) => ({ value: e.id, label: `${e.code} · ${e.name}` }))} value={machine} onChange={setMachine} />
        </Group>
        <div>
          <Text size="sm" fw={500} mb={4}>Impact on production</Text>
          <SegmentedControl fullWidth value={impact} onChange={setImpact} data={(meta?.impacts ?? []).map((i) => ({ value: i.code, label: i.label }))} />
        </div>
        <Group>
          <Text size="sm">Suggested severity</Text>{suggested && <SeverityBadge s={suggested} size="md" />}
          <Tooltip label="Override only with a reason in the description"><Select size="xs" w={140} placeholder="override" clearable data={["P1", "P2", "P3", "P4"]} value={severity} onChange={setSeverity} /></Tooltip>
        </Group>
        <Text size="xs" c="dimmed">The ticket starts at L1 and is created as a ServiceNow incident. Escalating to L3 opens a linked Jira issue.</Text>
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button loading={m.isPending} disabled={!title.trim() || !category} onClick={() => m.mutate()}>Raise ticket</Button>
        </Group>
      </Stack>
    </Modal>
  );
}

function ExternalRef({ label, url }: { label: string; url: string | null }) {
  return url
    ? <Anchor size="sm" href={url} target="_blank" rel="noreferrer" className="mono">{label} <IconExternalLink size={11} /></Anchor>
    : <Text span size="sm" className="mono">{label}</Text>;
}
