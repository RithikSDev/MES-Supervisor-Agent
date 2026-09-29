import {
  ActionIcon, Alert, Badge, Button, Group, List, Loader, Modal, Select, SimpleGrid, Stack, Switch, Table, Text, TextInput,
  UnstyledButton,
} from "@mantine/core";
import { coreRaw, itsm } from "@mes/api-client";
import {
  IconAlertTriangle, IconArrowsSort, IconCheck, IconChevronDown, IconChevronUp, IconDownload, IconLifebuoy, IconPencil,
  IconPlus, IconRefresh, IconSearch, IconShieldCheck, IconUserOff, IconUsers, IconX, type Icon,
} from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useCan } from "../auth";
import { PageHeader } from "../components/Layout";
import { usePlant } from "../components/plant";
import { type Cell, csvName, downloadCsv } from "../lib/csv";
import type { SupportMember } from "./TicketsPage";

type SnState = {
  username: string; name: string; group: string | null; status: "exists" | "created" | "missing" | "error";
  user_name?: string | null; itil?: boolean | null; in_group?: boolean | null; fixed?: string[]; error?: string;
};

const TIER_COLOR: Record<string, string> = { L1: "navy.6", L2: "teal", L3: "blue" };

/** What stops an engineer from being picked as "Assigned to" in ServiceNow (empty = ready). */
function problems(s: SnState): string[] {
  if (s.status === "error") return [s.error ?? "error"];
  if (s.status === "missing") return ["no ServiceNow user with this e-mail or user name"];
  return [...(s.itil === false ? ["no itil role"] : []), ...(s.in_group === false ? [`not in group ${s.group}`] : [])];
}

function SnCell({ s }: { s?: SnState }) {
  if (!s) return <Text size="xs" c="dimmed">not checked</Text>;
  const p = problems(s);
  if (!p.length) {
    return (
      <Group gap={6} wrap="nowrap">
        <IconCheck size={15} color="var(--mes-green)" />
        <div>
          <Text size="xs">ready{s.status === "created" ? " (created)" : s.fixed?.length ? ` (fixed: ${s.fixed.join(", ")})` : ""}</Text>
          <Text size="10px" c="dimmed">{s.user_name}{s.group ? ` · ${s.group}` : ""}</Text>
        </div>
      </Group>
    );
  }
  return (
    <Group gap={6} wrap="nowrap" align="flex-start">
      {s.status === "error" ? <IconX size={15} color="var(--mes-red)" /> : <IconAlertTriangle size={15} color="#b37f00" />}
      <Text size="xs" c={s.status === "error" ? "red.7" : "amber.8"}>{p.join("; ")}</Text>
    </Group>
  );
}

/** Clickable KPI tile: icon + colour-accented top border + hover lift (same look as Work Orders/Maintenance/Quality). */
function TeamKpi({
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

/** Quiet placeholder for a table with nothing in it yet. */
function EmptyState({ icon: Icon, title, sub }: { icon: Icon; title: string; sub?: string }) {
  return (
    <div className="panel" style={{ padding: "36px 20px", textAlign: "center" }}>
      <div style={{
        width: 40, height: 40, borderRadius: 10, background: "var(--mes-panel-2)", display: "grid", placeItems: "center",
        margin: "0 auto 10px", color: "var(--mes-text-dim)",
      }}>
        <Icon size={20} stroke={1.6} />
      </div>
      <Text fw={600} size="sm">{title}</Text>
      {sub && <Text size="xs" c="dimmed" mt={4}>{sub}</Text>}
    </div>
  );
}

/** Generic sortable column header; undecorated until a sort is picked, preserving the backend's own order. */
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
type MemberSortKey = "name" | "username" | "tier" | "open";
const MEMBER_SORT_VALUE: Record<MemberSortKey, (m: SupportMember) => number | string> = {
  name: (m) => m.display_name,
  username: (m) => m.username,
  tier: (m) => m.tier ?? "zzz",
  open: (m) => m.open_tickets,
};
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

type Draft = { id: string | null; display_name: string; username: string; email: string; tier: string | null; active: boolean };

function MemberModal({ draft, plantId, onClose }: { draft: Draft; plantId: string; onClose: () => void }) {
  const qc = useQueryClient();
  const [d, setD] = useState(draft);
  const save = useMutation({
    mutationFn: () => coreRaw<SupportMember>(d.id ? `/tickets/support-team/${d.id}` : "/tickets/support-team", {
      method: d.id ? "PUT" : "POST",
      body: JSON.stringify({ plant_id: plantId, display_name: d.display_name, username: d.username, email: d.email || null,
        tier: d.tier, active: d.active }),
    }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["support-team"] }); onClose(); },
    meta: { silent: true },
  });
  // suggest a user name from the e-mail for new engineers
  const setEmail = (email: string) => setD((x) => ({ ...x, email,
    username: !x.id && (!x.username || x.username === x.email.split("@")[0]) ? email.split("@")[0] : x.username }));
  return (
    <Modal opened onClose={onClose} centered title={<Text fw={700}>{d.id ? `Edit ${draft.display_name}` : "Add support engineer"}</Text>}>
      <Stack>
        <TextInput label="Name" required value={d.display_name} data-autofocus placeholder="e.g. Ayyappan Veeraiyan"
          onChange={(e) => { const v = e.currentTarget.value; setD((x) => ({ ...x, display_name: v })); }} />
        <TextInput label="E-mail" value={d.email} placeholder="the e-mail of their ServiceNow user"
          description="ServiceNow's Assigned to is matched by this e-mail, then by the user name"
          onChange={(e) => setEmail(e.currentTarget.value)} />
        <Group grow align="flex-start">
          <TextInput label="User name" required value={d.username} description="their ServiceNow / MES user ID"
            onChange={(e) => { const v = e.currentTarget.value; setD((x) => ({ ...x, username: v })); }} />
          <Select label="Usual tier" value={d.tier} clearable placeholder="any tier" description="decides their ServiceNow group"
            data={[{ value: "L1", label: "L1 · first line" }, { value: "L2", label: "L2 · technical" }, { value: "L3", label: "L3 · development" }]}
            onChange={(v) => setD((x) => ({ ...x, tier: v }))} />
        </Group>
        {d.id && <Switch label="On the team (off: no new tickets; assigned tickets stay with them)" checked={d.active}
          onChange={(e) => { const v = e.currentTarget.checked; setD((x) => ({ ...x, active: v })); }} />}
        {save.error && <Text size="sm" c="red.7">{save.error.message}</Text>}
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button loading={save.isPending} disabled={!d.display_name.trim() || !d.username.trim()} onClick={() => save.mutate()}>
            {d.id ? "Save" : "Add to the team"}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

export function SupportTeamPage() {
  const { plant } = usePlant();
  const canEdit = useCan("support:team");
  const [showInactive, setShowInactive] = useState(false);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [fixOpen, setFixOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<{ key: MemberSortKey; dir: 1 | -1 } | null>(null);
  const team = useQuery({
    queryKey: ["support-team", plant?.id, "all"],
    queryFn: () => coreRaw<SupportMember[]>(`/tickets/support-team?plant_id=${plant!.id}&include_inactive=true`),
    enabled: Boolean(plant),
  });
  const rows = team.data ?? [];
  const active = rows.filter((m) => m.active);
  const inactive = rows.filter((m) => !m.active);
  const openTotal = active.reduce((s, m) => s + m.open_tickets, 0);
  const check = useMutation({
    mutationFn: (create: boolean) => itsm<{ users: SnState[] }>("/api/v1/itsm/servicenow/users", {
      method: "POST", body: JSON.stringify({ create, users: active.map((m) => ({ username: m.username, name: m.display_name,
        email: m.email, tier: m.tier })) }) }),
    onSuccess: () => setFixOpen(false),
    meta: { silent: true },
  });
  const sn = Object.fromEntries((check.data?.users ?? []).map((u) => [u.username, u]));
  const toFix = (check.data?.users ?? []).filter((u) => problems(u).length && u.status !== "error");

  const shown = useMemo(() => {
    const base = rows.filter((m) => showInactive || m.active);
    const s = search.trim().toLowerCase();
    const filtered = base.filter((m) => !s
      || m.display_name.toLowerCase().includes(s) || m.username.toLowerCase().includes(s) || (m.email ?? "").toLowerCase().includes(s));
    return sortRows(filtered, sort, MEMBER_SORT_VALUE);
  }, [rows, showInactive, search, sort]);

  const exportCsv = () => {
    const header: Cell[] = ["Name", "User name", "E-mail", "Tier", "Active", "Open tickets", "ServiceNow"];
    const body: Cell[][] = shown.map((m) => [m.display_name, m.username, m.email, m.tier, m.active,
      m.open_tickets, sn[m.username] ? (problems(sn[m.username]).join("; ") || "ready") : "not checked"]);
    downloadCsv(csvName("support-team"), [header, ...body]);
  };

  const snTileAction = () => {
    if (!check.data) { check.mutate(false); return; }
    if (canEdit && toFix.length) setFixOpen(true);
  };

  return (
    <>
      <PageHeader title="Support team" sub="The support engineers tickets can be assigned to, from the ticket screen or by asking the AI Support Assistant. In ServiceNow they appear as Assigned to."
        right={canEdit && <Button leftSection={<IconPlus size={14} />} onClick={() => setDraft({ id: null, display_name: "", username: "", email: "", tier: null, active: true })}>Add engineer</Button>} />

      <SimpleGrid cols={{ base: 2, md: 4 }} mb="md" className="rise">
        <TeamKpi icon={IconUsers} label="Active engineers" value={active.length} color="var(--mes-blue)"
          sub={`${["L1", "L2", "L3"].map((t) => `${t} ${active.filter((m) => m.tier === t).length}`).join(" · ")}`} />
        <TeamKpi icon={IconUserOff} label="Off the team" value={inactive.length} color="var(--mes-amber)" hot={Boolean(inactive.length)}
          sub="assigned tickets stay with them" onClick={() => setShowInactive(true)} />
        <TeamKpi icon={IconLifebuoy} label="Open tickets assigned" value={openTotal} color="var(--mes-blue)" sub="across the active team" />
        <TeamKpi icon={check.data && toFix.length ? IconAlertTriangle : IconShieldCheck} label="ServiceNow ready"
          value={check.data ? active.length - toFix.length : "?"} color={check.data && toFix.length ? "var(--mes-amber)" : "var(--mes-green)"}
          hot={Boolean(check.data && toFix.length)} sub={check.data ? `of ${active.length} · ${toFix.length ? "click to fix" : "all ready"}` : "click to check"}
          onClick={snTileAction} />
      </SimpleGrid>

      <div className="panel" style={{ padding: 14, marginBottom: 16 }}>
        <Group justify="space-between" wrap="nowrap" align="flex-start">
          <div>
            <Text fw={600}>ServiceNow</Text>
            <Text size="sm" c="dimmed" maw={760}>ServiceNow only offers a user as Assigned to when the user is active, has the
              itil role and is a member of the incident's assignment group. Check shows who is ready; Fix creates missing users
              and adds a missing itil role or group membership.</Text>
          </div>
          <Group gap={6} wrap="nowrap">
            {canEdit && toFix.length > 0 && <Button size="xs" variant="light" color="amber" onClick={() => setFixOpen(true)}>Fix in ServiceNow ({toFix.length})</Button>}
            <Button size="xs" variant="default" leftSection={<IconRefresh size={13} />} loading={check.isPending && !check.variables}
              disabled={!active.length} onClick={() => check.mutate(false)}>Check</Button>
          </Group>
        </Group>
        {check.error && <Text size="sm" c="red.7" mt={6}>{check.error.message}</Text>}
      </div>

      <Group justify="space-between" mb="sm" wrap="wrap">
        <Group gap="md">
          <Switch size="xs" label={`Show engineers off the team (${inactive.length})`} checked={showInactive}
            onChange={(e) => setShowInactive(e.currentTarget.checked)} />
          <TextInput size="xs" w={240} leftSection={<IconSearch size={14} />}
            rightSection={search ? (
              <ActionIcon variant="subtle" color="gray" size="xs" onClick={() => setSearch("")} aria-label="Clear search"><IconX size={12} /></ActionIcon>
            ) : undefined}
            placeholder="Name, user name, e-mail…" value={search} onChange={(e) => setSearch(e.currentTarget.value)} />
        </Group>
        <ActionIcon variant="light" size="input-xs" disabled={!shown.length} onClick={exportCsv} aria-label="Download CSV"><IconDownload size={14} /></ActionIcon>
      </Group>
      {team.isPending ? <Loader size="sm" /> : rows.length === 0 ? (
        <EmptyState icon={IconUsers} title="No engineers yet" sub="Add the support engineers tickets can be assigned to." />
      ) : (
        <Table className="panel" highlightOnHover>
          <Table.Thead>
            <Table.Tr>
              <SortTh id="name" sort={sort} onSort={(k) => toggleSort(setSort, k)}>Engineer</SortTh>
              <SortTh id="username" sort={sort} onSort={(k) => toggleSort(setSort, k)}>User name</SortTh>
              <SortTh id="tier" sort={sort} onSort={(k) => toggleSort(setSort, k)}>Tier</SortTh>
              <SortTh id="open" align="right" sort={sort} onSort={(k) => toggleSort(setSort, k)}>Open tickets</SortTh>
              <Table.Th>ServiceNow</Table.Th>
              <Table.Th />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {shown.map((m) => (
              <Table.Tr key={m.id} style={{ opacity: m.active ? 1 : 0.55 }}>
                <Table.Td><Text size="sm" fw={500}>{m.display_name}</Text><Text size="xs" c="dimmed">{m.email ?? "no e-mail"}</Text></Table.Td>
                <Table.Td className="mono"><Text size="sm">{m.username}</Text></Table.Td>
                <Table.Td>{m.tier ? <Badge size="sm" variant="outline" color={TIER_COLOR[m.tier]}>{m.tier}</Badge> : <Text size="xs" c="dimmed">any</Text>}</Table.Td>
                <Table.Td ta="right" className="mono">{m.open_tickets}</Table.Td>
                <Table.Td>{m.active ? <SnCell s={sn[m.username]} /> : <Text size="xs" c="dimmed">off the team</Text>}</Table.Td>
                <Table.Td ta="right">{canEdit && (
                  <Button size="compact-xs" variant="subtle" leftSection={<IconPencil size={13} />}
                    onClick={() => setDraft({ id: m.id, display_name: m.display_name, username: m.username, email: m.email ?? "", tier: m.tier, active: m.active })}>
                    Edit
                  </Button>
                )}</Table.Td>
              </Table.Tr>
            ))}
            {shown.length === 0 && <Table.Tr><Table.Td colSpan={6}><Text size="sm" c="dimmed">No engineers match your search.</Text></Table.Td></Table.Tr>}
          </Table.Tbody>
        </Table>
      )}

      {draft && plant && <MemberModal draft={draft} plantId={plant.id} onClose={() => setDraft(null)} />}
      <Modal opened={fixOpen} onClose={() => setFixOpen(false)} centered title={<Text fw={700}>Fix in ServiceNow</Text>}>
        <Stack>
          <Text size="sm">These changes are made in your ServiceNow instance:</Text>
          <List size="sm" spacing={4}>
            {toFix.map((u) => (
              <List.Item key={u.username}>
                <b>{u.name}</b>: {u.status === "missing" ? `create user ${u.username} with role itil${u.group ? `, member of ${u.group}` : ""}`
                  : [u.itil === false && "add role itil", u.in_group === false && `add to ${u.group}`].filter(Boolean).join(", ")}
              </List.Item>
            ))}
          </List>
          {toFix.some((u) => u.status === "missing") && (
            <Alert color="blue" p="xs"><Text size="xs">New users get no password: they sign in the way your instance is set up
              (SSO, or a password an admin sets).</Text></Alert>
          )}
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setFixOpen(false)}>Cancel</Button>
            <Button loading={check.isPending && check.variables} onClick={() => check.mutate(true)}>Make the changes</Button>
          </Group>
        </Stack>
      </Modal>
    </>
  );
}
