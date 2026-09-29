import { ActionIcon, Alert, Group, Loader, Pagination, Select, SimpleGrid, Text, TextInput } from "@mantine/core";
import {
  IconDownload, IconHistory, IconSearch, IconShieldCheck, IconShieldOff, IconShieldX, IconX, type Icon,
} from "@tabler/icons-react";
import { useMutation } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { type AuditEntry, core, unwrap } from "@mes/api-client";
import { useCan } from "../auth";
import { AUDIT_SORT_VALUE, type AuditSortKey, AuditTable, useAudit } from "../components/AuditTrail";
import { PageHeader } from "../components/Layout";
import { usePlant } from "../components/plant";
import { type Cell, csvName, downloadCsv } from "../lib/csv";
import { fmtDateTime } from "../lib/format";

/** Clickable KPI tile: icon + colour-accented top border (same look used across the app). */
function AuditKpi({
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

const TABLES = [
  "planning.work_order",
  "planning.work_order_operation",
  "planning.schedule",
  "planning.schedule_entry",
  "planning.work_order_status_history",
  "execution.production_confirmation",
  "compliance.e_signature",
  "masterdata.material",
  "identity.app_user",
  "org.equipment",
];
const PAGE = 50;

export function AuditPage() {
  const { plant } = usePlant();
  const canVerify = useCan("audit:verify");
  const [table, setTable] = useState<string | null>(null);
  const [record, setRecord] = useState("");
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<{ key: AuditSortKey; dir: 1 | -1 }>({ key: "when", dir: -1 });
  const toggleSort = (key: AuditSortKey) => setSort((s) => (s.key === key ? { key, dir: s.dir === 1 ? -1 : 1 } : { key, dir: 1 }));

  const q = useAudit({
    plant_id: plant?.id,
    table_name: table ?? undefined,
    record_id: record.trim() ? [record.trim()] : undefined,
    offset: (page - 1) * PAGE,
    limit: PAGE,
  }, Boolean(plant));
  const todaySince = useMemo(() => new Date(new Date().setHours(0, 0, 0, 0)).toISOString(), []);
  const today = useAudit({ plant_id: plant?.id, since: todaySince, limit: 1 }, Boolean(plant));

  const verify = useMutation({ mutationFn: () => unwrap(core.GET("/api/v1/audit/verify")) });

  const rows = useMemo(() => {
    const s = search.trim().toLowerCase();
    const items = (q.data?.items ?? []).filter((e: AuditEntry) => !s
      || (e.actor_name ?? e.actor_id).toLowerCase().includes(s) || `${e.schema_name}.${e.table_name}`.toLowerCase().includes(s)
      || (e.reason ?? "").toLowerCase().includes(s) || (e.changed_fields ?? []).some((f) => f.toLowerCase().includes(s)));
    const val = AUDIT_SORT_VALUE[sort.key];
    return [...items].sort((a, b) => {
      const va = val(a);
      const vb = val(b);
      const cmp = typeof va === "number" && typeof vb === "number" ? va - vb : String(va).localeCompare(String(vb));
      return cmp * sort.dir;
    });
  }, [q.data, search, sort]);

  const exportCsv = () => {
    const header: Cell[] = ["When", "Actor", "Actor type", "Table", "Action", "Changed fields", "Reason", "Signed", "Request", "Tx"];
    const csvRows: Cell[][] = rows.map((e) => [
      fmtDateTime(e.occurred_at), e.actor_name ?? e.actor_id, e.actor_type, `${e.schema_name}.${e.table_name}`, e.action,
      (e.changed_fields ?? []).join(" "), e.reason ?? "", Boolean(e.signature_id), e.request_id ?? "", e.tx_id,
    ]);
    downloadCsv(csvName(`audit-trail${table ? `-${table}` : ""}`), [header, ...csvRows]);
  };

  return (
    <>
      <PageHeader
        title="Audit trail"
        sub="Every change to business data, written by database triggers and chained with SHA-256 hashes"
      />
      <SimpleGrid cols={{ base: 1, sm: 3 }} mb="md" className="rise">
        <AuditKpi icon={IconHistory} label="Entries (filtered)" value={q.data?.total ?? "…"} color="var(--mes-blue)"
          sub={table ?? "all tables"} />
        <AuditKpi icon={IconHistory} label="Today" value={today.data?.total ?? "…"} color="var(--mes-navy)" />
        <AuditKpi
          icon={verify.data ? (verify.data.ok ? IconShieldCheck : IconShieldX) : IconShieldOff}
          label="Hash chain" color={verify.data ? (verify.data.ok ? "var(--mes-green)" : "var(--mes-red)") : "var(--mes-text-dim)"}
          hot={Boolean(verify.data && !verify.data.ok)}
          value={verify.isPending ? "verifying…" : verify.data ? (verify.data.ok ? "intact" : "BROKEN") : "not checked"}
          sub={canVerify ? "click to verify" : undefined}
          onClick={canVerify ? () => verify.mutate() : undefined}
        />
      </SimpleGrid>
      {verify.data && (
        <Alert
          mb="md"
          color={verify.data.ok ? "green" : "red"}
          icon={verify.data.ok ? <IconShieldCheck /> : <IconShieldX />}
          title={verify.data.ok ? "Chain intact" : "Chain BROKEN"}
        >
          {verify.data.ok
            ? `${verify.data.checked} sealed entries verified. ${verify.data.unsealed} newer entries wait for the next seal (every ~10 s).`
            : `Verification failed at seal #${verify.data.first_broken_seq}: a row was altered or removed.`}
        </Alert>
      )}
      <Group mb="sm" justify="space-between" wrap="wrap">
        <Group gap="sm">
          <Select
            size="xs"
            placeholder="All tables"
            data={TABLES}
            value={table}
            onChange={(v) => {
              setTable(v);
              setPage(1);
            }}
            clearable
            w={280}
          />
          <TextInput size="xs" placeholder="Record id" value={record} onChange={(e) => setRecord(e.currentTarget.value)} w={220} />
        </Group>
        <Group gap="xs">
          <TextInput size="xs" w={260} leftSection={<IconSearch size={14} />}
            rightSection={search ? (
              <ActionIcon variant="subtle" color="gray" size="xs" onClick={() => setSearch("")} aria-label="Clear search"><IconX size={12} /></ActionIcon>
            ) : undefined}
            placeholder="Who, table, reason, field…" value={search} onChange={(e) => setSearch(e.currentTarget.value)} />
          <ActionIcon variant="light" size="input-xs" disabled={rows.length === 0} onClick={exportCsv} aria-label="Download CSV">
            <IconDownload size={14} />
          </ActionIcon>
          <Text size="xs" c="dimmed">{q.data?.total ?? 0} entries</Text>
        </Group>
      </Group>
      <div className="panel">
        {q.isPending ? <Loader m="md" size="sm" /> : rows.length === 0 ? (
          <EmptyState icon={IconHistory} title={search ? "No entries match your search" : "Nothing here yet"}
            sub={search ? "Try a different actor, table or field." : "Changes to business data will appear here as they happen."} />
        ) : <AuditTable items={rows} sort={sort} onSort={toggleSort} />}
      </div>
      {(q.data?.total ?? 0) > PAGE && (
        <Pagination mt="sm" size="sm" total={Math.ceil((q.data?.total ?? 0) / PAGE)} value={page} onChange={setPage} />
      )}
    </>
  );
}
