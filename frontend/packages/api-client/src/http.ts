import createClient, { type Middleware } from "openapi-fetch";
import type { components, paths } from "./core";

// The access token lives in react-oidc-context; AuthBridge pushes it here so plain modules can use it.
let accessToken: string | undefined;
export function setAccessToken(token: string | undefined) {
  accessToken = token;
}
export const getAccessToken = () => accessToken;

/**
 * Server-sent events over fetch (EventSource cannot send an Authorization header). Calls `onEvent` per event
 * and reconnects after errors until the returned stop function is called.
 */
export function streamEvents(
  path: string,
  onEvent: (event: string, data: string) => void,
  onStatus?: (connected: boolean) => void,
): () => void {
  let stopped = false;
  let controller: AbortController | null = null;
  const run = async () => {
    while (!stopped) {
      controller = new AbortController();
      try {
        const res = await fetch(`/api/core/api/v1${path}`, {
          headers: accessToken ? { Authorization: `Bearer ${accessToken}` } : {},
          signal: controller.signal,
        });
        if (!res.ok || !res.body) throw new Error(`stream HTTP ${res.status}`);
        onStatus?.(true);
        const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
        let buf = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += value;
          let idx: number;
          while ((idx = buf.indexOf("\n\n")) >= 0) {
            const block = buf.slice(0, idx);
            buf = buf.slice(idx + 2);
            let event = "message";
            const data: string[] = [];
            for (const line of block.split("\n")) {
              if (line.startsWith("event:")) event = line.slice(6).trim();
              else if (line.startsWith("data:")) data.push(line.slice(5).trim());
            }
            if (data.length) onEvent(event, data.join("\n"));
          }
        }
      } catch {
        /* reconnect below */
      }
      onStatus?.(false);
      if (!stopped) await new Promise((r) => setTimeout(r, 3000));
    }
  };
  void run();
  return () => {
    stopped = true;
    controller?.abort();
  };
}

// Demo aid: the terminal can simulate a network outage; every untyped call then fails like a dropped network.
let simulatedOutage = false;
export function setSimulatedOutage(on: boolean) {
  simulatedOutage = on;
}
export const isSimulatedOutage = () => simulatedOutage;

/** The request never got an HTTP response (offline, DNS, connection refused). Safe to queue and retry. */
export class NetworkError extends Error {}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details?: unknown,
  ) {
    super(message);
  }
}

async function toApiError(response: Response, body: unknown): Promise<ApiError> {
  const b = (body ?? {}) as { code?: string; message?: string; detail?: unknown; details?: unknown };
  let message = b.message ?? "";
  if (!message && b.detail) {
    // FastAPI HTTPException / validation errors
    message = Array.isArray(b.detail)
      ? b.detail.map((d: { loc?: unknown[]; msg?: string }) => `${(d.loc ?? []).slice(1).join(".")}: ${d.msg}`).join("; ")
      : String(b.detail);
  }
  return new ApiError(response.status, b.code ?? `http_${response.status}`, message || response.statusText, b.details);
}

const auth: Middleware = {
  onRequest({ request }) {
    if (accessToken) request.headers.set("Authorization", `Bearer ${accessToken}`);
    return request;
  },
};

export const core = createClient<paths>({ baseUrl: "/api/core" });
core.use(auth);

/** Unwraps an openapi-fetch result, throwing ApiError on failure. */
export async function unwrap<T>(p: Promise<{ data?: T; error?: unknown; response: Response }>): Promise<T> {
  const { data, error, response } = await p;
  if (!response.ok) throw await toApiError(response, error);
  return data as T;
}

/** Untyped JSON call (erp-adapter admin API, mock SAP control API, terminal actions). */
async function json<T>(base: string, path: string, init?: RequestInit & { auth?: boolean }): Promise<T> {
  const headers = new Headers(init?.headers);
  // FormData (file uploads) sets its own multipart content type
  if (init?.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (init?.auth !== false && accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  if (simulatedOutage) throw new NetworkError("network outage (simulated)");
  let response: Response;
  try {
    response = await fetch(`${base}${path}`, { ...init, headers });
  } catch (e) {
    throw new NetworkError(e instanceof Error ? e.message : String(e));
  }
  const text = await response.text();
  const body = text ? JSON.parse(text) : undefined;
  if (!response.ok) throw await toApiError(response, body);
  return body as T;
}

export const erp = <T>(path: string, init?: RequestInit) => json<T>("/api/erp", path, init);
/** Notifier API (alerts, on-call rota, rules). */
export const notify = <T>(path: string, init?: RequestInit) => json<T>("/api/notify/api/v1/notify", path, init);
export type Alert = {
  id: string; rule: string; severity: string | null; ticket_id: string | null; ticket_number: string | null; title: string;
  body: string; roles: string[]; status: "NEW" | "SENT" | "ACKED" | "CLOSED"; level: number;
  recipients: { name: string; email: string; role: string; level: number }[]; created_at: string; sent_at: string | null;
  acked_by: string | null; acked_at: string | null; closed_reason: string | null;
};
/** AI support agent API (admin: runs, approvals, policy; terminal: the assistant chat). */
export const agent = <T>(path: string, init?: RequestInit) => json<T>("/api/agent/api/v1/agent", path, init);

/** A POST answered with server-sent events (the assistant's streamed reply): `onEvent` gets each JSON event. */
export async function agentStream(path: string, body: unknown, onEvent: (ev: AgentEvent) => void): Promise<void> {
  if (simulatedOutage) throw new NetworkError("network outage (simulated)");
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
  let res: Response;
  try {
    res = await fetch(`/api/agent/api/v1/agent${path}`, { method: "POST", headers, body: JSON.stringify(body) });
  } catch (e) {
    throw new NetworkError(e instanceof Error ? e.message : String(e));
  }
  if (!res.ok || !res.body) {
    const text = await res.text();
    throw await toApiError(res, text ? JSON.parse(text) : undefined);
  }
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += value;
    let idx: number;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of block.split("\n")) if (line.startsWith("data: ")) onEvent(JSON.parse(line.slice(6)) as AgentEvent);
    }
  }
}

export type AgentEvent =
  | { type: "text"; delta: string }
  | { type: "status"; text: string }
  | { type: "choices"; options: string[] }
  | { type: "ticket"; number: string; status: string; severity: string }
  | { type: "closed"; status: string; ticket: string | null }
  | { type: "error"; status: number; message: string }
  | { type: "card"; card: ChatCard }
  | { type: "done"; cost_usd?: number };

/** Support engineer chat: what a card shows (a query, not data: the UI fetches live data) or a change to confirm. */
export type TicketFilter = {
  scope?: "open" | "all" | "resolved"; severity?: string[]; tier?: string[]; status?: string[]; category?: string[];
  sla?: "breached" | "at_risk"; text?: string; machine?: string; line?: string; assignee?: string; me?: string;
};
export type ChatCard = {
  kind: "tickets" | "ticket" | "approvals" | "sla" | "sla_report" | "forecast" | "team" | "alerts" | "ai_run" | "machines"
    | "integration" | "confirm";
  title?: string | null; filter?: TicketFilter; ticket_id?: string | null; number?: string; run_id?: string; days?: number;
  horizon_days?: number;
  machine?: string | null; system?: "sap" | "itsm" | "both";
  // confirm
  action_id?: string; action?: string; label?: string; args?: Record<string, unknown>; summary?: string;
  reason?: string | null; status?: "PENDING" | "RUNNING" | "EXECUTED" | "FAILED" | "CANCELLED"; error?: string;
  decided_by?: string; decided_at?: string; duplicates?: { number: string; title: string; status: string; ticket_id: string }[];
  // the same change on several tickets
  tickets?: { ticket_id: string; number: string; title: string; severity: string; status: string; assignee: string | null }[];
  result?: { done?: string[]; failed?: { number: string; error: string }[] } & Record<string, unknown>;
};
export type EngineerPart = { type: "text"; text: string } | { type: "card"; card: ChatCard };
export type EngineerEntry =
  | { role: "engineer"; text: string; at: string }
  | { role: "assistant"; at: string; parts: EngineerPart[]; suggestions?: string[] };
export type EngineerChat = {
  id: string; plant_id: string; user_name: string | null; title: string | null; transcript: EngineerEntry[];
  cost_usd: string; started_at: string; last_at: string;
};
export type EngineerChatRow = { id: string; title: string | null; started_at: string; last_at: string; cost_usd: string };

export type ChatEntry = { role: "assistant" | "operator"; text: string; at: string; choices?: string[]; ticket?: string };
export type ChatSession = {
  id: string; plant_id: string; user_id: string; user_name: string | null; terminal_code: string | null;
  line_code: string | null; context: Record<string, string> | null; status: "OPEN" | "FIXED" | "TICKET_CREATED" | "CLOSED";
  ticket_id: string | null; ticket_number: string | null; transcript: ChatEntry[]; cost_usd: string; started_at: string;
  last_at: string;
};
export type AgentRun = {
  id: string; kind: "TICKET" | "CHAT"; tier: "L1" | "L2" | null; trigger: string; status: string; outcome: string | null;
  ticket_id: string | null; rca: RootCause | null;
  ticket_number: string | null; chat_session_id: string | null; summary: string | null; model: string | null;
  input_tokens: number; output_tokens: number; cache_read_tokens: number; cache_write_tokens: number; cost_usd: string;
  steps: number; error: string | null; created_at: string; started_at: string | null; finished_at: string | null;
};
export type RootCause = {
  symptom: string; root_cause: string; category: string; confidence: "high" | "medium" | "low"; evidence: string[];
  fix: string; prevention?: string | null;
};
export type AgentStep = {
  id: number; seq: number; at: string; kind: "MODEL" | "TOOL" | "ACTION" | "PROPOSAL" | "ERROR"; name: string | null;
  input: unknown; output: unknown; is_error: boolean; ms: number | null;
};
export type AgentProposal = {
  id: string; run_id: string; ticket_id: string; ticket_number: string; action: string; args: Record<string, unknown>;
  summary: string; rationale: string | null; status: "PENDING" | "EXECUTING" | "EXECUTED" | "FAILED" | "REJECTED";
  created_at: string; decided_by: string | null; decided_at: string | null; decision_note: string | null;
  result: unknown; error: string | null;
};
/** Core API without the typed client: used where we must distinguish network failures (terminal queue). */
export const coreRaw = <T>(path: string, init?: RequestInit) => json<T>("/api/core/api/v1", path, init);
export const mockSap = <T>(path: string, init?: RequestInit) => json<T>("/api/mock-sap", path, { ...init, auth: false });

export type Schemas = components["schemas"];
export type WorkOrderSummary = Schemas["WorkOrderSummary"];
export type WorkOrderDetail = Schemas["WorkOrderDetail"];
export type Operation = Schemas["OperationOut"];
export type ScheduleOut = Schemas["ScheduleOut"];
export type ScheduleView = Schemas["ScheduleView"];
export type EntryOut = Schemas["EntryOut"];
export type OpInfo = Schemas["OpInfo"];
export type Conflict = Schemas["ConflictOut"];
export type Confirmation = Schemas["ConfirmationOut"];
export type AuditEntry = Schemas["AuditEntryOut"];
export type Me = Schemas["MeOut"];
export type Plant = Schemas["PlantOut"];
export type RunOut = Schemas["RunOut"];
export type QueueOut = Schemas["QueueOut"];
export type QueueItem = Schemas["QueueItemOut"];
export type MachineState = Schemas["MachineState"];
export type WorkCenterWip = Schemas["WorkCenterWip"];
export type StockRow = Schemas["StockOut"];
export type MovementRow = Schemas["MovementOut"];
export type ShortageRow = Schemas["ShortageRow"];
export type Location = Schemas["LocationOut"];
export type OrderMaterials = Schemas["OrderMaterials"];
export type ComponentStatus = Schemas["ComponentStatus"];
export type TerminalAdmin = Schemas["TerminalAdminOut"];
export type OperatorRow = Schemas["OperatorOut"];
export type TerminalInfo = Schemas["TerminalInfo"];
export type LoginOut = Schemas["LoginOut"];
export type ReasonCodes = Schemas["ReasonCodes"];
export type WorkOrderExecution = Schemas["WorkOrderExecution"];
export type ReceiptOut = Schemas["ReceiptOut"];
export type QualityMeta = Schemas["QualityMeta"];
export type InspectionPlan = Schemas["PlanOut"];
export type PlanIn = Schemas["PlanIn"];
export type Characteristic = Schemas["CharacteristicIO-Output"];
export type InspectionLot = Schemas["InspectionLotOut"];
export type InspectionLotDetail = Schemas["InspectionLotDetail"];
export type InspectionResult = Schemas["ResultOut"];
export type Nonconformance = Schemas["NcOut"];
export type NcDetail = Schemas["NcDetail"];
export type RunInspection = Schemas["RunInspection"];
export type TraceHit = Schemas["TraceHit"];
export type TraceNode = Schemas["TraceNode"];
export type TraceResult = Schemas["TraceResult"];
export type QuarantinedLot = Schemas["QuarantinedLot"];
export const itsm = <T>(path: string, init?: RequestInit) => json<T>("/api/itsm", path, init);
export type Ticket = Schemas["TicketOut"];
export type TicketDetail = Schemas["TicketDetail"];
export type TicketMeta = Schemas["TicketMeta"];
export type TicketActivity = Schemas["ActivityOut"];
export type EquipmentHealth = Schemas["EquipmentHealth"];
export type MaintNotification = Schemas["NotificationOut"];
export type MaintOrder = Schemas["MaintOrderOut"];
export type MaintOrderDetail = Schemas["MaintOrderDetail"];
export type Downtime = Schemas["DowntimeOut"];
export type MaintPlan = Schemas["MaintPlanOut"];
export type MaintPlanIn = Schemas["MaintPlanIn"];
export type SparePart = Schemas["SparePart"];
