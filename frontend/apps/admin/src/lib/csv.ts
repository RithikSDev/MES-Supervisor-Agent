/** CSV downloads for the report cards: Excel-friendly (UTF-8 with BOM, comma separated, CRLF, quoted where needed). */

export type Cell = string | number | boolean | null | undefined;

function cell(v: Cell): string {
  const s = v == null ? "" : typeof v === "boolean" ? (v ? "yes" : "no") : String(v);
  return /[",\n\r;]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export function toCsv(rows: Cell[][]): string {
  return rows.map((r) => r.map(cell).join(",")).join("\r\n");
}

/** File name: `<name>-<yyyy-mm-dd-hhmm>.csv` (local time of the download). */
export function csvName(name: string): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  const slug = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "report";
  return `${slug}-${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}.csv`;
}

export function downloadCsv(filename: string, rows: Cell[][]): void {
  const blob = new Blob(["﻿" + toCsv(rows)], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
