import { useEffect, useState } from "react";

export function Keypad({ value, onChange, decimals = false, maxLength = 7 }: {
  value: string;
  onChange: (v: string) => void;
  decimals?: boolean;
  maxLength?: number;
}) {
  const press = (k: string) => {
    if (k === "⌫") return onChange(value.slice(0, -1));
    if (k === "C") return onChange("");
    if (k === "." && (!decimals || value.includes("."))) return;
    if (value.length >= maxLength) return;
    onChange(value === "0" && k !== "." ? k : value + k);
  };
  const keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", decimals ? "." : "C", "0", "⌫"];
  return (
    <div className="t-keypad">
      {keys.map((k) => (
        <button key={k} type="button" className="t-key" onClick={() => press(k)} aria-label={k === "⌫" ? "delete" : k}>
          {k}
        </button>
      ))}
    </div>
  );
}

function fmt(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

/** Elapsed time since `since`, ticking every second. */
export function Elapsed({ since, offset = 0 }: { since: string; offset?: number }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  return <>{fmt(offset + (now - new Date(since).getTime()) / 1000)}</>;
}

export function Clock({ tz }: { tz?: string }) {
  const [now, setNow] = useState(new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  return <>{now.toLocaleTimeString("en-GB", { timeZone: tz, hour: "2-digit", minute: "2-digit" })}</>;
}

export const qty = (v: string | number | null | undefined) =>
  v === null || v === undefined ? "—" : Number(v).toLocaleString(undefined, { maximumFractionDigits: 3 });
