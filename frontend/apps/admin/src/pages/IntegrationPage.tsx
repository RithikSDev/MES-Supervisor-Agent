import {
  ActionIcon, Alert, Badge, Button, Code, Drawer, Group, SegmentedControl, SimpleGrid, Stack, Table, Tabs, Text,
  TextInput, Timeline, UnstyledButton,
} from "@mantine/core";
import {
  IconAlertHexagon, IconArrowsSort, IconChevronDown, IconChevronUp, IconCircleCheck, IconDownload, IconPlugConnected,
  IconRefresh, IconRotateClockwise, IconSearch, IconX, type Icon,
} from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { erp } from "@mes/api-client";
import { useCan } from "../auth";
import { OutboundStatus } from "../components/badges";
import { ItsmPanel } from "../components/ItsmPanel";
import { McpServers } from "../components/McpServers";
import { PageHeader } from "../components/Layout";
import { type Cell, csvName, downloadCsv } from "../lib/csv";
import { fmtDateTime, fmtTime, fromNow } from "../lib/format";

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

type Outbound = {
  id: string;
  message_type: string;
  erp_key: string;
  status: string;
  attempts: number;
  max_attempts: number;
  next_attempt_at: string;
  last_error: string | null;
  erp_result: { erp_confirmation_group?: string; erp_confirmation_count?: string; erp_material_document?: string; erp_document_year?: string } | null;
  reconciled: boolean;
  payload: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
};
type Attempt = { attempt_no: number; started_at: string; finished_at: string; outcome: string; error: string | null };
type Inbound = { id: string; erp_object: string; erp_key: string; erp_changed_at: string | null; received_at: string; payload: Record<string, unknown> };
type PollState = { name: string; watermark: string; last_run_at: string | null; last_success_at: string | null; last_error: string | null; last_published: number };

const OUTCOME_COLOR: Record<string, string> = { POSTED: "green", RECONCILED: "amber", RETRYABLE: "orange", AMBIGUOUS: "amber", PERMANENT: "red" };
const FILTERS: Record<string, string[] | undefined> = {
  all: undefined,
  open: ["PENDING", "SENDING", "RETRY_WAIT"],
  failed: ["FAILED", "DEAD"],
  posted: ["POSTED"],
};

type OutSortKey = "created" | "status" | "attempts";
const OUT_SORT_VALUE: Record<OutSortKey, (m: Outbound) => number | string> = {
  created: (m) => m.created_at,
  status: (m) => m.status,
  attempts: (m) => m.attempts,
};
const outRefText = (m: Outbound) => (m.message_type === "GOODS_MOVEMENT"
  ? `${String(m.payload.material ?? "")} ${m.payload.erp_order_number ? String(m.payload.erp_order_number) : ""}`
  : `${String(m.payload.erp_order_number ?? "")} ${String(m.payload.op_no ?? "")}`);

export function IntegrationPage() {
  const qc = useQueryClient();
  const canRetry = useCan("schedule:edit"); // supervisors/admins operate the integration
  const [filter, setFilter] = useState("all");
  const [openId, setOpenId] = useState<string | null>(null);
  const [outSearch, setOutSearch] = useState("");
  const [outSort, setOutSort] = useState<{ key: OutSortKey; dir: 1 | -1 } | null>(null);
  const [inSearch, setInSearch] = useState("");

  const stats = useQuery({ queryKey: ["erp", "stats"], queryFn: () => erp<Record<string, number>>("/api/v1/outbound-stats"), refetchInterval: 3000 });
  const outbound = useQuery({
    queryKey: ["erp", "outbound", filter],
    queryFn: () => {
      const qs = new URLSearchParams({ limit: "200" });
      FILTERS[filter]?.forEach((s) => qs.append("status", s));
      return erp<Outbound[]>(`/api/v1/outbound?${qs}`);
    },
    refetchInterval: 3000,
  });
  const outRows = useMemo(() => {
    const s = outSearch.trim().toLowerCase();
    const filtered = (outbound.data ?? []).filter((m) => !s
      || outRefText(m).toLowerCase().includes(s) || m.message_type.toLowerCase().includes(s) || m.status.toLowerCase().includes(s));
    return sortRows(filtered, outSort, OUT_SORT_VALUE);
  }, [outbound.data, outSearch, outSort]);
  const exportOutCsv = () => {
    const header: Cell[] = ["Created", "Type", "Reference", "Status", "Attempts", "Max attempts", "SAP document", "Last error"];
    const body: Cell[][] = outRows.map((m) => [m.created_at, m.message_type, outRefText(m), m.status, m.attempts, m.max_attempts,
      m.erp_result ? `${m.erp_result.erp_confirmation_group}/${m.erp_result.erp_confirmation_count}` : "", m.last_error]);
    downloadCsv(csvName("outbound-messages"), [header, ...body]);
  };
  const inbound = useQuery({ queryKey: ["erp", "inbound"], queryFn: () => erp<Inbound[]>("/api/v1/inbound?limit=100"), refetchInterval: 5000 });
  const inRows = useMemo(() => {
    const s = inSearch.trim().toLowerCase();
    return (inbound.data ?? []).filter((m) => !s || m.erp_key.toLowerCase().includes(s));
  }, [inbound.data, inSearch]);
  const poll = useQuery({ queryKey: ["erp", "poll"], queryFn: () => erp<PollState[]>("/api/v1/poll-state"), refetchInterval: 5000 });

  const pollNow = useMutation({
    mutationFn: () => erp<{ published: number }>("/api/v1/poll", { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["erp"] }),
  });

  if (stats.isError) {
    return (
      <Alert color="red" title="ERP adapter unreachable" icon={<IconPlugConnected />}>
        {stats.error.message}. Work orders and confirmations queue up in Kafka and are processed once the adapter is back.
      </Alert>
    );
  }
  const s = stats.data ?? {};
  const failed = (s.FAILED ?? 0) + (s.DEAD ?? 0);
  const open = (s.PENDING ?? 0) + (s.SENDING ?? 0) + (s.RETRY_WAIT ?? 0);

  return (
    <>
      <PageHeader
        title="Integrations"
        sub="SAP S/4HANA via the ERP adapter (OData) · ServiceNow and Jira Cloud via the ITSM adapter (REST)"
        right={
          canRetry && (
            <Button variant="light" leftSection={<IconRefresh size={14} />} loading={pollNow.isPending} onClick={() => pollNow.mutate()}>
              Poll SAP now
            </Button>
          )
        }
      />
      <SimpleGrid cols={4} mb="md" className="rise">
        <Tile icon={IconCircleCheck} label="Posted to SAP" value={s.POSTED ?? 0} color="var(--mes-green)" />
        <Tile icon={IconRefresh} label="In flight / retrying" value={open} color="var(--mes-amber)" hot={Boolean(open)} />
        <Tile icon={IconAlertHexagon} label="Failed (needs action)" value={failed} color="var(--mes-red)" hot={Boolean(failed)} />
        <div className="panel kpi wo-kpi" style={{ borderTop: `3px solid ${poll.data?.some((p) => p.last_error) ? "var(--mes-red)" : "var(--mes-green)"}`, cursor: "default" }}>
          <div className="wo-kpi-top"><span className="wo-kpi-icon" style={{ background: poll.data?.some((p) => p.last_error) ? "var(--mes-red)" : "var(--mes-green)" }}><IconRotateClockwise size={13} stroke={2.2} /></span></div>
          <div className="label">Order poll</div>
          {(poll.data ?? []).map((p) => (
            <div key={p.name}>
              <Text size="sm" c={p.last_error ? "red.7" : undefined}>
                {p.last_error ? "failing" : "healthy"} · last run {fromNow(p.last_run_at)}
              </Text>
              <Text size="xs" c="dimmed" className="mono">watermark {fmtDateTime(p.watermark)}</Text>
              {p.last_error && <Text size="xs" c="red.7" lineClamp={2}>{p.last_error}</Text>}
            </div>
          ))}
        </div>
      </SimpleGrid>

      <Tabs defaultValue="out">
        <Tabs.List>
          <Tabs.Tab value="out">Outbound · confirmations & goods movements</Tabs.Tab>
          <Tabs.Tab value="in">Inbound · production orders</Tabs.Tab>
          <Tabs.Tab value="itsm">ServiceNow / Jira</Tabs.Tab>
          <Tabs.Tab value="mcp">AI data connections</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="mcp" pt="sm">
          <McpServers />
        </Tabs.Panel>
        <Tabs.Panel value="itsm" pt="sm">
          <ItsmPanel />
        </Tabs.Panel>
        <Tabs.Panel value="out" pt="sm">
          <Group justify="space-between" mb="xs" wrap="wrap">
            <SegmentedControl
              size="xs"
              value={filter}
              onChange={setFilter}
              data={[{ value: "all", label: "All" }, { value: "open", label: "In flight" }, { value: "failed", label: "Failed" }, { value: "posted", label: "Posted" }]}
            />
            <Group gap="xs">
              <TextInput size="xs" w={240} leftSection={<IconSearch size={14} />}
                rightSection={outSearch ? (
                  <ActionIcon variant="subtle" color="gray" size="xs" onClick={() => setOutSearch("")} aria-label="Clear search"><IconX size={12} /></ActionIcon>
                ) : undefined}
                placeholder="Order, material…" value={outSearch} onChange={(e) => setOutSearch(e.currentTarget.value)} />
              <ActionIcon variant="light" size="input-xs" disabled={!outRows.length} onClick={exportOutCsv} aria-label="Download CSV"><IconDownload size={14} /></ActionIcon>
            </Group>
          </Group>
          <Table className="panel">
            <Table.Thead>
              <Table.Tr>
                <SortTh id="created" sort={outSort} onSort={(k) => toggleSort(setOutSort, k)}>Created</SortTh>
                <Table.Th>What</Table.Th>
                <Table.Th>Reference</Table.Th>
                <Table.Th>Qty</Table.Th>
                <SortTh id="status" sort={outSort} onSort={(k) => toggleSort(setOutSort, k)}>Status</SortTh>
                <SortTh id="attempts" sort={outSort} onSort={(k) => toggleSort(setOutSort, k)}>Attempts</SortTh>
                <Table.Th>SAP document</Table.Th>
                <Table.Th>Last error</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {outRows.map((m) => (
                <Table.Tr key={m.id} onClick={() => setOpenId(m.id)} style={{ cursor: "pointer" }}>
                  <Table.Td className="mono">{fmtDateTime(m.created_at)}</Table.Td>
                  <Table.Td>
                    {m.message_type === "GOODS_MOVEMENT" ? (
                      <Text size="xs">goods movement <span className="mono">{String(m.payload.movement_type)}</span></Text>
                    ) : (
                      <Text size="xs">confirmation</Text>
                    )}
                  </Table.Td>
                  <Table.Td className="mono">
                    {m.message_type === "GOODS_MOVEMENT"
                      ? `${m.payload.material ? String(m.payload.material) : "—"}${m.payload.erp_order_number ? ` · ${String(m.payload.erp_order_number)}` : ""}`
                      : `${m.payload.erp_order_number ? String(m.payload.erp_order_number) : "—"} / ${m.payload.op_no ? String(m.payload.op_no) : "—"}`}
                  </Table.Td>
                  <Table.Td className="mono">
                    {m.message_type === "GOODS_MOVEMENT"
                      ? `${m.payload.quantity != null ? String(m.payload.quantity) : "—"} ${m.payload.uom ? String(m.payload.uom) : ""}`
                      : `${m.payload.yield_qty != null ? String(m.payload.yield_qty) : "—"}${m.payload.final ? " (final)" : ""}`}
                  </Table.Td>
                  <Table.Td>
                    <Group gap={4}>
                      <OutboundStatus status={m.status} />
                      {m.reconciled && <Badge size="xs" color="amber" variant="outline">reconciled</Badge>}
                    </Group>
                  </Table.Td>
                  <Table.Td className="mono">{m.attempts}/{m.max_attempts}</Table.Td>
                  <Table.Td className="mono">
                    {m.erp_result?.erp_confirmation_group && m.erp_result?.erp_confirmation_count
                      ? `${m.erp_result.erp_confirmation_group}/${m.erp_result.erp_confirmation_count}` : "—"}
                  </Table.Td>
                  <Table.Td><Text size="xs" c="red.7" lineClamp={1} maw={360}>{m.last_error}</Text></Table.Td>
                </Table.Tr>
              ))}
              {!outbound.data?.length && (
                <Table.Tr><Table.Td colSpan={8}><Text c="dimmed" size="sm" ta="center" py="md">No messages.</Text></Table.Td></Table.Tr>
              )}
              {Boolean(outbound.data?.length) && !outRows.length && (
                <Table.Tr><Table.Td colSpan={8}><Text c="dimmed" size="sm" ta="center" py="md">No messages match your search.</Text></Table.Td></Table.Tr>
              )}
            </Table.Tbody>
          </Table>
        </Tabs.Panel>
        <Tabs.Panel value="in" pt="sm">
          <TextInput size="xs" w={240} mb="xs" leftSection={<IconSearch size={14} />}
            rightSection={inSearch ? (
              <ActionIcon variant="subtle" color="gray" size="xs" onClick={() => setInSearch("")} aria-label="Clear search"><IconX size={12} /></ActionIcon>
            ) : undefined}
            placeholder="Order number…" value={inSearch} onChange={(e) => setInSearch(e.currentTarget.value)} />
          <Table className="panel">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Received</Table.Th>
                <Table.Th>Order</Table.Th>
                <Table.Th>SAP change time</Table.Th>
                <Table.Th>SAP status</Table.Th>
                <Table.Th>Material</Table.Th>
                <Table.Th>Qty</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {inRows.map((m) => (
                <Table.Tr key={m.id}>
                  <Table.Td className="mono">{fmtTime(m.received_at)}</Table.Td>
                  <Table.Td className="mono">{m.erp_key}</Table.Td>
                  <Table.Td className="mono">{fmtDateTime(m.erp_changed_at)}</Table.Td>
                  {/* production orders and (phase 4) PM orders arrive here */}
                  <Table.Td className="mono">{Array.isArray(m.payload.erp_status) ? (m.payload.erp_status as string[]).join(" ") : `PM ${String(m.payload.system_status ?? "")}`}</Table.Td>
                  <Table.Td className="mono">{(m.payload.material as { number: string } | undefined)?.number ?? "maintenance order"}</Table.Td>
                  <Table.Td className="mono">{m.payload.planned_qty != null ? String(m.payload.planned_qty) : ""}</Table.Td>
                </Table.Tr>
              ))}
              {!inbound.data?.length && (
                <Table.Tr><Table.Td colSpan={6}><Text c="dimmed" size="sm" ta="center" py="md">No inbound orders yet.</Text></Table.Td></Table.Tr>
              )}
              {Boolean(inbound.data?.length) && !inRows.length && (
                <Table.Tr><Table.Td colSpan={6}><Text c="dimmed" size="sm" ta="center" py="md">No orders match your search.</Text></Table.Td></Table.Tr>
              )}
            </Table.Tbody>
          </Table>
        </Tabs.Panel>
      </Tabs>

      <MessageDrawer id={openId} canRetry={canRetry} onClose={() => setOpenId(null)} />
    </>
  );
}

function Tile({ icon: Icon, label, value, color, hot }: { icon: Icon; label: string; value: number; color: string; hot?: boolean }) {
  return (
    <div className="panel kpi wo-kpi" style={{ borderTop: `3px solid ${color}`, cursor: "default" }}>
      <div className="wo-kpi-top"><span className="wo-kpi-icon" style={{ background: color }}><Icon size={13} stroke={2.2} /></span></div>
      <div className="label">{label}</div>
      <div className="value" style={{ color: hot ? color : undefined }}>{value}</div>
    </div>
  );
}

function MessageDrawer({ id, canRetry, onClose }: { id: string | null; canRetry: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const msg = useQuery({
    queryKey: ["erp", "outbound-detail", id],
    queryFn: () => erp<Outbound & { attempts_log: Attempt[] }>(`/api/v1/outbound/${id}`),
    enabled: Boolean(id),
    refetchInterval: 2000,
  });
  const retry = useMutation({
    mutationFn: () => erp(`/api/v1/outbound/${id}/retry`, { method: "POST", body: JSON.stringify({ extra_attempts: 3 }) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["erp"] }),
  });
  const m = msg.data;
  return (
    <Drawer opened={Boolean(id)} onClose={onClose} position="right" size="lg" title={<Text fw={600}>Outbound message</Text>}>
      {m && (
        <Stack>
          <Group justify="space-between">
            <Group gap={6}>
              <OutboundStatus status={m.status} />
              {m.reconciled && <Badge size="sm" color="amber" variant="outline">reconciled</Badge>}
            </Group>
            {canRetry && ["FAILED", "DEAD", "RETRY_WAIT"].includes(m.status) && (
              <Button leftSection={<IconRotateClockwise size={14} />} loading={retry.isPending} onClick={() => retry.mutate()}>
                Retry now
              </Button>
            )}
          </Group>
          <Text size="xs" c="dimmed">
            Idempotency key <Code>{m.id}</Code>. It is written to SAP's ConfirmationText as <Code>MES:{m.id.slice(0, 8)}…</Code>, and every retry
            looks it up first so a posting is never duplicated.
          </Text>
          <div className="label">Attempts</div>
          <Timeline bulletSize={14} lineWidth={2}>
            {m.attempts_log.map((a) => (
              <Timeline.Item
                key={a.attempt_no}
                color={OUTCOME_COLOR[a.outcome] ?? "gray"}
                title={<Text size="sm" fw={600} c={`${OUTCOME_COLOR[a.outcome] ?? "gray"}.8`}>#{a.attempt_no} {a.outcome}</Text>}
              >
                <Text size="xs" c="dimmed" className="mono">
                  {fmtTime(a.started_at)} · {Math.round(new Date(a.finished_at).getTime() - new Date(a.started_at).getTime())} ms
                </Text>
                {a.outcome === "RECONCILED" && <Text size="xs">SAP already had this posting. No second posting was made.</Text>}
                {a.error && <Text size="xs" c="red.3">{a.error}</Text>}
              </Timeline.Item>
            ))}
          </Timeline>
          {m.status === "RETRY_WAIT" && <Text size="xs" c="amber.8">Next attempt {fromNow(m.next_attempt_at)}</Text>}
          <div className="label">Canonical payload</div>
          <Code block>{JSON.stringify(m.payload, null, 2)}</Code>
        </Stack>
      )}
    </Drawer>
  );
}
