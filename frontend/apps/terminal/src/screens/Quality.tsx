/**
 * Quality on the terminal: first-off inspection entry (measured values / OK-not OK per sample) and defect reports.
 *
 * Inspection results go straight to the server (they are evaluated there, and a failure puts the order on hold,
 * so the operator must see the verdict now). Defect reports go through the offline queue like other actions.
 */
import { Group, Modal, SimpleGrid, Stack, Text } from "@mantine/core";
import { type InspectionLotDetail, type QualityMeta, coreRaw } from "@mes/api-client";
import { IconAlertTriangle, IconCheck, IconX } from "@tabler/icons-react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Keypad, qty } from "../components/ui";
import { isOnline } from "../state/queue";

type Cell = { value?: string; ok?: boolean };
const key = (c: string, n: number) => `${c}:${n}`;

export function InspectionDialog({ id, onClose, onDone }: { id: string; onClose: () => void; onDone: (d: InspectionLotDetail) => void }) {
  const lot = useQuery({
    queryKey: ["inspection", id],
    queryFn: () => coreRaw<InspectionLotDetail>(`/quality/inspection-lots/${id}`),
  });
  const d = lot.data;
  const [cells, setCells] = useState<Record<string, Cell>>({});
  const [focus, setFocus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const rows = d ? d.characteristics.flatMap((c) => Array.from({ length: d.sample_size }, (_, i) => ({ c, n: i + 1 }))) : [];
  const current = rows.find((r) => key(r.c.id!, r.n) === focus) ?? rows.find((r) => !cells[key(r.c.id!, r.n)]) ?? rows[0];
  const cur = current ? key(current.c.id!, current.n) : "";
  const complete = rows.length > 0 && rows.every((r) => {
    const v = cells[key(r.c.id!, r.n)];
    return r.c.kind === "QUANTITATIVE" ? Boolean(v?.value) : v?.ok !== undefined;
  });
  const next = () => {
    const i = rows.findIndex((r) => key(r.c.id!, r.n) === cur);
    const after = rows[i + 1];
    if (after) setFocus(key(after.c.id!, after.n));
  };
  const submit = async () => {
    if (!d) return;
    if (!isOnline()) {
      setError("No connection: inspection results need the server. Try again when the terminal is online.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const results = rows.map((r) => {
        const v = cells[key(r.c.id!, r.n)];
        return r.c.kind === "QUANTITATIVE"
          ? { characteristic_id: r.c.id, sample_no: r.n, measured_value: v.value }
          : { characteristic_id: r.c.id, sample_no: r.n, attribute_ok: v.ok };
      });
      onDone(await coreRaw<InspectionLotDetail>(`/quality/inspection-lots/${id}/results`, { method: "POST", body: JSON.stringify({ results }) }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal opened onClose={onClose} size={1100} centered
      title={<span className="t-title" style={{ fontSize: 30 }}>First-off inspection {d?.number} · {d?.plan_name}</span>}>
      {!d ? <Text size="xl">Loading…</Text> : (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 360px", gap: 20 }}>
          <Stack gap={8}>
            {rows.map(({ c, n }) => {
              const k = key(c.id!, n);
              const v = cells[k];
              const lim = c.kind === "QUANTITATIVE" ? `${qty(c.lower_limit)} … ${qty(c.upper_limit)} ${c.uom ?? ""}` : "OK / not OK";
              return (
                <button key={k} className={`t-choice${k === cur ? " selected" : ""}`} onClick={() => setFocus(k)}>
                  <Group justify="space-between" wrap="nowrap">
                    <div>
                      <Text size="lg" fw={600}>{c.name}{d.sample_size > 1 && <Text span c="dimmed"> · sample {n}</Text>}</Text>
                      <Text size="sm" c="dimmed" className="mono">{lim}</Text>
                    </div>
                    <span className="t-big" style={{ fontSize: 26 }}>
                      {c.kind === "QUANTITATIVE" ? (v?.value ?? "—") : v?.ok === undefined ? "—" : v.ok ? "OK" : "NOT OK"}
                    </span>
                  </Group>
                </button>
              );
            })}
            {error && <Text c="red.4" size="lg">{error}</Text>}
          </Stack>
          <Stack gap="sm">
            {current?.c.kind === "QUANTITATIVE" ? (
              <>
                <div className="t-display focused">{cells[cur]?.value || "0"}</div>
                <Keypad decimals maxLength={9} value={cells[cur]?.value ?? ""} onChange={(v) => setCells((p) => ({ ...p, [cur]: { value: v } }))} />
                <button className="t-btn small" disabled={!cells[cur]?.value} onClick={next}>Next</button>
              </>
            ) : current ? (
              <SimpleGrid cols={1} spacing={12}>
                <button className="t-btn primary" onClick={() => { setCells((p) => ({ ...p, [cur]: { ok: true } })); next(); }}><IconCheck size={30} /> OK</button>
                <button className="t-btn danger" onClick={() => { setCells((p) => ({ ...p, [cur]: { ok: false } })); next(); }}><IconX size={30} /> Not OK</button>
              </SimpleGrid>
            ) : null}
            <button className="t-btn primary" disabled={!complete || busy} onClick={submit} style={{ marginTop: "auto" }}>
              <IconCheck size={30} /> Save inspection
            </button>
            <Text size="sm" c="dimmed">A value outside tolerance fails the inspection and puts the order on quality hold.</Text>
          </Stack>
        </div>
      )}
    </Modal>
  );
}

export function useQualityMeta() {
  return useQuery({ queryKey: ["quality-meta"], queryFn: () => coreRaw<QualityMeta>("/quality/meta"), staleTime: Infinity });
}

export function DefectDialog({ uom, busy, onClose, onConfirm }: {
  uom: string;
  busy: boolean;
  onClose: () => void;
  onConfirm: (code: string, label: string, severity: string, quantity: number) => void;
}) {
  const meta = useQualityMeta();
  const [code, setCode] = useState<string | null>(null);
  const [severity, setSeverity] = useState("MINOR");
  const [amount, setAmount] = useState("1");
  const codes = meta.data?.defect_codes ?? [];
  return (
    <Modal opened onClose={onClose} size={1100} centered title={<span className="t-title" style={{ fontSize: 30 }}>Report a defect</span>}>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 360px", gap: 20 }}>
        <Stack gap="sm">
          <Text className="label" style={{ fontSize: 14 }}>What is wrong?</Text>
          <SimpleGrid cols={2} spacing={8}>
            {codes.map((c) => (
              <button key={c.code} className={`t-choice${code === c.code ? " selected" : ""}`} onClick={() => setCode(c.code)} style={{ minHeight: 60, fontSize: 18 }}>
                {c.label}
              </button>
            ))}
          </SimpleGrid>
          <Text className="label" style={{ fontSize: 14 }}>How bad?</Text>
          <SimpleGrid cols={3} spacing={8}>
            {["MINOR", "MAJOR", "CRITICAL"].map((s) => (
              <button key={s} className={`t-choice${severity === s ? " selected" : ""}`} onClick={() => setSeverity(s)}
                style={{ minHeight: 60, fontSize: 18, borderColor: s === "CRITICAL" && severity === s ? "var(--mes-red)" : undefined }}>
                {s.toLowerCase()}
              </button>
            ))}
          </SimpleGrid>
          {severity === "CRITICAL" && (
            <Group gap={8}><IconAlertTriangle color="var(--mes-red)" /><Text c="red.4" size="lg">Critical: the order goes on quality hold.</Text></Group>
          )}
        </Stack>
        <Stack gap="sm">
          <Text className="label" style={{ fontSize: 14 }}>Affected quantity ({uom})</Text>
          <div className="t-display focused">{amount || "0"}</div>
          <Keypad value={amount} onChange={setAmount} />
          <button className="t-btn warn" disabled={!code || busy || !Number(amount)}
            onClick={() => code && onConfirm(code, codes.find((c) => c.code === code)?.label ?? code, severity, Number(amount))}>
            <IconAlertTriangle size={30} /> Report
          </button>
        </Stack>
      </div>
    </Modal>
  );
}
