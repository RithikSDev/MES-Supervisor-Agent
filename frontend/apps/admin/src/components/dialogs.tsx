import { Alert, Button, Checkbox, Group, Modal, Stack, Text, Textarea } from "@mantine/core";
import { IconSignature } from "@tabler/icons-react";
import dayjs from "dayjs";
import { useEffect, useState } from "react";
import { useMe } from "../auth";

export function ReasonModal({
  opened,
  title,
  label = "Reason",
  required = true,
  confirmLabel = "Confirm",
  busy,
  onClose,
  onSubmit,
}: {
  opened: boolean;
  title: string;
  label?: string;
  required?: boolean;
  confirmLabel?: string;
  busy?: boolean;
  onClose: () => void;
  onSubmit: (reason: string) => void;
}) {
  const [reason, setReason] = useState("");
  useEffect(() => {
    if (opened) setReason("");
  }, [opened]);
  return (
    <Modal opened={opened} onClose={onClose} title={<Text fw={600}>{title}</Text>} centered>
      <Stack>
        <Textarea
          label={label}
          autosize
          minRows={2}
          data-autofocus
          value={reason}
          onChange={(e) => setReason(e.currentTarget.value)}
          description="Recorded in the audit trail."
        />
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button loading={busy} disabled={required && !reason.trim()} onClick={() => onSubmit(reason.trim())}>
            {confirmLabel}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

const VERB: Record<string, string> = { AUTHORED: "author", REVIEWED: "review", APPROVED: "approve", RELEASED: "release" };

/** Electronic signature (ISO 9001 level): the signer, the meaning, the time and a reason. */
export function SignatureModal({
  opened,
  meaning,
  subject,
  busy,
  error,
  onClose,
  onSign,
}: {
  opened: boolean;
  meaning: string;
  subject: string;
  busy?: boolean;
  error?: string | null;
  onClose: () => void;
  onSign: (reason: string) => void;
}) {
  const me = useMe();
  const [reason, setReason] = useState("");
  const [ack, setAck] = useState(false);
  useEffect(() => {
    if (opened) {
      setReason("");
      setAck(false);
    }
  }, [opened]);
  return (
    <Modal
      opened={opened}
      onClose={onClose}
      centered
      size="lg"
      title={
        <Group gap={8}>
          <IconSignature size={18} color="var(--mes-amber)" />
          <Text fw={600}>Electronic signature</Text>
        </Group>
      }
    >
      <Stack>
        <div className="panel" style={{ padding: 12 }}>
          <Group justify="space-between" align="flex-start">
            <div>
              <div className="label">Signer</div>
              <Text fw={600}>{me.display_name ?? me.username}</Text>
              <Text size="xs" c="dimmed" className="mono">{me.username}</Text>
            </div>
            <div>
              <div className="label">Meaning</div>
              <Text fw={600} c="amber.8">{meaning}</Text>
            </div>
            <div>
              <div className="label">Time</div>
              <Text className="mono">{dayjs().format("DD MMM YYYY HH:mm")}</Text>
            </div>
          </Group>
          <div className="label" style={{ marginTop: 10 }}>Subject</div>
          <Text size="sm">{subject}</Text>
        </div>
        <Textarea
          label="Reason for signing"
          autosize
          minRows={2}
          data-autofocus
          value={reason}
          onChange={(e) => setReason(e.currentTarget.value)}
        />
        <Checkbox
          checked={ack}
          onChange={(e) => setAck(e.currentTarget.checked)}
          label="I confirm this signature is my own and I understand what I am approving."
        />
        {error && <Alert color="red" variant="light">{error}</Alert>}
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button loading={busy} disabled={!reason.trim() || !ack} onClick={() => onSign(reason.trim())}>
            Sign &amp; {VERB[meaning] ?? meaning.toLowerCase()}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
