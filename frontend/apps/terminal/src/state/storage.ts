/** Storage helpers: storage can be unavailable (private mode, kiosk lockdown), so every access is guarded. */

export function read<T>(store: "local" | "session", key: string): T | null {
  try {
    const raw = (store === "local" ? localStorage : sessionStorage).getItem(key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}

export function write(store: "local" | "session", key: string, value: unknown): void {
  try {
    const s = store === "local" ? localStorage : sessionStorage;
    if (value === null || value === undefined) s.removeItem(key);
    else s.setItem(key, JSON.stringify(value));
  } catch {
    /* not persisted; the terminal still works for this page load */
  }
}

/** RFC 4122 v4. crypto.randomUUID needs a secure context, which a kiosk on http://<lan-ip> is not. */
export function uuid(): string {
  if (typeof crypto.randomUUID === "function" && window.isSecureContext) return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}
