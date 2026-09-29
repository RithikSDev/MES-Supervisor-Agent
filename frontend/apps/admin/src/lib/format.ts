import dayjs from "dayjs";
import relativeTime from "dayjs/plugin/relativeTime";

dayjs.extend(relativeTime);

export const fmtDate = (v?: string | null) => (v ? dayjs(v).format("DD MMM") : "—");
export const fmtDateTime = (v?: string | null) => (v ? dayjs(v).format("DD MMM HH:mm") : "—");
export const fmtTime = (v?: string | null) => (v ? dayjs(v).format("HH:mm:ss") : "—");
export const fromNow = (v?: string | null) => (v ? dayjs(v).fromNow() : "—");
export const num = (v?: string | number | null, digits = 0) =>
  v === null || v === undefined ? "—" : Number(v).toLocaleString(undefined, { maximumFractionDigits: digits });

export function minutes(m: number): string {
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  const r = m % 60;
  return r ? `${h} h ${r} min` : `${h} h`;
}

/** Due = end of the ERP due date. Late if not finished and the due date has passed. */
export function isLate(dueDate: string, status: string): boolean {
  if (["COMPLETED", "CLOSED", "CANCELLED"].includes(status)) return false;
  return dayjs(dueDate).endOf("day").isBefore(dayjs());
}
