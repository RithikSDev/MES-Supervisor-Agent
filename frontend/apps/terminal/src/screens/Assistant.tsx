/**
 * The support assistant on the terminal: the operator describes the problem, the AI agent looks things up (machine,
 * order, SAP interface, similar tickets), walks them through the fix, and raises a ticket when it can't be fixed
 * at the line. Answers stream in; choices come as big buttons; typing works with the on-screen keyboard (gloves).
 * It needs the network: offline, or with the agent switched off, the operator falls back to the plain form.
 */
import { Group, Loader, Text } from "@mantine/core";
import { type AgentEvent, ApiError, type ChatEntry, type ChatSession, NetworkError, agent, agentStream } from "@mes/api-client";
import { IconBackspace, IconCircleCheck, IconKeyboard, IconRobot, IconSend, IconTicket } from "@tabler/icons-react";
import { useEffect, useRef, useState } from "react";

export type ChatContext = { equipment_code?: string; erp_order_number?: string; op_no?: string; screen?: string };

const ROWS = ["1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm-.,"];

function Keyboard({ onKey }: { onKey: (k: string) => void }) {
  const [upper, setUpper] = useState(false);
  return (
    <div className="t-kbd">
      {ROWS.map((row) => (
        <div key={row} className="t-kbd-row">
          {[...row].map((k) => (
            <button key={k} type="button" className="t-kbd-key" onClick={() => { onKey(upper ? k.toUpperCase() : k); setUpper(false); }}>
              {upper ? k.toUpperCase() : k}
            </button>
          ))}
        </div>
      ))}
      <div className="t-kbd-row">
        <button type="button" className={`t-kbd-key wide${upper ? " on" : ""}`} onClick={() => setUpper((u) => !u)}>Aa</button>
        <button type="button" className="t-kbd-key space" onClick={() => onKey(" ")}>space</button>
        <button type="button" className="t-kbd-key wide" aria-label="delete" onClick={() => onKey("⌫")}><IconBackspace size={26} /></button>
      </div>
    </div>
  );
}

export function AssistantChat({ context, onFallback, onClose }: {
  context: ChatContext;
  onFallback: () => void;
  onClose: () => void;
}) {
  const [session, setSession] = useState<ChatSession | null>(null);
  const [entries, setEntries] = useState<ChatEntry[]>([]);
  const [draft, setDraft] = useState("");
  const [streaming, setStreaming] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [keyboard, setKeyboard] = useState(false);
  const [unavailable, setUnavailable] = useState<string | null>(null);
  const [closed, setClosed] = useState<{ status: string; ticket: string | null } | null>(null);
  const [raised, setRaised] = useState<string | null>(null); // ticket raised: the chat stays open for details
  const scroller = useRef<HTMLDivElement>(null);
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    agent<ChatSession>("/chat/sessions", { method: "POST", body: JSON.stringify(context) })
      .then((s) => {
        setSession(s);
        setEntries(s.transcript);
        if (s.status !== "OPEN") setUnavailable(s.transcript[0]?.text ?? "The assistant is not available.");
      })
      .catch((e) => setUnavailable(e instanceof NetworkError
        ? "The assistant needs the network. The form works offline."
        : "The assistant is not available right now."));
  }, [context]);

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  }, [entries, streaming, status]);

  const send = async (text: string) => {
    const t = text.trim();
    if (!t || !session || busy || closed) return;
    setDraft("");
    setKeyboard(false);
    setBusy(true);
    setEntries((e) => [...e.map((x) => ({ ...x, choices: [] })), { role: "operator", text: t, at: new Date().toISOString() }]);
    setStreaming("");
    let said = "";
    let choices: string[] = [];
    let ticket: string | undefined;
    try {
      await agentStream(`/chat/sessions/${session.id}/messages`, { text: t }, (ev: AgentEvent) => {
        if (ev.type === "text") { said += ev.delta; setStreaming(said); setStatus(null); }
        else if (ev.type === "status") setStatus(ev.text);
        else if (ev.type === "choices") choices = ev.options;
        else if (ev.type === "ticket") { ticket = ev.number; setRaised(ev.number); }
        else if (ev.type === "closed") setClosed({ status: ev.status, ticket: ev.ticket });
        else if (ev.type === "error") {
          said = ev.status === 503 ? "The assistant has been switched off. Please use the form." : `Sorry, something went wrong (${ev.message}).`;
          if (ev.status === 503) setUnavailable(said);
        }
      });
    } catch (e) {
      said = e instanceof NetworkError ? "The connection dropped. Please use the form, it works offline." : e instanceof ApiError ? e.message : String(e);
    }
    setEntries((e) => [...e, { role: "assistant", text: said.trim() || "…", at: new Date().toISOString(), choices, ticket }]);
    setStreaming(null);
    setStatus(null);
    setBusy(false);
  };

  const key = (k: string) => setDraft((d) => (k === "⌫" ? d.slice(0, -1) : (d + k).slice(0, 500)));
  const last = entries[entries.length - 1];
  const choices = !busy && !closed && last?.role === "assistant" ? last.choices ?? [] : [];

  if (unavailable && (!session || session.status !== "OPEN")) {
    return (
      <div className="t-chat-empty">
        <IconRobot size={48} color="var(--mes-text-dim)" />
        <Text size="xl">{unavailable}</Text>
        <button className="t-btn primary" onClick={onFallback}>Use the form</button>
      </div>
    );
  }
  return (
    <div className="t-chat">
      <div className="t-chat-log" ref={scroller}>
        {!session && <Group justify="center" py="xl"><Loader color="green" /></Group>}
        {entries.map((e, i) => (
          <div key={i} className={`t-bubble ${e.role}`}>
            {e.role === "assistant" && <IconRobot size={20} className="t-bubble-icon" />}
            <div style={{ whiteSpace: "pre-wrap" }}>{e.text}</div>
            {e.ticket && <div className="t-bubble-ticket"><IconTicket size={20} /> {e.ticket}</div>}
          </div>
        ))}
        {streaming !== null && (
          <div className="t-bubble assistant">
            <IconRobot size={20} className="t-bubble-icon" />
            <div style={{ whiteSpace: "pre-wrap" }}>{streaming || (status ? "" : "…")}</div>
            {status && <div className="t-chat-status"><Loader size="xs" color="cyan" /> {status}…</div>}
          </div>
        )}
      </div>

      {closed ? (
        <div className="t-chat-closed" data-testid="chat-closed">
          {closed.status === "FIXED"
            ? <><IconCircleCheck size={32} color="var(--mes-green)" /> <span>Fixed. Recorded as {closed.ticket}.</span></>
            : <><IconTicket size={32} color="var(--mes-cyan)" /> <span>Ticket {closed.ticket} raised. Support takes over.</span></>}
          <button className="t-btn primary small" style={{ width: 200 }} onClick={onClose}>Done</button>
        </div>
      ) : unavailable ? (
        <div className="t-chat-closed"><span>{unavailable}</span><button className="t-btn primary small" style={{ width: 220 }} onClick={onFallback}>Use the form</button></div>
      ) : (
        <>
          {choices.length > 0 && (
            <div className="t-chat-choices">
              {choices.map((c) => <button key={c} className="t-choice" onClick={() => send(c)}>{c}</button>)}
            </div>
          )}
          <div className="t-chat-input">
            <input className="t-chat-text" placeholder={busy ? "The assistant is answering…" : "Type your answer or tap a button"}
              value={draft} disabled={busy || !session} onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") void send(draft); }} />
            <button className={`t-btn ghost small${keyboard ? " on" : ""}`} style={{ width: 84 }} aria-label="keyboard" onClick={() => setKeyboard((k) => !k)}>
              <IconKeyboard size={30} />
            </button>
            <button className="t-btn primary small" style={{ width: 150 }} disabled={busy || !draft.trim()} onClick={() => send(draft)}>
              <IconSend size={26} /> Send
            </button>
            {raised && <button className="t-btn ghost small" style={{ width: 150 }} disabled={busy} onClick={onClose}>Done</button>}
          </div>
          {keyboard && <Keyboard onKey={key} />}
          <Text size="sm" c="dimmed" ta="center">AI assistant: it can make mistakes. For safety issues stop the machine and call your supervisor.</Text>
        </>
      )}
    </div>
  );
}
