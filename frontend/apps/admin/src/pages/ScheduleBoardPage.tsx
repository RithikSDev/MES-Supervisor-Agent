import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Group,
  Loader,
  Menu,
  ScrollArea,
  SegmentedControl,
  Select,
  Stack,
  Tabs,
  Text,
  Tooltip,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import {
  IconAlertTriangle,
  IconChevronDown,
  IconCircleX,
  IconLock,
  IconLockOpen,
  IconPlus,
  IconSignature,
  IconTarget,
  IconTrash,
  IconWand,
} from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import dayjs from "dayjs";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router";
import * as vis from "vis-timeline/standalone";
import { core, type EntryOut, type ScheduleView, unwrap } from "@mes/api-client";
import { useCan } from "../auth";
import { WoStatus } from "../components/badges";
import { SignatureModal } from "../components/dialogs";
import { PageHeader } from "../components/Layout";
import { usePlant } from "../components/plant";
import { fmtDate, fmtDateTime, minutes } from "../lib/format";

type TlItem = {
  id: string;
  group: string;
  start: Date;
  end: Date;
  content?: string;
  className?: string;
  type?: string;
  title?: string;
  editable?: boolean;
};
// The standalone bundle exports DataSet at runtime, but its typings don't; this is the slice we use.
type DataSetLike<T> = {
  add(items: T[]): void;
  update(items: T[]): void;
  remove(ids: (string | number)[]): void;
  clear(): void;
  getIds(): (string | number)[];
};
const { Timeline } = vis;
const DataSet = (vis as unknown as { DataSet: new <T>() => DataSetLike<T> }).DataSet;
type Timeline = vis.Timeline;

type TlGroup = { id: string; content: string; order: number; className?: string };

const ZOOM: Record<string, number> = { "1d": 1, "3d": 3, "1w": 7, "2w": 14 };
const esc = (s: string) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);

export function ScheduleBoardPage() {
  const { plant } = usePlant();
  const qc = useQueryClient();
  const canEdit = useCan("schedule:edit");
  const canPublish = useCan("schedule:publish");
  const [scheduleId, setScheduleId] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [tab, setTab] = useState<string | null>("unscheduled");
  const [zoom, setZoom] = useState("3d");
  const [signing, setSigning] = useState(false);
  const [signError, setSignError] = useState<string | null>(null);

  const schedules = useQuery({
    queryKey: ["schedules", plant?.id],
    queryFn: () => unwrap(core.GET("/api/v1/schedules", { params: { query: { plant_id: plant!.id } } })),
    enabled: Boolean(plant),
  });
  // default to the draft if one exists, else the published schedule
  useEffect(() => {
    const list = schedules.data ?? [];
    if (!list.length) return;
    if (scheduleId && list.some((s) => s.id === scheduleId)) return;
    setScheduleId((list.find((s) => s.status === "DRAFT") ?? list.find((s) => s.status === "PUBLISHED") ?? list[0]).id);
  }, [schedules.data, scheduleId]);

  const view = useQuery({
    queryKey: ["schedule", scheduleId],
    queryFn: () => unwrap(core.GET("/api/v1/schedules/{schedule_id}", { params: { path: { schedule_id: scheduleId! } } })),
    enabled: Boolean(scheduleId),
    refetchInterval: 15_000,
  });
  const v = view.data;
  const isDraft = v?.schedule.status === "DRAFT";
  const editable = isDraft && canEdit;

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["schedule", scheduleId] });
    qc.invalidateQueries({ queryKey: ["schedules", plant?.id] });
    qc.invalidateQueries({ queryKey: ["work-orders"] });
  };

  const createDraft = useMutation({
    mutationFn: () => unwrap(core.POST("/api/v1/schedules", { body: { plant_id: plant!.id, horizon_days: 14 } })),
    onSuccess: (s) => {
      setScheduleId(s.id);
      qc.invalidateQueries({ queryKey: ["schedules", plant?.id] });
    },
  });
  const autoSeq = useMutation({
    mutationFn: (reset: boolean) =>
      unwrap(core.POST("/api/v1/schedules/{schedule_id}/auto-sequence", { params: { path: { schedule_id: scheduleId! } }, body: { reset_unlocked: reset } })),
    onSuccess: (r) => {
      invalidate();
      notifications.show({
        color: r.unplaced.length ? "amber" : "green",
        title: `Placed ${r.placed} operation${r.placed === 1 ? "" : "s"}`,
        message: r.unplaced.length ? `${r.unplaced.length} could not be placed (see Unscheduled).` : "Every open operation is on the board.",
      });
    },
  });
  const publish = useMutation({
    mutationFn: (reason: string) =>
      unwrap(core.POST("/api/v1/schedules/{schedule_id}/publish", { params: { path: { schedule_id: scheduleId! } }, body: { reason, row_version: v?.schedule.row_version } })),
    meta: { silent: true },
    onSuccess: () => {
      setSigning(false);
      invalidate();
      notifications.show({ color: "green", title: "Schedule published", message: "Work orders are now SCHEDULED and can be dispatched." });
    },
    onError: (e: Error & { details?: unknown }) => {
      const details = Array.isArray(e.details) ? (e.details as { message: string }[]).map((d) => d.message).join("\n") : "";
      setSignError(`${e.message}${details ? `\n${details}` : ""}`);
    },
  });
  const discard = useMutation({
    mutationFn: () => unwrap(core.POST("/api/v1/schedules/{schedule_id}/discard", { params: { path: { schedule_id: scheduleId! } } })),
    onSuccess: () => {
      setScheduleId(null);
      invalidate();
    },
  });
  const place = useMutation({
    mutationFn: (operation_id: string) =>
      unwrap(core.POST("/api/v1/schedules/{schedule_id}/entries", { params: { path: { schedule_id: scheduleId! } }, body: { operation_id } })),
    onSuccess: (e) => {
      invalidate();
      setSelected(e.id);
    },
  });
  const lock = useMutation({
    mutationFn: ({ id, locked }: { id: string; locked: boolean }) =>
      unwrap(core.PUT("/api/v1/schedules/{schedule_id}/entries/{entry_id}/lock", { params: { path: { schedule_id: scheduleId!, entry_id: id } }, body: { locked } })),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: (id: string) =>
      unwrap(core.DELETE("/api/v1/schedules/{schedule_id}/entries/{entry_id}", { params: { path: { schedule_id: scheduleId!, entry_id: id } } })),
    onSuccess: () => {
      setSelected(null);
      invalidate();
    },
  });

  const errors = v?.conflicts.filter((c) => c.severity === "error") ?? [];
  const warnings = v?.conflicts.filter((c) => c.severity === "warning") ?? [];
  const selectedEntry = v?.entries.find((e) => e.id === selected) ?? null;

  return (
    <>
      <PageHeader
        title="Schedule board"
        sub={
          v ? (
            <>
              {v.schedule.name} · horizon {fmtDate(v.schedule.horizon_start)} → {fmtDate(v.schedule.horizon_end)}
              {v.schedule.published_at && <> · published {fmtDateTime(v.schedule.published_at)} by {v.schedule.published_by}</>}
            </>
          ) : (
            "No schedule yet"
          )
        }
        right={
          <Group gap="xs">
            <Select
              size="xs"
              w={260}
              placeholder="Schedule"
              value={scheduleId}
              onChange={setScheduleId}
              data={(schedules.data ?? []).map((s) => ({ value: s.id, label: `v${s.version} · ${s.status}` }))}
            />
            {canEdit && !isDraft && (
              <Button leftSection={<IconPlus size={14} />} loading={createDraft.isPending} onClick={() => createDraft.mutate()}>
                {schedules.data?.length ? "New draft from published" : "Create schedule"}
              </Button>
            )}
          </Group>
        }
      />

      {v && (
        <Group justify="space-between" mb="xs" className="panel" p={8}>
          <Group gap="lg">
            <ScheduleStatusBadge status={v.schedule.status} />
            <Stat label="On board" value={v.entries.length} />
            <Stat label="Unscheduled" value={v.unscheduled.length} tone={v.unscheduled.length ? "amber" : undefined} />
            <Stat label="Blocking" value={errors.length} tone={errors.length ? "red" : undefined} />
            <Stat label="Warnings" value={warnings.length} tone={warnings.length ? "amber" : undefined} />
          </Group>
          <Group gap="xs">
            <SegmentedControl size="xs" value={zoom} onChange={setZoom} data={[{ label: "Day", value: "1d" }, { label: "3 days", value: "3d" }, { label: "Week", value: "1w" }, { label: "2 weeks", value: "2w" }]} />
            {editable && (
              <>
                <Menu position="bottom-end">
                  <Menu.Target>
                    <Button variant="light" leftSection={<IconWand size={14} />} rightSection={<IconChevronDown size={12} />} loading={autoSeq.isPending}>
                      Auto-sequence
                    </Button>
                  </Menu.Target>
                  <Menu.Dropdown>
                    <Menu.Item onClick={() => autoSeq.mutate(false)}>
                      <Text size="sm">Fill: place unscheduled operations</Text>
                      <Text size="xs" c="dimmed">Keeps everything already on the board</Text>
                    </Menu.Item>
                    <Menu.Item onClick={() => autoSeq.mutate(true)}>
                      <Text size="sm">Re-sequence all unlocked</Text>
                      <Text size="xs" c="dimmed">Earliest due date first; locked entries stay put</Text>
                    </Menu.Item>
                  </Menu.Dropdown>
                </Menu>
                <Button variant="default" leftSection={<IconCircleX size={14} />} loading={discard.isPending} onClick={() => discard.mutate()}>
                  Discard draft
                </Button>
                {canPublish && (
                  <Tooltip label={errors.length ? "Resolve blocking conflicts first" : "Sign and publish"} disabled={!errors.length}>
                    <Button
                      leftSection={<IconSignature size={14} />}
                      disabled={errors.length > 0 || v.entries.length === 0}
                      onClick={() => {
                        setSignError(null);
                        setSigning(true);
                      }}
                    >
                      Publish…
                    </Button>
                  </Tooltip>
                )}
              </>
            )}
          </Group>
        </Group>
      )}

      {!scheduleId && !schedules.isPending && (
        <Alert color="navy" variant="outline" title="No schedule for this plant yet">
          Create a draft, auto-sequence it, then publish it with an e-signature.
        </Alert>
      )}
      {view.isPending && scheduleId && <Loader size="sm" />}

      {v && (
        <Group align="stretch" gap="sm" wrap="nowrap">
          <div style={{ flex: 1, minWidth: 0 }}>
            <Board
              view={v}
              editable={editable}
              zoomDays={ZOOM[zoom]}
              selected={selected}
              onSelect={setSelected}
              onMoved={invalidate}
              scheduleId={scheduleId!}
            />
            {editable && (
              <Text size="xs" c="dimmed" mt={4}>
                Drag to move or change machine; start times snap to working time and the end is recalculated from standard times.
                Hatched areas are outside shifts. Ctrl + scroll zooms.
              </Text>
            )}
          </div>

          <div className="panel" style={{ width: 330, flexShrink: 0 }}>
            <Tabs value={tab} onChange={setTab}>
              <Tabs.List grow>
                <Tabs.Tab value="unscheduled">Unscheduled {v.unscheduled.length}</Tabs.Tab>
                <Tabs.Tab value="conflicts" c={errors.length ? "red.7" : undefined}>Conflicts {v.conflicts.length}</Tabs.Tab>
                <Tabs.Tab value="selected">Selected</Tabs.Tab>
              </Tabs.List>
              <ScrollArea h="calc(100vh - 290px)">
                <Tabs.Panel value="unscheduled" p="xs">
                  <Stack gap={6}>
                    {v.unscheduled.map((o) => (
                      <div key={o.operation_id} className="panel" style={{ padding: "6px 8px", background: "var(--mes-panel-2)" }}>
                        <Group justify="space-between" wrap="nowrap">
                          <div>
                            <Text className="mono" size="sm">{o.erp_order_number} / {o.op_no}</Text>
                            <Text size="xs" c="dimmed">{o.material_number} · {o.op_description}</Text>
                            <Text size="xs" c="dimmed" className="mono">
                              {minutes(o.duration_min)} · due {fmtDate(o.due)} · P{o.priority}
                            </Text>
                          </div>
                          {editable && (
                            <Tooltip label="Place at the earliest free slot">
                              <ActionIcon variant="light" loading={place.isPending && place.variables === o.operation_id} onClick={() => place.mutate(o.operation_id)}>
                                <IconPlus size={14} />
                              </ActionIcon>
                            </Tooltip>
                          )}
                        </Group>
                      </div>
                    ))}
                    {!v.unscheduled.length && <Text size="sm" c="dimmed" p="sm">Every open operation is scheduled.</Text>}
                  </Stack>
                </Tabs.Panel>
                <Tabs.Panel value="conflicts" p="xs">
                  <Stack gap={6}>
                    {[...errors, ...warnings].map((c, i) => (
                      <Group
                        key={i}
                        gap={8}
                        wrap="nowrap"
                        align="flex-start"
                        style={{ cursor: c.entry_id ? "pointer" : undefined }}
                        onClick={() => {
                          if (c.entry_id) {
                            setSelected(c.entry_id);
                            setTab("selected");
                          }
                        }}
                      >
                        <IconAlertTriangle size={15} color={c.severity === "error" ? "var(--mes-red)" : "var(--mes-amber)"} style={{ flexShrink: 0, marginTop: 2 }} />
                        <div>
                          <Text size="xs" fw={600} c={c.severity === "error" ? "red.7" : "amber.8"}>{c.kind.replaceAll("_", " ")}</Text>
                          <Text size="xs">{c.message}</Text>
                        </div>
                      </Group>
                    ))}
                    {!v.conflicts.length && <Text size="sm" c="dimmed" p="sm">No conflicts.</Text>}
                  </Stack>
                </Tabs.Panel>
                <Tabs.Panel value="selected" p="xs">
                  {selectedEntry ? (
                    <SelectedEntry
                      e={selectedEntry}
                      view={v}
                      editable={editable}
                      onLock={(locked) => lock.mutate({ id: selectedEntry.id, locked })}
                      onRemove={() => remove.mutate(selectedEntry.id)}
                    />
                  ) : (
                    <Text size="sm" c="dimmed" p="sm">Click an operation on the board.</Text>
                  )}
                </Tabs.Panel>
              </ScrollArea>
            </Tabs>
          </div>
        </Group>
      )}

      {v && (
        <SignatureModal
          opened={signing}
          meaning="APPROVED"
          subject={`Publish ${v.schedule.name}: ${v.entries.length} operations on ${new Set(v.entries.map((e) => e.equipment_id)).size} machines. Replaces the currently published schedule.`}
          busy={publish.isPending}
          error={signError}
          onClose={() => setSigning(false)}
          onSign={(reason) => publish.mutate(reason)}
        />
      )}
    </>
  );
}

function Stat({ label, value, tone }: { label: string; value: number; tone?: "red" | "amber" }) {
  return (
    <Group gap={6} align="baseline">
      <Text ff="var(--mes-serif)" fw={600} fz={22} lh={1} c={tone ? `${tone}.8` : undefined}>{value}</Text>
      <span className="label">{label}</span>
    </Group>
  );
}

function ScheduleStatusBadge({ status }: { status: string }) {
  const color = status === "DRAFT" ? "amber" : status === "PUBLISHED" ? "green" : "navy.6";
  return <Badge color={color} variant={status === "DRAFT" ? "filled" : "outline"}>{status}</Badge>;
}

function SelectedEntry({
  e,
  view,
  editable,
  onLock,
  onRemove,
}: {
  e: EntryOut;
  view: ScheduleView;
  editable: boolean;
  onLock: (locked: boolean) => void;
  onRemove: () => void;
}) {
  const eq = view.resources.find((r) => r.id === e.equipment_id);
  const conflicts = view.conflicts.filter((c) => c.entry_id === e.id);
  return (
    <Stack gap="xs">
      <div>
        <Text className="mono" fw={600}>{e.erp_order_number} / {e.op_no}</Text>
        <Text size="sm">{e.op_description}</Text>
        <Text size="xs" c="dimmed">{e.material_number} · {e.material_description}</Text>
      </div>
      <Group gap={6}><WoStatus status={e.wo_status} /><Badge size="sm" variant="outline" color="navy.6">P{e.priority}</Badge>{e.locked && <Badge size="sm" color="navy.6" leftSection={<IconLock size={10} />}>locked</Badge>}</Group>
      <div className="label">Machine</div>
      <Text size="sm" className="mono">{eq?.code} · {eq?.name}</Text>
      <div className="label">Planned</div>
      <Text size="sm" className="mono">{fmtDateTime(e.planned_start)} → {fmtDateTime(e.planned_end)}</Text>
      <Text size="xs" c="dimmed">{minutes(e.duration_min)} working time · {Number(e.planned_qty)} {e.uom} · due {fmtDate(e.due)}</Text>
      {conflicts.map((c, i) => (
        <Alert key={i} color={c.severity === "error" ? "red" : "amber"} variant="light" p={6}>
          <Text size="xs">{c.message}</Text>
        </Alert>
      ))}
      <Group gap="xs" mt="xs">
        <Button component={Link} to={`/work-orders/${e.work_order_id}`} variant="default" leftSection={<IconTarget size={14} />}>
          Open order
        </Button>
        {editable && (
          <>
            <Button variant="default" leftSection={e.locked ? <IconLockOpen size={14} /> : <IconLock size={14} />} onClick={() => onLock(!e.locked)}>
              {e.locked ? "Unlock" : "Lock"}
            </Button>
            <ActionIcon variant="subtle" color="red" onClick={onRemove} aria-label="Remove from schedule">
              <IconTrash size={15} />
            </ActionIcon>
          </>
        )}
      </Group>
    </Stack>
  );
}

// ------------------------------------------------------------------------------------------------ vis-timeline

type Span = [Date, Date];

/** Intersection of every line's off-shift gaps. */
function plantWideOffShift(view: ScheduleView): Span[] {
  const byLine = new Map<string, Span[]>();
  view.off_shift.forEach((o) => byLine.set(o.line_id, [...(byLine.get(o.line_id) ?? []), [new Date(o.start), new Date(o.end)]]));
  const lines = [...byLine.values()];
  if (!lines.length) return [];
  return lines.slice(1).reduce((acc, gaps) => {
    const out: Span[] = [];
    for (const [a0, a1] of acc) {
      for (const [b0, b1] of gaps) {
        const s = a0 > b0 ? a0 : b0;
        const e = a1 < b1 ? a1 : b1;
        if (s < e) out.push([s, e]);
      }
    }
    return out;
  }, lines[0]);
}

function Board({
  view,
  editable,
  zoomDays,
  selected,
  onSelect,
  onMoved,
  scheduleId,
}: {
  view: ScheduleView;
  editable: boolean;
  zoomDays: number;
  selected: string | null;
  onSelect: (id: string | null) => void;
  onMoved: () => void;
  scheduleId: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const tl = useRef<Timeline | null>(null);
  const items = useRef(new DataSet<TlItem>());
  const groups = useRef(new DataSet<TlGroup>());
  // onMove is registered once; it reads the latest values through this ref
  const latest = useRef({ view, scheduleId, onMoved, onSelect });
  latest.current = { view, scheduleId, onMoved, onSelect };

  const entryIds = useMemo(() => new Set(view.entries.map((e) => e.id)), [view.entries]);
  const [range, setRange] = useState<{ start: number; end: number } | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    const t = new Timeline(ref.current, items.current as never, groups.current as never, {
      stack: false,
      orientation: "top",
      // explicit initial range: otherwise vis auto-fits to the data after first load and overrides the zoom
      start: dayjs().subtract(2, "hour").toDate(),
      end: dayjs().add(3, "day").toDate(),
      // content is escaped by us; allow class/style so labels keep their styling
      xss: { disabled: false, filterOptions: { whiteList: { div: ["class"], span: ["class", "style"], b: [], br: [] } } },
      zoomKey: "ctrlKey",
      horizontalScroll: true,
      verticalScroll: true,
      height: "calc(100vh - 290px)",
      groupOrder: "order",
      margin: { item: { horizontal: 0, vertical: 6 } },
      snap: (date: Date) => {
        const ms = 5 * 60 * 1000;
        return new Date(Math.round(date.getTime() / ms) * ms);
      },
      tooltip: { followMouse: true, overflowMethod: "cap" },
      onMove: async (item: TlItem, callback: (i: TlItem | null) => void) => {
        const { scheduleId: sid, view: vw, onMoved: done } = latest.current;
        const entry = vw.entries.find((x) => x.id === item.id);
        try {
          const updated = await unwrap(
            core.PUT("/api/v1/schedules/{schedule_id}/entries/{entry_id}", {
              params: { path: { schedule_id: sid, entry_id: String(item.id) } },
              body: { equipment_id: String(item.group), planned_start: new Date(item.start).toISOString(), row_version: entry?.row_version },
            }),
          );
          callback({ ...item, start: new Date(updated.planned_start), end: new Date(updated.planned_end) });
          done();
        } catch (e) {
          notifications.show({ color: "red", title: "Move rejected", message: (e as Error).message });
          callback(null);
        }
      },
    } as never);
    t.on("select", (p: { items: string[] }) => latest.current.onSelect(p.items[0] ?? null));
    t.on("rangechanged", (p: { start: Date; end: Date }) => setRange({ start: p.start.getTime(), end: p.end.getTime() }));
    tl.current = t;
    return () => {
      t.destroy();
      tl.current = null;
    };
  }, []);

  useEffect(() => {
    tl.current?.setOptions({
      editable: editable ? { updateTime: true, updateGroup: true, add: false, remove: false, overrideItems: false } : false,
    } as never);
  }, [editable]);

  // one flat row per machine (sorted line > work center > machine); nested groups misrender background items
  useEffect(() => {
    let prevWc = "";
    const next: TlGroup[] = view.resources.map((m, order) => {
      const down = m.status !== "AVAILABLE" ? ` <span style="color:var(--mes-red)">${m.status}</span>` : "";
      const first = m.work_center_id !== prevWc;
      prevWc = m.work_center_id;
      return {
        id: m.id,
        order,
        className: first ? "wc-first" : undefined,
        content:
          `<div class="machine-label"><span class="mono">${esc(m.code)}</span>${down}` +
          `<span class="wc">${esc(m.line_code)} · ${esc(m.work_center_code)}</span></div>`,
      };
    });
    groups.current.clear();
    groups.current.add(next);
  }, [view.resources]);

  // items: operations + off-shift shading
  useEffect(() => {
    const errorIds = new Set(view.conflicts.filter((c) => c.severity === "error").map((c) => c.entry_id));
    const lateIds = new Set(view.conflicts.filter((c) => c.kind === "LATE").map((c) => c.entry_id));
    const ops: TlItem[] = view.entries.map((e) => ({
      id: e.id,
      group: e.equipment_id,
      start: new Date(e.planned_start),
      end: new Date(e.planned_end),
      content: `${e.erp_order_number}/${e.op_no}`,
      editable: !e.locked,
      className: [
        "op",
        e.locked && "locked",
        e.wo_status === "IN_PROGRESS" && "in-progress",
        lateIds.has(e.id) && "late",
        errorIds.has(e.id) && "error",
      ]
        .filter(Boolean)
        .join(" "),
      title: `<b>${esc(e.erp_order_number)} / ${esc(e.op_no)}</b> ${esc(e.op_description)}<br/>${esc(e.material_number ?? "")} · ${Number(e.planned_qty)} ${esc(e.uom)}<br/>${dayjs(e.planned_start).format("DD MMM HH:mm")} → ${dayjs(e.planned_end).format("DD MMM HH:mm")}<br/>due ${dayjs(e.due).format("DD MMM")} · ${esc(e.wo_status)}${e.locked ? " · locked" : ""}`,
    }));
    const keep = new Set(ops.map((x) => x.id));
    items.current.remove(items.current.getIds().filter((id) => !/^(off|blk):/.test(String(id)) && !keep.has(String(id))));
    items.current.update(ops);
  }, [view.entries, view.conflicts]);

  // maintenance windows and machines that are down: hatched on the machine's row (auto-sequence plans around them)
  useEffect(() => {
    const blocks = (view.blocked ?? []).map((b, i) => ({
      id: `blk:${i}:${b.equipment_id}`,
      group: b.equipment_id,
      start: new Date(b.start),
      end: new Date(b.end),
      type: "background",
      className: "maint-block",
      title: "Not available: maintenance / machine down",
    })) as TlItem[];
    const keep = new Set(blocks.map((x) => x.id));
    items.current.remove(items.current.getIds().filter((id) => String(id).startsWith("blk:") && !keep.has(String(id))));
    items.current.update(blocks);
  }, [view.blocked]);

  // Off-shift shading: time when *every* line is off shift, as full-height bands. Only bands near the visible
  // window are handed to vis-timeline, clipped to it: off-screen background items otherwise get stretched
  // across the whole view.
  const spans = useMemo(() => plantWideOffShift(view), [view.off_shift]);
  useEffect(() => {
    if (!range) return;
    const margin = range.end - range.start;
    const lo = range.start - margin;
    const hi = range.end + margin;
    const shading = spans
      .filter(([s, e]) => e.getTime() > lo && s.getTime() < hi)
      .map(([s, e]) => ({
        id: `off:${s.getTime()}`,
        start: new Date(Math.max(s.getTime(), lo)),
        end: new Date(Math.min(e.getTime(), hi)),
        type: "background",
        className: "off-shift",
      })) as TlItem[];
    const keep = new Set(shading.map((x) => x.id));
    items.current.remove(items.current.getIds().filter((id) => String(id).startsWith("off:") && !keep.has(String(id))));
    items.current.update(shading);
  }, [spans, range]);

  useEffect(() => {
    const t = tl.current;
    if (!t) return;
    const horizon = dayjs(view.schedule.horizon_start);
    const recent = dayjs().subtract(2, "hour");
    const start = horizon.isAfter(recent) ? horizon : recent;
    const end = start.add(zoomDays, "day");
    t.setWindow(start.toDate(), end.toDate(), { animation: false });
    setRange({ start: start.valueOf(), end: end.valueOf() });
  }, [zoomDays, view.schedule.id, view.schedule.horizon_start]);

  useEffect(() => {
    const t = tl.current;
    if (!t) return;
    if (selected && entryIds.has(selected)) {
      t.setSelection([selected]);
      t.focus(selected, { animation: { duration: 300, easingFunction: "easeInOutQuad" }, zoom: false } as never);
    } else if (!selected) {
      t.setSelection([]);
    }
  }, [selected, entryIds]);

  return <div ref={ref} />;
}
