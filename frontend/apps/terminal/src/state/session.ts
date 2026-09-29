import { type LoginOut, type TerminalInfo, setAccessToken } from "@mes/api-client";
import { read, write } from "./storage";

const DEVICE = "mes.terminal.device";
const INFO = "mes.terminal.info";
const SESSION = "mes.terminal.session";

// ---- device enrolment (survives restarts: localStorage)

export function readDeviceToken(): string | null {
  return read<string>("local", DEVICE);
}

export function saveDevice(token: string | null, info: TerminalInfo | null = null): void {
  write("local", DEVICE, token);
  write("local", INFO, info);
}

export function cachedTerminalInfo(): TerminalInfo | null {
  return read<TerminalInfo>("local", INFO);
}

export function saveTerminalInfo(info: TerminalInfo): void {
  write("local", INFO, info);
}

/** Enrolment links look like http://terminal/#enroll=<token>; the fragment never reaches a server. */
export function consumeEnrollFragment(): string | null {
  const m = window.location.hash.match(/^#enroll=([A-Za-z0-9_-]+)$/);
  if (!m) return null;
  history.replaceState(null, "", window.location.pathname);
  return m[1];
}

// ---- operator session (this browser tab only: sessionStorage)

export type Session = Pick<LoginOut, "access_token" | "expires_at" | "operator" | "terminal" | "roles">;

export function loadSession(): Session | null {
  const s = read<Session>("session", SESSION);
  if (!s || new Date(s.expires_at).getTime() <= Date.now()) {
    write("session", SESSION, null);
    return null;
  }
  setAccessToken(s.access_token);
  return s;
}

export function saveSession(s: Session | null): void {
  write("session", SESSION, s);
  setAccessToken(s?.access_token);
}
