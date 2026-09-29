import {
  ActionIcon, Alert, Anchor, Badge, Button, Code, CopyButton, Group, Modal, PasswordInput, SimpleGrid, Stack, Switch,
  Table, Tabs, Text, TextInput, UnstyledButton,
} from "@mantine/core";
import { type OperatorRow, type TerminalAdmin, core, unwrap } from "@mes/api-client";
import {
  IconArrowsSort, IconChevronDown, IconChevronUp, IconDeviceTablet, IconDeviceTabletOff, IconDownload,
  IconExternalLink, IconKey, IconLock, IconLockOpen, IconSearch, IconTool, IconUserCheck, IconUserPlus, IconUsers,
  IconX, type Icon,
} from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useCan } from "../auth";
import { PageHeader } from "../components/Layout";
import { usePlant } from "../components/plant";
import { type Cell, csvName, downloadCsv } from "../lib/csv";
import { fmtDateTime, fromNow } from "../lib/format";

const TERMINAL_URL = import.meta.env.VITE_TERMINAL_URL ?? "http://localhost:3081";

/** Clickable KPI tile: icon + colour-accented top border + hover lift (same look as Work Orders/Maintenance/Quality). */
function AdminKpi({
  icon: Icon, label, value, color, hot, selected, onClick,
}: { icon: Icon; label: string; value: number; color: string; hot?: boolean; selected?: boolean; onClick: () => void }) {
  return (
    <div className={`panel kpi wo-kpi${selected ? " selected" : ""}`} style={{ borderTop: `3px solid ${color}` }}
      onClick={onClick} role="button" tabIndex={0} onKeyDown={(e) => e.key === "Enter" && onClick()}>
      <div className="wo-kpi-top"><span className="wo-kpi-icon" style={{ background: color }}><Icon size={13} stroke={2.2} /></span></div>
      <div className="label">{label}</div>
      <div className="value" style={{ color: hot ? color : undefined }}>{value}</div>
    </div>
  );
}

/** Quiet placeholder for a table with nothing in it (yet, or after a filter), instead of an empty box. */
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
      {sub && <Text size="xs" c="dimmed" mt={4} maw={440} mx="auto">{sub}</Text>}
    </div>
  );
}

/** Generic sortable column header, shared by the terminals and operators tables below. */
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

const isLocked = (t: TerminalAdmin) => Boolean(t.pin_locked_until && new Date(t.pin_locked_until) > new Date());

type TermSortKey = "terminal" | "line" | "enrolled" | "session" | "pin";
const TERM_SORT_VALUE: Record<TermSortKey, (t: TerminalAdmin) => number | string> = {
  terminal: (t) => t.code,
  line: (t) => t.line_code,
  enrolled: (t) => (t.enrolled ? 1 : 0),
  session: (t) => t.session_operator ?? "",
  pin: (t) => (isLocked(t) ? 1 : 0),
};
type TermFilter = "all" | "enrolled" | "not_enrolled" | "logged_in" | "locked";

type OpSortKey = "person" | "account" | "pin";
const OP_SORT_VALUE: Record<OpSortKey, (o: OperatorRow) => number | string> = {
  person: (o) => o.display_name ?? o.username,
  account: (o) => (o.sso_linked ? 1 : 0),
  pin: (o) => (o.has_pin ? 1 : 0),
};
type OpFilter = "all" | "has_pin" | "no_pin" | "technician";

export function TerminalsPage() {
  const { plant } = usePlant();
  const qc = useQueryClient();
  const canTerminals = useCan("terminal:admin");
  const canOperators = useCan("operator:admin");
  const [enrolled, setEnrolled] = useState<{ terminal: TerminalAdmin; token: string } | null>(null);
  const [pinFor, setPinFor] = useState<OperatorRow | null>(null);
  const [adding, setAdding] = useState(false);
  const pid = plant?.id;

  const terminals = useQuery({
    queryKey: ["terminals", pid],
    queryFn: () => unwrap(core.GET("/api/v1/terminals", { params: { query: { plant_id: pid! } } })),
    enabled: Boolean(pid) && canTerminals,
    refetchInterval: 10_000,
  });
  const operators = useQuery({
    queryKey: ["operators", pid],
    queryFn: () => unwrap(core.GET("/api/v1/operators", { params: { query: { plant_id: pid! } } })),
    enabled: Boolean(pid) && canOperators,
  });
  const enroll = useMutation({
    mutationFn: (t: TerminalAdmin) => unwrap(core.POST("/api/v1/terminals/{terminal_id}/enroll", { params: { path: { terminal_id: t.id } } })),
    onSuccess: (r, t) => {
      setEnrolled({ terminal: t, token: r.device_token });
      qc.invalidateQueries({ queryKey: ["terminals", pid] });
    },
  });
  const revoke = useMutation({
    mutationFn: (t: TerminalAdmin) => unwrap(core.POST("/api/v1/terminals/{terminal_id}/revoke", { params: { path: { terminal_id: t.id } } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["terminals", pid] }),
  });

  const [termFilter, setTermFilter] = useState<TermFilter>("all");
  const [termSearch, setTermSearch] = useState("");
  const [termSort, setTermSort] = useState<{ key: TermSortKey; dir: 1 | -1 } | null>(null);
  const termRows = useMemo(() => {
    const s = termSearch.trim().toLowerCase();
    const filtered = (terminals.data ?? []).filter((t) => {
      if (termFilter === "enrolled" && !t.enrolled) return false;
      if (termFilter === "not_enrolled" && t.enrolled) return false;
      if (termFilter === "logged_in" && !t.session_operator) return false;
      if (termFilter === "locked" && !isLocked(t)) return false;
      return !s || t.code.toLowerCase().includes(s) || t.name.toLowerCase().includes(s) || t.line_code.toLowerCase().includes(s);
    });
    return sortRows(filtered, termSort, TERM_SORT_VALUE);
  }, [terminals.data, termFilter, termSearch, termSort]);
  const termCounts = useMemo(() => {
    const all = terminals.data ?? [];
    return {
      enrolled: all.filter((t) => t.enrolled).length,
      not_enrolled: all.filter((t) => !t.enrolled).length,
      logged_in: all.filter((t) => t.session_operator).length,
      locked: all.filter(isLocked).length,
    };
  }, [terminals.data]);
  const exportTerminals = () => {
    const header: Cell[] = ["Terminal", "Name", "Line", "Enrolled", "Enrolled at", "Enrolled by", "Logged in", "Session started", "PIN locked"];
    const body: Cell[][] = termRows.map((t) => [t.code, t.name, t.line_code, t.enrolled, t.enrolled_at, t.enrolled_by,
      t.session_operator, t.session_started_at, isLocked(t)]);
    downloadCsv(csvName("terminals"), [header, ...body]);
  };

  const [opFilter, setOpFilter] = useState<OpFilter>("all");
  const [opSearch, setOpSearch] = useState("");
  const [opSort, setOpSort] = useState<{ key: OpSortKey; dir: 1 | -1 } | null>(null);
  const opRows = useMemo(() => {
    const s = opSearch.trim().toLowerCase();
    const filtered = (operators.data ?? []).filter((o) => {
      if (opFilter === "has_pin" && !o.has_pin) return false;
      if (opFilter === "no_pin" && o.has_pin) return false;
      if (opFilter === "technician" && !o.technician) return false;
      return !s || o.username.toLowerCase().includes(s) || (o.display_name ?? "").toLowerCase().includes(s);
    });
    return sortRows(filtered, opSort, OP_SORT_VALUE);
  }, [operators.data, opFilter, opSearch, opSort]);
  const opCounts = useMemo(() => {
    const all = operators.data ?? [];
    return {
      has_pin: all.filter((o) => o.has_pin).length,
      no_pin: all.filter((o) => !o.has_pin).length,
      technician: all.filter((o) => o.technician).length,
    };
  }, [operators.data]);
  const exportOperators = () => {
    const header: Cell[] = ["Name", "Username", "Keycloak linked", "PIN set", "PIN set at", "Technician"];
    const body: Cell[][] = opRows.map((o) => [o.display_name ?? "", o.username, o.sso_linked, o.has_pin, o.pin_set_at, o.technician]);
    downloadCsv(csvName("operators"), [header, ...body]);
  };

  return (
    <>
      <PageHeader title="Terminals & operators" sub="Shop-floor devices and the people who log in on them with a PIN" />
      <Tabs defaultValue={canTerminals ? "terminals" : "operators"}>
        <Tabs.List>
          {canTerminals && <Tabs.Tab value="terminals">Terminals</Tabs.Tab>}
          {canOperators && <Tabs.Tab value="operators">Operators & PINs</Tabs.Tab>}
        </Tabs.List>

        <Tabs.Panel value="terminals" pt="sm">
          <SimpleGrid cols={{ base: 2, md: 4 }} mb="md" className="rise">
            <AdminKpi icon={IconDeviceTablet} label="Enrolled" value={termCounts.enrolled} color="var(--mes-green)"
              selected={termFilter === "enrolled"} onClick={() => setTermFilter((f) => (f === "enrolled" ? "all" : "enrolled"))} />
            <AdminKpi icon={IconDeviceTabletOff} label="Not enrolled" value={termCounts.not_enrolled} color="var(--mes-amber)"
              hot={termCounts.not_enrolled > 0} selected={termFilter === "not_enrolled"}
              onClick={() => setTermFilter((f) => (f === "not_enrolled" ? "all" : "not_enrolled"))} />
            <AdminKpi icon={IconUsers} label="Logged in now" value={termCounts.logged_in} color="var(--mes-blue)"
              selected={termFilter === "logged_in"} onClick={() => setTermFilter((f) => (f === "logged_in" ? "all" : "logged_in"))} />
            <AdminKpi icon={IconLock} label="PIN locked" value={termCounts.locked} color="var(--mes-red)"
              hot={termCounts.locked > 0} selected={termFilter === "locked"} onClick={() => setTermFilter((f) => (f === "locked" ? "all" : "locked"))} />
          </SimpleGrid>
          <Group justify="space-between" mb="xs" wrap="wrap">
            <TextInput size="xs" w={280} leftSection={<IconSearch size={14} />}
              rightSection={termSearch ? (
                <ActionIcon variant="subtle" color="gray" size="xs" onClick={() => setTermSearch("")} aria-label="Clear search"><IconX size={12} /></ActionIcon>
              ) : undefined}
              placeholder="Terminal, name, line…" value={termSearch} onChange={(e) => setTermSearch(e.currentTarget.value)} />
            <ActionIcon variant="light" size="input-xs" disabled={!termRows.length} onClick={exportTerminals} aria-label="Download CSV"><IconDownload size={14} /></ActionIcon>
          </Group>
          <Table className="panel" highlightOnHover>
            <Table.Thead>
              <Table.Tr>
                <SortTh id="terminal" sort={termSort} onSort={(k) => toggleSort(setTermSort, k)}>Terminal</SortTh>
                <SortTh id="line" sort={termSort} onSort={(k) => toggleSort(setTermSort, k)}>Line</SortTh>
                <SortTh id="enrolled" sort={termSort} onSort={(k) => toggleSort(setTermSort, k)}>Device</SortTh>
                <SortTh id="session" sort={termSort} onSort={(k) => toggleSort(setTermSort, k)}>Logged in</SortTh>
                <SortTh id="pin" sort={termSort} onSort={(k) => toggleSort(setTermSort, k)}>PIN entry</SortTh>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {termRows.map((t) => (
                <Table.Tr key={t.id}>
                  <Table.Td><span className="mono">{t.code}</span> <Text span size="xs" c="dimmed">{t.name}</Text></Table.Td>
                  <Table.Td className="mono">{t.line_code}</Table.Td>
                  <Table.Td>
                    {t.enrolled ? (
                      <Text size="xs">enrolled {fromNow(t.enrolled_at)} by {t.enrolled_by}</Text>
                    ) : (
                      <Badge size="sm" color="navy.6" variant="outline">not enrolled</Badge>
                    )}
                  </Table.Td>
                  <Table.Td>{t.session_operator ? <Text size="sm">{t.session_operator} <Text span size="xs" c="dimmed">since {fmtDateTime(t.session_started_at)}</Text></Text> : <Text size="xs" c="dimmed">—</Text>}</Table.Td>
                  <Table.Td>{isLocked(t) ? <Badge color="amber">locked</Badge> : <Text size="xs" c="dimmed">ok</Text>}</Table.Td>
                  <Table.Td ta="right">
                    <Group gap={6} justify="flex-end">
                      <Button size="compact-xs" leftSection={<IconDeviceTablet size={12} />} loading={enroll.isPending && enroll.variables?.id === t.id} onClick={() => enroll.mutate(t)}>
                        {t.enrolled ? "Re-enrol" : "Enrol device"}
                      </Button>
                      {t.enrolled && <Button size="compact-xs" variant="subtle" color="red" onClick={() => revoke.mutate(t)}>Revoke</Button>}
                    </Group>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
          {terminals.data?.length === 0 && <EmptyState icon={IconDeviceTablet} title="No terminals yet" sub="Terminals are created by seeding or setup; once added they appear here to enrol." />}
          {Boolean(terminals.data?.length) && termRows.length === 0 && <Text c="dimmed" size="sm" ta="center" py="md">No terminals match this filter.</Text>}
        </Tabs.Panel>

        <Tabs.Panel value="operators" pt="sm">
          <SimpleGrid cols={{ base: 2, md: 4 }} mb="md" className="rise">
            <AdminKpi icon={IconUsers} label="Operators" value={operators.data?.length ?? 0} color="var(--mes-navy)"
              selected={opFilter === "all"} onClick={() => setOpFilter("all")} />
            <AdminKpi icon={IconUserCheck} label="PIN set" value={opCounts.has_pin} color="var(--mes-green)"
              selected={opFilter === "has_pin"} onClick={() => setOpFilter((f) => (f === "has_pin" ? "all" : "has_pin"))} />
            <AdminKpi icon={IconLockOpen} label="No PIN" value={opCounts.no_pin} color="var(--mes-amber)"
              hot={opCounts.no_pin > 0} selected={opFilter === "no_pin"} onClick={() => setOpFilter((f) => (f === "no_pin" ? "all" : "no_pin"))} />
            <AdminKpi icon={IconTool} label="Technicians" value={opCounts.technician} color="var(--mes-amber)"
              selected={opFilter === "technician"} onClick={() => setOpFilter((f) => (f === "technician" ? "all" : "technician"))} />
          </SimpleGrid>
          <Group justify="space-between" mb="xs" wrap="wrap">
            <TextInput size="xs" w={280} leftSection={<IconSearch size={14} />}
              rightSection={opSearch ? (
                <ActionIcon variant="subtle" color="gray" size="xs" onClick={() => setOpSearch("")} aria-label="Clear search"><IconX size={12} /></ActionIcon>
              ) : undefined}
              placeholder="Name, username…" value={opSearch} onChange={(e) => setOpSearch(e.currentTarget.value)} />
            <Group gap={8}>
              <ActionIcon variant="light" size="input-xs" disabled={!opRows.length} onClick={exportOperators} aria-label="Download CSV"><IconDownload size={14} /></ActionIcon>
              <Button leftSection={<IconUserPlus size={14} />} onClick={() => setAdding(true)}>Add shop-floor operator</Button>
            </Group>
          </Group>
          <Table className="panel" highlightOnHover>
            <Table.Thead>
              <Table.Tr>
                <SortTh id="person" sort={opSort} onSort={(k) => toggleSort(setOpSort, k)}>Person</SortTh>
                <SortTh id="account" sort={opSort} onSort={(k) => toggleSort(setOpSort, k)}>Account</SortTh>
                <SortTh id="pin" sort={opSort} onSort={(k) => toggleSort(setOpSort, k)}>PIN</SortTh>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {opRows.map((o) => (
                <Table.Tr key={o.id}>
                  <Table.Td>{o.display_name ?? o.username} <Text span size="xs" c="dimmed" className="mono">{o.username}</Text></Table.Td>
                  <Table.Td>{o.sso_linked ? <Badge size="xs" variant="outline" color="navy.6">Keycloak</Badge> : <Badge size="xs" variant="outline" color="navy.5">shop floor only</Badge>}</Table.Td>
                  <Table.Td>{o.has_pin ? <Text size="xs">set {fromNow(o.pin_set_at)}{o.technician && <Badge ml={6} size="xs" color="amber" variant="light">technician</Badge>}</Text> : <Text size="xs" c="dimmed">none</Text>}</Table.Td>
                  <Table.Td ta="right"><Button size="compact-xs" variant="light" leftSection={<IconKey size={12} />} onClick={() => setPinFor(o)}>{o.has_pin ? "Reset PIN" : "Set PIN"}</Button></Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
          {operators.data?.length === 0 && <EmptyState icon={IconUsers} title="No shop-floor operators yet" sub="Add one to let them log in on a terminal with a PIN." />}
          {Boolean(operators.data?.length) && opRows.length === 0 && <Text c="dimmed" size="sm" ta="center" py="md">No operators match this filter.</Text>}
        </Tabs.Panel>
      </Tabs>

      <Modal opened={Boolean(enrolled)} onClose={() => setEnrolled(null)} centered size="lg" title={<Text fw={600}>Enrol {enrolled?.terminal.code}</Text>}>
        {enrolled && (
          <Stack>
            <Text size="sm">Open this link <b>on the terminal device</b>. It is shown only once, and it replaces any device enrolled before.</Text>
            <Code block style={{ wordBreak: "break-all" }}>{`${TERMINAL_URL}/#enroll=${enrolled.token}`}</Code>
            <Group>
              <CopyButton value={`${TERMINAL_URL}/#enroll=${enrolled.token}`}>
                {({ copied, copy }) => <Button variant="light" onClick={copy}>{copied ? "Copied" : "Copy link"}</Button>}
              </CopyButton>
              <Anchor href={`${TERMINAL_URL}/#enroll=${enrolled.token}`} target="_blank" rel="noreferrer">
                <Group gap={4}><IconExternalLink size={14} />Open terminal here (demo)</Group>
              </Anchor>
            </Group>
            <Alert color="navy" variant="outline">Demo PINs: operator1 1111 · operator2 2222 · operator3 3333 · supervisor1 4444</Alert>
          </Stack>
        )}
      </Modal>
      {pinFor && <PinModal op={pinFor} onClose={() => setPinFor(null)} />}
      {adding && <AddOperatorModal onClose={() => { setAdding(false); qc.invalidateQueries({ queryKey: ["operators", pid] }); }} />}
    </>
  );
}

function PinModal({ op, onClose }: { op: OperatorRow; onClose: () => void }) {
  const { plant } = usePlant();
  const qc = useQueryClient();
  const [pin, setPin] = useState("");
  const [technician, setTechnician] = useState(Boolean(op.technician));
  const m = useMutation({
    mutationFn: () => unwrap(core.PUT("/api/v1/operators/{user_id}/pin", { params: { path: { user_id: op.id } }, body: { plant_id: plant!.id, pin, technician } })),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["operators", plant?.id] });
      onClose();
    },
  });
  return (
    <Modal opened onClose={onClose} centered title={<Text fw={600}>PIN for {op.display_name ?? op.username}</Text>}>
      <Stack>
        <PasswordInput label="New PIN (4-6 digits)" value={pin} onChange={(e) => setPin(e.currentTarget.value.replace(/\D/g, "").slice(0, 6))} data-autofocus />
        <Switch label="Maintenance technician on terminals (technician mode: maintenance orders, spare parts, sign-off)" checked={technician}
          onChange={(e) => setTechnician(e.currentTarget.checked)} />
        <Text size="xs" c="dimmed">Stored as a salted hash; the audit log records that it changed, not the value.</Text>
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button loading={m.isPending} disabled={pin.length < 4} onClick={() => m.mutate()}>Save PIN</Button>
        </Group>
      </Stack>
    </Modal>
  );
}

function AddOperatorModal({ onClose }: { onClose: () => void }) {
  const { plant } = usePlant();
  const [username, setUsername] = useState("");
  const [name, setName] = useState("");
  const [pin, setPin] = useState("");
  const m = useMutation({
    mutationFn: () => unwrap(core.POST("/api/v1/operators", { body: { plant_id: plant!.id, username, display_name: name, pin } })),
    onSuccess: onClose,
  });
  return (
    <Modal opened onClose={onClose} centered title={<Text fw={600}>Add shop-floor operator</Text>}>
      <Stack>
        <TextInput label="Name" value={name} onChange={(e) => setName(e.currentTarget.value)} data-autofocus />
        <TextInput label="Username" description="lowercase, digits, . _ -" value={username} onChange={(e) => setUsername(e.currentTarget.value.toLowerCase())} />
        <PasswordInput label="PIN (4-6 digits)" value={pin} onChange={(e) => setPin(e.currentTarget.value.replace(/\D/g, "").slice(0, 6))} />
        <Text size="xs" c="dimmed">No Keycloak account is created: this person can only log in on terminals of this plant.</Text>
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button loading={m.isPending} disabled={!name || username.length < 3 || pin.length < 4} onClick={() => m.mutate()}>Add</Button>
        </Group>
      </Stack>
    </Modal>
  );
}
