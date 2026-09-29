import { Badge, Group, Text, Tooltip } from "@mantine/core";
import { IconCircleCheck, IconLoader2, IconRefresh, IconX } from "@tabler/icons-react";

// Normal lifecycle states stay grey/quiet; only states that need attention get colour.
const WO_STYLE: Record<string, { color: string; variant: "outline" | "light" | "filled" | "dot" }> = {
  IMPORTED: { color: "navy.5", variant: "outline" },
  RELEASED: { color: "navy.6", variant: "outline" },
  SCHEDULED: { color: "navy.6", variant: "light" },
  DISPATCHED: { color: "cyan", variant: "outline" },
  IN_PROGRESS: { color: "blue", variant: "light" },
  COMPLETED: { color: "green", variant: "outline" },
  CLOSED: { color: "navy.5", variant: "light" },
  ON_HOLD: { color: "amber", variant: "filled" },
  CANCELLED: { color: "red", variant: "light" },
};

export function WoStatus({ status }: { status: string }) {
  const s = WO_STYLE[status] ?? { color: "gray", variant: "outline" };
  return (
    <Badge color={s.color} variant={s.variant} size="sm">
      {status.replace("_", " ")}
    </Badge>
  );
}

export function OpStatus({ status }: { status: string }) {
  const color = status === "CONFIRMED" ? "green" : status === "IN_PROGRESS" ? "blue" : "navy.6";
  return (
    <Badge color={color} variant={status === "PENDING" ? "outline" : "light"} size="sm">
      {status.replace("_", " ")}
    </Badge>
  );
}

export function ErpSync({
  status,
  group,
  count,
  error,
  reconciled,
}: {
  status: string;
  group?: string | null;
  count?: string | null;
  error?: string | null;
  reconciled?: boolean;
}) {
  if (status === "POSTED") {
    return (
      <Group gap={6} wrap="nowrap">
        <IconCircleCheck size={15} color="var(--mes-green)" />
        <Text className="mono" c="green.7">
          {group}/{count}
        </Text>
        {reconciled && (
          <Tooltip label="SAP had already committed this posting after a timeout. The adapter found it and did not post again.">
            <Badge size="xs" color="amber" variant="outline" leftSection={<IconRefresh size={10} />}>
              reconciled
            </Badge>
          </Tooltip>
        )}
      </Group>
    );
  }
  if (status === "PENDING") {
    return (
      <Group gap={6} wrap="nowrap" className="pulse">
        <IconLoader2 size={15} color="var(--mes-amber)" />
        <Text size="xs" c="amber.8">sending to SAP</Text>
      </Group>
    );
  }
  if (status === "FAILED") {
    return (
      <Tooltip label={error ?? "failed"} multiline w={360}>
        <Group gap={6} wrap="nowrap">
          <IconX size={15} color="var(--mes-red)" />
          <Text size="xs" c="red.7">SAP rejected</Text>
        </Group>
      </Tooltip>
    );
  }
  return <Text size="xs" c="dimmed">not required</Text>;
}

const OUTBOUND_COLORS: Record<string, string> = {
  PENDING: "navy.6",
  SENDING: "amber",
  RETRY_WAIT: "amber",
  POSTED: "green",
  SYNCED: "green",
  SKIPPED: "gray",
  FAILED: "red",
  DEAD: "red",
};

export function OutboundStatus({ status }: { status: string }) {
  return (
    <Badge color={OUTBOUND_COLORS[status] ?? "gray"} variant={status === "POSTED" ? "outline" : "light"} size="sm">
      {status.replace("_", " ")}
    </Badge>
  );
}
