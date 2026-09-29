import { Center, Group, Stack, Text } from "@mantine/core";
import { ApiError, type LoginOut, NetworkError, type TerminalInfo, coreRaw } from "@mes/api-client";
import { useEffect, useState } from "react";
import { Clock } from "../components/ui";
import { actionQueue, useActionQueue } from "../state/queue";

/** Badge stand-in: the operator types a 4-6 digit PIN. */
export function PinPad({ device, info, onLogin }: { device: string; info: TerminalInfo; onLogin: (s: LoginOut) => void }) {
  const [pin, setPin] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [lockedFor, setLockedFor] = useState(0);
  const [busy, setBusy] = useState(false);
  const queued = useActionQueue().filter((a) => a.status === "pending").length;

  useEffect(() => {
    if (lockedFor <= 0) return;
    const t = setTimeout(() => setLockedFor((s) => s - 1), 1000);
    return () => clearTimeout(t);
  }, [lockedFor]);

  const submit = async (value: string) => {
    setBusy(true);
    setError(null);
    try {
      const s = await coreRaw<LoginOut>("/terminal/login", {
        method: "POST",
        body: JSON.stringify({ pin: value }),
        headers: { "X-Terminal-Device": device },
      });
      onLogin(s);
      void actionQueue.flush();
    } catch (e) {
      if (e instanceof ApiError && e.status === 429) {
        setLockedFor(Number((e.details as { retry_after?: number })?.retry_after ?? 60));
        setError(e.message);
      } else if (e instanceof NetworkError) {
        setError("No connection to the MES server. Try again in a moment.");
      } else {
        setError(e instanceof ApiError ? e.message : String(e));
      }
      setPin("");
    } finally {
      setBusy(false);
    }
  };

  const press = (k: string) => {
    if (busy || lockedFor > 0) return;
    setError(null);
    if (k === "⌫") return setPin((p) => p.slice(0, -1));
    if (k === "OK") return pin.length >= 4 && submit(pin);
    if (pin.length < 6) setPin((p) => p + k);
  };

  return (
    <Center h="100vh">
      <Group gap={64} align="center" wrap="nowrap">
        <Stack gap={6} w={420}>
          <Text className="label" style={{ fontSize: 16 }}>{info.plant_code} · line {info.line_code}</Text>
          <div className="t-title" style={{ fontSize: 46 }}>{info.name}</div>
          <div className="t-big" style={{ fontSize: 72, color: "#fff" }}><Clock tz={info.plant_timezone} /></div>
          <Text size="xl" c="dimmed" mt="md">Enter your PIN (or scan your badge).</Text>
          {queued > 0 && (
            <Text size="lg" c="amber.4" mt="sm">
              {queued} action{queued === 1 ? "" : "s"} recorded offline will be sent after the next login.
            </Text>
          )}
        </Stack>
        <Stack w={340} gap="md">
          <div className={`t-display${pin ? " focused" : ""}`} style={{ justifyContent: "center", letterSpacing: "0.4em" }}>
            {pin.replace(/./g, "●") || <span style={{ color: "var(--mes-text-dim)", fontSize: 22, letterSpacing: 0 }}>PIN</span>}
          </div>
          <div className="t-keypad">
            {["1", "2", "3", "4", "5", "6", "7", "8", "9", "⌫", "0", "OK"].map((k) => (
              <button
                key={k}
                className="t-key"
                style={k === "OK" ? { background: "#1d5b3c", borderColor: "var(--mes-green)", fontFamily: "Barlow Condensed", fontWeight: 700 } : undefined}
                disabled={busy || lockedFor > 0 || (k === "OK" && pin.length < 4)}
                onClick={() => press(k)}
              >
                {k}
              </button>
            ))}
          </div>
          <Text c={lockedFor > 0 ? "amber.4" : "red.4"} size="lg" mih={28} ta="center">
            {lockedFor > 0 ? `Locked — try again in ${lockedFor} s` : error}
          </Text>
        </Stack>
      </Group>
    </Center>
  );
}
