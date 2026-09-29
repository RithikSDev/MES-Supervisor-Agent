/**
 * Offline action queue.
 *
 * Every shop-floor action carries a client-generated `client_event_id` and the time it happened. If the
 * network is down (or earlier actions are still waiting), the action is stored locally and replayed in order
 * once the connection is back. The server ignores replays it has already applied, so "send again" is safe.
 * Actions the server rejects (e.g. someone else completed the operation meanwhile) are kept as "failed" for the
 * operator to see and dismiss.
 */
import { ApiError, NetworkError, coreRaw, isSimulatedOutage } from "@mes/api-client";
import { useSyncExternalStore } from "react";
import { read, uuid, write } from "./storage";

export type ActionKind = "start" | "start-production" | "pause" | "resume" | "report" | "stop" | "issue" | "defect" | "breakdown" | "ticket" | "back-up";

export type ActionMeta = {
  workOrderId: string;
  operationId?: string;
  runId?: string;
  equipmentId?: string;
  phase?: "SETUP" | "RUN";
  reason?: string;
  resumeTo?: string;
  yieldQty?: number;
  scrapQty?: number;
  final?: boolean;
};

export type QueuedAction = {
  id: string;
  kind: ActionKind;
  path: string;
  body: Record<string, unknown>;
  label: string;
  createdAt: string;
  status: "pending" | "failed";
  error?: string;
  meta: ActionMeta;
};

const KEY = "mes.terminal.queue";

export function isOnline(): boolean {
  return navigator.onLine && !isSimulatedOutage();
}

class ActionQueue {
  private items: QueuedAction[] = read<QueuedAction[]>("local", KEY) ?? [];
  private listeners = new Set<() => void>();
  private flushing = false;

  snapshot = (): QueuedAction[] => this.items;

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  private set(items: QueuedAction[]) {
    this.items = items;
    write("local", KEY, items);
    this.listeners.forEach((l) => l());
  }

  get pending(): QueuedAction[] {
    return this.items.filter((i) => i.status === "pending");
  }

  /** Send now if possible, else queue. Business errors (4xx) are thrown so the operator sees them at once. */
  async submit<T>(
    kind: ActionKind, path: string, body: Record<string, unknown>, label: string, meta: ActionMeta,
  ): Promise<{ queued: boolean; result?: T }> {
    const action: QueuedAction = {
      id: (body.client_event_id as string) ?? uuid(),
      kind, path, label, meta, status: "pending", createdAt: new Date().toISOString(),
      body: { ...body },
    };
    action.body.client_event_id = action.id;
    action.body.occurred_at = action.createdAt;
    if (this.pending.length > 0 || !isOnline()) {
      this.set([...this.items, action]);
      return { queued: true };
    }
    try {
      const result = await coreRaw<T>(path, { method: "POST", body: JSON.stringify(action.body) });
      return { queued: false, result };
    } catch (e) {
      if (e instanceof NetworkError) {
        this.set([...this.items, action]);
        return { queued: true };
      }
      throw e;
    }
  }

  /** Replay queued actions in order. Stops at the first network failure or when the session has ended. */
  async flush(): Promise<void> {
    if (this.flushing || this.pending.length === 0 || !isOnline()) return;
    this.flushing = true;
    try {
      for (const action of this.pending) {
        try {
          await coreRaw(action.path, { method: "POST", body: JSON.stringify(action.body) });
          this.set(this.items.filter((i) => i.id !== action.id));
        } catch (e) {
          if (e instanceof NetworkError) break;
          if (e instanceof ApiError && e.status === 401) break; // needs a PIN login first; keep everything
          const msg = e instanceof Error ? e.message : String(e);
          this.set(this.items.map((i) => (i.id === action.id ? { ...i, status: "failed", error: msg } : i)));
        }
      }
    } finally {
      this.flushing = false;
    }
  }

  dismiss(id: string) {
    this.set(this.items.filter((i) => i.id !== id));
  }
}

export const actionQueue = new ActionQueue();

export function useActionQueue(): QueuedAction[] {
  return useSyncExternalStore(actionQueue.subscribe, actionQueue.snapshot);
}
