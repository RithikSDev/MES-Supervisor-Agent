import { Badge, Code, Collapse, Group, Loader, Stack, Table, Text, Tooltip, UnstyledButton } from "@mantine/core";
import { IconArrowsSort, IconChevronDown, IconChevronUp, IconLock, IconSignature } from "@tabler/icons-react";
import { useQuery } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { type AuditEntry, core, unwrap } from "@mes/api-client";
import { fmtDateTime } from "../lib/format";

const ACTION: Record<string, { label: string; color: string }> = {
  I: { label: "INSERT", color: "green" },
  U: { label: "UPDATE", color: "blue" },
  D: { label: "DELETE", color: "red" },
};

export function useAudit(params: Record<string, unknown>, enabled = true) {
  return useQuery({
    queryKey: ["audit", params],
    queryFn: () =>
      unwrap(core.GET("/api/v1/audit", { params: { query: params as never } })),
    enabled,
    refetchInterval: 10_000,
  });
}

function show(v: unknown): string {
  if (v === null || v === undefined) return "∅";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

export function AuditDiff({ entry }: { entry: AuditEntry }) {
  const before = (entry.before ?? {}) as Record<string, unknown>;
  const after = (entry.after ?? {}) as Record<string, unknown>;
  const fields =
    entry.action === "U" ? entry.changed_fields ?? [] : Object.keys(entry.action === "I" ? after : before);
  return (
    <Table withRowBorders={false} verticalSpacing={2} fz="xs">
      <Table.Tbody>
        {fields
          .filter((f) => !["created_at", "updated_at", "created_by", "updated_by", "row_version"].includes(f) || entry.action === "U")
          .map((f) => (
            <Table.Tr key={f}>
              <Table.Td w={200} className="mono" c="dimmed">{f}</Table.Td>
              {entry.action === "U" && (
                <Table.Td className="mono" c="red.3" style={{ textDecoration: "line-through", opacity: 0.8 }}>
                  {show(before[f])}
                </Table.Td>
              )}
              <Table.Td className="mono" c={entry.action === "D" ? "red.3" : "green.3"}>
                {show(entry.action === "D" ? before[f] : after[f])}
              </Table.Td>
            </Table.Tr>
          ))}
      </Table.Tbody>
    </Table>
  );
}

export type AuditSortKey = "when" | "who" | "record" | "action";
export const AUDIT_SORT_VALUE: Record<AuditSortKey, (e: AuditEntry) => number | string> = {
  when: (e) => e.occurred_at,
  who: (e) => e.actor_name ?? e.actor_id,
  record: (e) => `${e.schema_name}.${e.table_name}`,
  action: (e) => e.action,
};

/** Sortable column header, undecorated until a sort is picked; shared with the runs/chats tables elsewhere. */
function SortTh({
  id, sort, onSort, w, children,
}: { id: AuditSortKey; sort: { key: AuditSortKey; dir: 1 | -1 }; onSort: (id: AuditSortKey) => void; w?: number; children: React.ReactNode }) {
  const active = sort.key === id;
  return (
    <Table.Th w={w}>
      <UnstyledButton onClick={() => onSort(id)} className="sortable-th">
        <Group gap={4} wrap="nowrap">
          <span>{children}</span>
          {active ? (sort.dir === 1 ? <IconChevronUp size={12} /> : <IconChevronDown size={12} />) : <IconArrowsSort size={12} opacity={0.35} />}
        </Group>
      </UnstyledButton>
    </Table.Th>
  );
}

export function AuditTable({
  items, sort, onSort,
}: { items: AuditEntry[]; sort?: { key: AuditSortKey; dir: 1 | -1 }; onSort?: (id: AuditSortKey) => void }) {
  const [open, setOpen] = useState<number | null>(null);
  return (
    <Table>
      <Table.Thead>
        <Table.Tr>
          {sort && onSort ? (
            <>
              <SortTh id="when" w={130} sort={sort} onSort={onSort}>When</SortTh>
              <SortTh id="who" sort={sort} onSort={onSort}>Who</SortTh>
              <SortTh id="record" sort={sort} onSort={onSort}>Record</SortTh>
              <SortTh id="action" sort={sort} onSort={onSort}>Change</SortTh>
              <Table.Th>Reason</Table.Th>
              <Table.Th w={50} />
            </>
          ) : (
            <>
              <Table.Th w={130}>When</Table.Th>
              <Table.Th>Who</Table.Th>
              <Table.Th>Record</Table.Th>
              <Table.Th>Change</Table.Th>
              <Table.Th>Reason</Table.Th>
              <Table.Th w={50} />
            </>
          )}
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {items.map((e) => (
          <Fragment key={e.id}>
            <Table.Tr onClick={() => setOpen(open === e.id ? null : e.id)} style={{ cursor: "pointer" }}>
              <Table.Td className="mono">{fmtDateTime(e.occurred_at)}</Table.Td>
              <Table.Td>
                <Group gap={6} wrap="nowrap">
                  <Text size="sm" style={{ whiteSpace: "nowrap" }}>{e.actor_name ?? e.actor_id}</Text>
                  {e.actor_type === "SERVICE" && <Badge size="xs" variant="outline" color="navy.6" style={{ flexShrink: 0 }}>service</Badge>}
                </Group>
              </Table.Td>
              <Table.Td>
                <Text size="xs" className="mono" c="dimmed">
                  {e.schema_name}.{e.table_name}
                </Text>
              </Table.Td>
              <Table.Td>
                <Group gap={6} wrap="nowrap">
                  <Badge size="xs" variant="light" color={ACTION[e.action]?.color} style={{ flexShrink: 0 }}>{ACTION[e.action]?.label}</Badge>
                  <Text size="xs" c="dimmed" lineClamp={1}>{(e.changed_fields ?? []).join(", ")}</Text>
                </Group>
              </Table.Td>
              <Table.Td>
                <Group gap={6} wrap="nowrap">
                  {e.signature_id && (
                    <Tooltip label="Electronically signed">
                      <IconSignature size={15} color="var(--mes-amber)" />
                    </Tooltip>
                  )}
                  <Text size="xs" lineClamp={1}>{e.reason ?? ""}</Text>
                </Group>
              </Table.Td>
              <Table.Td>
                {e.seal_seq ? (
                  <Tooltip label={`Sealed in hash chain #${e.seal_seq}`}>
                    <IconLock size={14} color="var(--mes-text-dim)" />
                  </Tooltip>
                ) : (
                  <Text size="xs" c="dimmed">…</Text>
                )}
              </Table.Td>
            </Table.Tr>
            <Table.Tr style={{ display: open === e.id ? undefined : "none" }}>
              <Table.Td colSpan={6} bg="var(--mes-panel-2)">
                <Collapse in={open === e.id}>
                  <Stack gap={4} p={4}>
                    <AuditDiff entry={e} />
                    <Text size="xs" c="dimmed">
                      request <Code>{e.request_id ?? "—"}</Code> · tx <Code>{e.tx_id}</Code>
                      {e.chain_hash && (
                        <>
                          {" "}· hash <Code>{e.chain_hash.slice(0, 16)}…</Code>
                        </>
                      )}
                    </Text>
                  </Stack>
                </Collapse>
              </Table.Td>
            </Table.Tr>
          </Fragment>
        ))}
      </Table.Tbody>
    </Table>
  );
}

export function RecordAuditTrail({ recordIds }: { recordIds: string[] }) {
  const q = useAudit({ record_id: recordIds, limit: 200 }, recordIds.length > 0);
  if (q.isPending) return <Loader size="sm" />;
  if (!q.data?.items.length) return <Text c="dimmed" size="sm">No audit entries.</Text>;
  return <AuditTable items={q.data.items} />;
}
