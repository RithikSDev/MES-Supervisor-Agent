import { Alert, Center, Stack, Text, TextInput } from "@mantine/core";
import { ApiError, coreRaw, type TerminalInfo } from "@mes/api-client";
import { useState } from "react";

/** Shown until a supervisor enrols this device (admin UI -> Terminals -> Enrol, then open the link here). */
export function Enroll({ onEnrolled }: { onEnrolled: (token: string, info: TerminalInfo) => void }) {
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const token = code.trim().replace(/^.*#enroll=/, "");
      const info = await coreRaw<TerminalInfo>("/terminal/device", { headers: { "X-Terminal-Device": token } });
      onEnrolled(token, info);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Cannot reach the MES server.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Center h="100vh">
      <Stack w={620} gap="lg" className="panel" p={36}>
        <div className="t-title" style={{ fontSize: 34 }}>This device is not a terminal yet</div>
        <Text size="lg" c="dimmed">
          A supervisor enrols it in the admin UI: <b>Terminals</b> → choose the terminal → <b>Enrol device</b>.
          Open the enrolment link on this device, or paste the enrolment code here.
        </Text>
        <TextInput
          size="xl"
          placeholder="Enrolment code or link"
          value={code}
          onChange={(e) => setCode(e.currentTarget.value)}
          styles={{ input: { fontFamily: "IBM Plex Mono, monospace" } }}
        />
        {error && <Alert color="red" variant="light">{error}</Alert>}
        <button className="t-btn primary" disabled={!code.trim() || busy} onClick={submit}>
          {busy ? "Checking…" : "Enrol this device"}
        </button>
      </Stack>
    </Center>
  );
}
