import {
  ActionIcon, Alert as MAlert, Badge, Button, Checkbox, Drawer, Group, NumberInput, SegmentedControl, SimpleGrid, Stack,
  Switch, Table, Tabs, Text, TextInput, UnstyledButton,
} from "@mantine/core";
import { type Alert, notify } from "@mes/api-client";
import {
  IconAlertTriangle, IconArrowsSort, IconBell, IconCheck, IconChevronDown, IconChevronUp, IconClock, IconDownload,
  IconFlame, IconMail, IconPlus, IconSearch, IconSend, IconTrash, IconX, type Icon,
} from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { useMe } from "../auth";
import { PageHeader } from "../components/Layout";
import { type Cell, csvName, downloadCsv } from "../lib/csv";
import { fmtDateTime, fromNow } from "../lib/format";

/** Clickable KPI tile: icon + colour-accented top border + hover lift (same look as Work Orders/Maintenance/Quality). */
function NotifyKpi({
  icon: Icon, label, value, sub, color, hot, onClick,
}: { icon: Icon; label: string; value: string | number; sub?: string; color: string; hot?: boolean; onClick?: () => void }) {
  return (
    <div className="panel kpi wo-kpi" style={{ borderTop: `3px solid ${color}`, cursor: onClick ? "pointer" : "default" }}
      onClick={onClick} role={onClick ? "button" : undefined} tabIndex={onClick ? 0 : undefined}
      onKeyDown={onClick ? (e) => e.key === "Enter" && onClick() : undefined}>
      <div className="wo-kpi-top"><span className="wo-kpi-icon" style={{ background: color }}><Icon size={13} stroke={2.2} /></span></div>
      <div className="label">{label}</div>
      <div className="value" style={{ color: hot ? color : undefined }}>{value}</div>
      {sub && <Text size="xs" c="dimmed" mt={2}>{sub}</Text>}
    </div>
  );
}

/** Generic sortable column header (chevron/sort icon), matching the pattern used on other pages this session. */
function SortTh<K extends string>({
  id, sort, onSort, children,
}: { id: K; sort: { key: K; dir: 1 | -1 } | null; onSort: (k: K) => void; children: React.ReactNode }) {
  const active = sort?.key === id;
  return (
    <Table.Th>
      <UnstyledButton onClick={() => onSort(id)} className="sortable-th">
        <Group gap={4} wrap="nowrap">
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

type Summary = { open: number; escalated: number; channels: { email: boolean; teams: boolean; in_app: boolean } };
type RotaRow = { role: string; position: number; name: string; email: string; teams: boolean; active: boolean };
type Rule = { key: string; label: string; enabled: boolean; channels: string[]; ack_minutes: number; updated_by: string | null };
type Delivery = { id: number; level: number; channel: string; recipient: string; ok: boolean; error: string | null; at: string };

const STATUS_COLOR: Record<string, string> = { NEW: "amber", SENT: "amber", ACKED: "green", CLOSED: "gray" };
const LEVEL = ["on call", "backup", "supervisor"];
const ROLES = ["L1", "L2", "L3", "SUPERVISOR"];

/** Header bell: open alerts (sent, not yet acknowledged). */
export function AlertBell() {
  const q = useQuery({ queryKey: ["notify", "summary"], queryFn: () => notify<Summary>("/summary"), refetchInterval: 15_000, retry: false });
  const n = q.data?.open ?? 0;
  return (
    <Link to="/notifications" aria-label={`${n} open alerts`} style={{ color: "inherit", display: "flex", alignItems: "center", gap: 4 }}>
      <IconBell size={20} color={n ? "var(--mes-amber)" : "var(--mes-text-dim)"} />
      {n > 0 && <Badge size="sm" color={q.data?.escalated ? "red" : "amber"} variant="filled">{n}</Badge>}
    </Link>
  );
}

export function NotificationsPage() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "alerts";
  return (
    <>
      <PageHeader title="Notifications" sub="Alerts to the on-call support people: SLA at risk or missed, P1s, escalations, AI approvals and handovers" />
      <Tabs value={tab} onChange={(v) => setParams(v === "alerts" ? {} : { tab: v ?? "alerts" })}>
        <Tabs.List mb="md">
          <Tabs.Tab value="alerts">Alerts</Tabs.Tab>
          <Tabs.Tab value="rota">On-call rota</Tabs.Tab>
          <Tabs.Tab value="rules">Alert rules</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="alerts"><Alerts /></Tabs.Panel>
        <Tabs.Panel value="rota"><Rota /></Tabs.Panel>
        <Tabs.Panel value="rules"><Rules /></Tabs.Panel>
      </Tabs>
    </>
  );
}

type AlertSortKey = "raised" | "title" | "ticket" | "status";
const ALERT_SORT_VALUE: Record<AlertSortKey, (a: Alert) => number | string> = {
  raised: (a) => a.created_at,
  title: (a) => a.title,
  ticket: (a) => a.ticket_number ?? "",
  status: (a) => a.status,
};

function Alerts() {
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [openOnly, setOpenOnly] = useState(true);
  const [open, setOpen] = useState<string | null>(null);
  const [acked, setAcked] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<{ key: AlertSortKey; dir: 1 | -1 } | null>(null);
  const list = useQuery({ queryKey: ["notify", "alerts", openOnly], queryFn: () => notify<Alert[]>(`/notifications?open_only=${openOnly}`), refetchInterval: 5000 });
  const summary = useQuery({ queryKey: ["notify", "summary"], queryFn: () => notify<Summary>("/summary") });
  const rows = useMemo(() => {
    const s = search.trim().toLowerCase();
    const filtered = (list.data ?? []).filter((a) => !s
      || a.title.toLowerCase().includes(s) || a.rule.toLowerCase().includes(s) || (a.ticket_number ?? "").toLowerCase().includes(s));
    return sortRows(filtered, sort, ALERT_SORT_VALUE);
  }, [list.data, search, sort]);
  const openNow = (list.data ?? []).filter((a) => ["NEW", "SENT"].includes(a.status));
  const p1p2 = openNow.filter((a) => a.severity === "P1" || a.severity === "P2").length;
  const oldest = openNow.reduce((min: string | null, a) => (!min || a.created_at < min ? a.created_at : min), null);
  const exportCsv = () => {
    const header: Cell[] = ["Raised", "Alert", "Rule", "Ticket", "Alerted to", "Escalated", "Status", "Acked by", "Acked at"];
    const body: Cell[][] = rows.map((a) => [a.created_at, a.title, a.rule, a.ticket_number, a.recipients.map((r) => r.name).join("; "),
      a.level > 0 ? LEVEL[a.level] : "", a.status, a.acked_by, a.acked_at]);
    downloadCsv(csvName("alerts"), [header, ...body]);
  };
  const ack = useMutation({
    mutationFn: (id: string) => notify(`/notifications/${id}/ack`, { method: "POST", body: "{}" }),
    onSettled: () => qc.invalidateQueries({ queryKey: ["notify"] }),
  });
  // the link in an alert e-mail / Teams card: ?ack=<id> acknowledges once
  const done = useRef(false);
  useEffect(() => {
    const id = params.get("ack");
    if (id && !done.current) {
      done.current = true;
      ack.mutate(id, { onSettled: () => { setAcked(id); setParams({}); } });
    }
  }, [params, setParams, ack]);
  const ch = summary.data?.channels;
  return (
    <Stack>
      {acked && <MAlert color="green" icon={<IconCheck />} withCloseButton onClose={() => setAcked(null)}>Alert acknowledged: it will not escalate further.</MAlert>}
      <SimpleGrid cols={{ base: 2, md: 4 }} className="rise">
        <NotifyKpi icon={IconBell} label="Open alerts" value={summary.data?.open ?? openNow.length} color="var(--mes-amber)"
          hot={Boolean(summary.data?.open)} sub="waiting for ack" />
        <NotifyKpi icon={IconAlertTriangle} label="Escalated" value={summary.data?.escalated ?? 0} color="var(--mes-red)"
          hot={Boolean(summary.data?.escalated)} sub="to supervisor" />
        <NotifyKpi icon={IconFlame} label="P1 / P2 open" value={p1p2} color="var(--mes-red)" hot={p1p2 > 0} />
        <NotifyKpi icon={IconClock} label="Oldest waiting" value={oldest ? fromNow(oldest) : "—"} color="var(--mes-navy)" />
      </SimpleGrid>
      <Group justify="space-between" wrap="wrap">
        <SegmentedControl size="xs" value={openOnly ? "open" : "all"} onChange={(v) => setOpenOnly(v === "open")}
          data={[{ value: "open", label: "Open" }, { value: "all", label: "All" }]} />
        <Group gap="xs">
          {ch && (
            <Group gap="xs">
              <Badge variant="outline" color={ch.email ? "green" : "gray"} leftSection={<IconMail size={12} />}>e-mail {ch.email ? "on" : "off"}</Badge>
              <Badge variant="outline" color={ch.teams ? "green" : "gray"}>Teams {ch.teams ? "on" : "off"}</Badge>
              <Badge variant="outline" color="green">in-app</Badge>
            </Group>
          )}
          <TextInput size="xs" w={240} leftSection={<IconSearch size={14} />}
            rightSection={search ? (
              <ActionIcon variant="subtle" color="gray" size="xs" onClick={() => setSearch("")} aria-label="Clear search"><IconX size={12} /></ActionIcon>
            ) : undefined}
            placeholder="Alert, ticket, rule…" value={search} onChange={(e) => setSearch(e.currentTarget.value)} />
          <ActionIcon variant="light" size="input-xs" disabled={!rows.length} onClick={exportCsv} aria-label="Download CSV"><IconDownload size={14} /></ActionIcon>
        </Group>
      </Group>
      <Table className="panel" highlightOnHover>
        <Table.Thead>
          <Table.Tr>
            <SortTh id="raised" sort={sort} onSort={(k) => toggleSort(setSort, k)}>Raised</SortTh>
            <SortTh id="title" sort={sort} onSort={(k) => toggleSort(setSort, k)}>Alert</SortTh>
            <SortTh id="ticket" sort={sort} onSort={(k) => toggleSort(setSort, k)}>Ticket</SortTh>
            <Table.Th>Alerted</Table.Th>
            <SortTh id="status" sort={sort} onSort={(k) => toggleSort(setSort, k)}>Status</SortTh>
            <Table.Th />
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {rows.map((a) => (
            <Table.Tr key={a.id} style={{ cursor: "pointer" }} onClick={() => setOpen(a.id)}>
              <Table.Td className="mono">{fmtDateTime(a.created_at)}</Table.Td>
              <Table.Td>
                <Text size="sm" fw={600}>{a.title}</Text>
                <Text size="10px" c="dimmed" className="mono">{a.rule.replace("_", " ").toLowerCase()}</Text>
              </Table.Td>
              <Table.Td>{a.ticket_id ? <Link to={`/tickets?ticket=${a.ticket_id}`} className="mono" style={{ color: "inherit" }} onClick={(e) => e.stopPropagation()}>{a.ticket_number}</Link> : "—"}</Table.Td>
              <Table.Td>
                <Text size="xs">{[...new Set(a.recipients.map((r) => r.name))].join(", ") || "queued"}</Text>
                {a.level > 0 && <Badge size="xs" color="red" variant="light">escalated to {LEVEL[a.level]}</Badge>}
              </Table.Td>
              <Table.Td>
                <Badge size="sm" variant="light" color={STATUS_COLOR[a.status]}>{a.status === "SENT" ? "waiting for ack" : a.status.toLowerCase()}</Badge>
                {a.acked_by && <Text size="10px" c="dimmed">{a.acked_by} {fromNow(a.acked_at)}</Text>}
                {a.closed_reason && <Text size="10px" c="dimmed">{a.closed_reason}</Text>}
              </Table.Td>
              <Table.Td>
                {["NEW", "SENT"].includes(a.status) && (
                  <Button size="compact-xs" onClick={(e) => { e.stopPropagation(); ack.mutate(a.id); }} loading={ack.isPending && ack.variables === a.id}>Acknowledge</Button>
                )}
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      {list.data?.length === 0 && <Text c="dimmed" size="sm">No {openOnly ? "open " : ""}alerts.</Text>}
      {Boolean(list.data?.length) && rows.length === 0 && <Text c="dimmed" size="sm">No alerts match your search.</Text>}
      <AlertDrawer id={open} onClose={() => setOpen(null)} />
    </Stack>
  );
}

function AlertDrawer({ id, onClose }: { id: string | null; onClose: () => void }) {
  const q = useQuery({ queryKey: ["notify", "alert", id], queryFn: () => notify<Alert & { deliveries: Delivery[] }>(`/notifications/${id}`), enabled: Boolean(id) });
  const a = q.data;
  return (
    <Drawer opened={Boolean(id)} onClose={onClose} position="right" size="lg" title={a && <Text fw={700}>{a.title}</Text>}>
      {a && (
        <Stack>
          <Text size="sm" style={{ whiteSpace: "pre-wrap" }}>{a.body}</Text>
          <Text className="label">Deliveries</Text>
          <Table>
            <Table.Tbody>
              {a.deliveries.map((d) => (
                <Table.Tr key={d.id}>
                  <Table.Td className="mono">{fmtDateTime(d.at)}</Table.Td>
                  <Table.Td>{LEVEL[d.level]}</Table.Td>
                  <Table.Td>{d.channel}</Table.Td>
                  <Table.Td className="mono">{d.recipient}</Table.Td>
                  <Table.Td>{d.ok ? <Badge size="xs" color="green" variant="light">sent</Badge> : <Text size="xs" c="red.7">{d.error}</Text>}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Stack>
      )}
    </Drawer>
  );
}

function Rota() {
  const qc = useQueryClient();
  const isAdmin = useMe().roles.includes("admin");
  const q = useQuery({ queryKey: ["notify", "rota"], queryFn: () => notify<RotaRow[]>("/rota") });
  const [rows, setRows] = useState<RotaRow[] | null>(null);
  useEffect(() => { if (q.data) setRows(q.data.map(({ role, position, name, email, teams, active }) => ({ role, position, name, email, teams, active }))); }, [q.data]);
  const save = useMutation({
    mutationFn: (r: RotaRow[]) => notify<RotaRow[]>("/rota", { method: "PUT", body: JSON.stringify(r) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["notify", "rota"] }),
  });
  const test = useMutation({ mutationFn: () => notify("/test", { method: "POST" }), onSuccess: () => qc.invalidateQueries({ queryKey: ["notify"] }) });
  if (!rows) return null;
  const set = (i: number, patch: Partial<RotaRow>) => setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return (
    <Stack maw={1000}>
      <Text size="sm" c="dimmed">Each alert goes to the role's position 1 first. Not acknowledged in time: position 2 (backup), then the supervisor.</Text>
      <Table className="panel">
        <Table.Thead><Table.Tr><Table.Th>Role</Table.Th><Table.Th>Position</Table.Th><Table.Th>Name</Table.Th><Table.Th>E-mail</Table.Th><Table.Th>Teams</Table.Th><Table.Th>Active</Table.Th><Table.Th /></Table.Tr></Table.Thead>
        <Table.Tbody>
          {rows.map((r, i) => (
            <Table.Tr key={i}>
              <Table.Td><SegmentedControl size="xs" disabled={!isAdmin} value={r.role} data={ROLES} onChange={(v) => set(i, { role: v })} /></Table.Td>
              <Table.Td><NumberInput size="xs" w={70} min={1} max={9} disabled={!isAdmin} value={r.position} onChange={(v) => set(i, { position: Number(v) })} /></Table.Td>
              <Table.Td><TextInput size="xs" disabled={!isAdmin} value={r.name} onChange={(e) => set(i, { name: e.currentTarget.value })} /></Table.Td>
              <Table.Td><TextInput size="xs" disabled={!isAdmin} value={r.email} onChange={(e) => set(i, { email: e.currentTarget.value })} /></Table.Td>
              <Table.Td><Checkbox disabled={!isAdmin} checked={r.teams} onChange={(e) => set(i, { teams: e.currentTarget.checked })} /></Table.Td>
              <Table.Td><Checkbox disabled={!isAdmin} checked={r.active} onChange={(e) => set(i, { active: e.currentTarget.checked })} /></Table.Td>
              <Table.Td>{isAdmin && <Button size="compact-xs" variant="subtle" color="gray" onClick={() => setRows(rows.filter((_, j) => j !== i))}><IconTrash size={14} /></Button>}</Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      {isAdmin && (
        <Group>
          <Button size="xs" variant="default" leftSection={<IconPlus size={14} />}
            onClick={() => setRows([...rows, { role: "L1", position: 2, name: "", email: "", teams: true, active: true }])}>Add person</Button>
          <Button size="xs" loading={save.isPending} disabled={JSON.stringify(rows) === JSON.stringify(q.data?.map(({ role, position, name, email, teams, active }) => ({ role, position, name, email, teams, active })))}
            onClick={() => save.mutate(rows)}>Save rota</Button>
          <Button size="xs" variant="subtle" leftSection={<IconSend size={14} />} loading={test.isPending} onClick={() => test.mutate()}>Send a test alert</Button>
          {save.error && <Text size="sm" c="red.7">{String(save.error.message)}</Text>}
          {test.isSuccess && <Text size="sm" c="green.7">Test alert queued for the L1 on call.</Text>}
        </Group>
      )}
    </Stack>
  );
}

function Rules() {
  const qc = useQueryClient();
  const isAdmin = useMe().roles.includes("admin");
  const q = useQuery({ queryKey: ["notify", "rules"], queryFn: () => notify<Rule[]>("/rules") });
  const save = useMutation({
    mutationFn: (r: Rule) => notify(`/rules/${r.key}`, { method: "PUT", body: JSON.stringify({ enabled: r.enabled, channels: r.channels, ack_minutes: r.ack_minutes }) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["notify", "rules"] }),
  });
  return (
    <Table className="panel" maw={1000}>
      <Table.Thead><Table.Tr><Table.Th>Alert</Table.Th><Table.Th>On</Table.Th><Table.Th>Channels</Table.Th><Table.Th>Escalate if not acknowledged in</Table.Th></Table.Tr></Table.Thead>
      <Table.Tbody>
        {(q.data ?? []).map((r) => (
          <Table.Tr key={r.key}>
            <Table.Td><Text size="sm">{r.label}</Text><Text size="10px" c="dimmed" className="mono">{r.key}</Text></Table.Td>
            <Table.Td><Switch disabled={!isAdmin} checked={r.enabled} onChange={(e) => save.mutate({ ...r, enabled: e.currentTarget.checked })} /></Table.Td>
            <Table.Td>
              <Group gap="xs">
                {["email", "teams"].map((c) => (
                  <Checkbox key={c} label={c === "email" ? "e-mail" : "Teams"} disabled={!isAdmin} checked={r.channels.includes(c)}
                    onChange={(e) => save.mutate({ ...r, channels: e.currentTarget.checked ? [...r.channels, c] : r.channels.filter((x) => x !== c) })} />
                ))}
              </Group>
            </Table.Td>
            <Table.Td>
              <NumberInput size="xs" w={120} min={1} max={1440} suffix=" min" disabled={!isAdmin} defaultValue={r.ack_minutes}
                onBlur={(e) => { const v = Number(e.currentTarget.value.replace(/\D/g, "")); if (v && v !== r.ack_minutes) save.mutate({ ...r, ack_minutes: v }); }} />
            </Table.Td>
          </Table.Tr>
        ))}
      </Table.Tbody>
    </Table>
  );
}
