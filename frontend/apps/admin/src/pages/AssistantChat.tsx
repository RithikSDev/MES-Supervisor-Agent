import { ActionIcon, Alert, Button, Group, Loader, Modal, Stack, Text, TextInput, Textarea, Tooltip, UnstyledButton } from "@mantine/core";
import {
  type ChatCard, type EngineerChat, type EngineerChatRow, type EngineerEntry, type EngineerPart, agent, agentStream,
} from "@mes/api-client";
import {
  IconArrowLeft, IconArrowUp, IconLayoutSidebarLeftCollapse, IconLayoutSidebarLeftExpand, IconMessagePlus, IconRobot,
  IconSearch, IconSettings, IconTrash,
} from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, type ReactNode, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { useMe } from "../auth";
import { BrandMark } from "../components/Layout";
import { usd } from "../components/agent";
import { ChatCardView } from "../components/chatCards";
import { usePlant } from "../components/plant";
import { fromNow } from "../lib/format";

const STARTERS = ["Bring up all the open tickets", "Forecast the tickets for the next two weeks",
  "SLA breach and exit report for this week", "List the tickets assigned to other engineers",
  "Which tickets are close to missing their SLA?", "What is waiting for my approval?"];

/** Conversations grouped like a chat app: Today, Yesterday, Previous 7 days, Older. */
function groupByDay(rows: EngineerChatRow[]): [string, EngineerChatRow[]][] {
  const start = new Date();
  start.setHours(0, 0, 0, 0);
  const day = 86_400_000;
  const bucket = (at: string) => {
    const t = new Date(at).getTime();
    return t >= start.getTime() ? "Today" : t >= start.getTime() - day ? "Yesterday"
      : t >= start.getTime() - 7 * day ? "Previous 7 days" : "Older";
  };
  const order = ["Today", "Yesterday", "Previous 7 days", "Older"];
  return order.map((g) => [g, rows.filter((r) => bucket(r.last_at) === g)] as [string, EngineerChatRow[]]).filter(([, r]) => r.length);
}

type Live = { question: string; parts: EngineerPart[]; status: string | null; error: string | null };

/** Plain text with paragraphs, line breaks and **bold** (the assistant is told not to use other markdown). */
function RichText({ text }: { text: string }) {
  return (
    <>
      {text.trim().split(/\n{2,}/).map((para, i) => (
        <p key={i} className="chat-p">
          {para.split("\n").map((line, j) => (
            <Fragment key={j}>
              {j > 0 && <br />}
              {line.split(/(\*\*[^*]+\*\*)/g).map((seg, k) => (seg.startsWith("**") && seg.endsWith("**")
                ? <strong key={k}>{seg.slice(2, -2)}</strong> : <Fragment key={k}>{seg.replace(/^[-*] /, "• ")}</Fragment>))}
            </Fragment>
          ))}
        </p>
      ))}
    </>
  );
}

function AssistantTurn({ parts, chatId, onDecided, footer }: { parts: EngineerPart[]; chatId: string; onDecided: (c: ChatCard) => void; footer?: ReactNode }) {
  return (
    <div className="chat-row assistant">
      <span className="chat-avatar"><IconRobot size={16} stroke={1.8} /></span>
      <Stack gap={10} style={{ flex: 1, minWidth: 0 }}>
        {parts.map((p, i) => (p.type === "text"
          ? (p.text.trim() ? <div key={i} className="chat-text"><RichText text={p.text} /></div> : null)
          : <ChatCardView key={p.card.action_id ?? `${p.card.kind}-${i}`} card={p.card} chatId={chatId} onDecided={onDecided} />))}
        {footer}
      </Stack>
    </div>
  );
}

function EngineerTurn({ text }: { text: string }) {
  return <div className="chat-row engineer"><div className="chat-bubble">{text}</div></div>;
}

export function AssistantChat() {
  const qc = useQueryClient();
  const me = useMe();
  const { plant } = usePlant();
  const [params, setParams] = useSearchParams();
  const chatId = params.get("chat");
  const [draft, setDraft] = useState("");
  const [live, setLive] = useState<Live | null>(null);
  const [liveChatId, setLiveChatId] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const [search, setSearch] = useState("");
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(false);

  const list = useQuery({ queryKey: ["agent", "engineer-chats"], queryFn: () => agent<EngineerChatRow[]>("/engineer/chats") });
  const chat = useQuery({
    queryKey: ["agent", "engineer-chat", chatId],
    queryFn: () => agent<EngineerChat>(`/engineer/chats/${chatId}`),
    enabled: Boolean(chatId),
  });
  const status = useQuery({ queryKey: ["agent", "status"], queryFn: () => agent<{ active: boolean; key_configured: boolean }>("/status"), refetchInterval: 30_000 });
  const remove = useMutation({
    mutationFn: (id: string) => agent(`/engineer/chats/${id}`, { method: "DELETE" }),
    onSuccess: (_d, id) => {
      qc.invalidateQueries({ queryKey: ["agent", "engineer-chats"] });
      if (id === chatId) setParams({});
    },
  });

  const open = (id: string | null) => setParams(id ? { chat: id } : {});
  const busy = live !== null && live.error === null && liveChatId !== null;
  const transcript: EngineerEntry[] = chat.data?.transcript ?? [];

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [transcript.length, live?.parts.length, live?.parts[live.parts.length - 1], live?.status]);
  useEffect(() => {  // an opened conversation: its cards load their data and grow, so follow them to the end once more
    if (!chat.data) return;
    const timers = [400, 1200].map((ms) => setTimeout(() => bottom.current?.scrollIntoView({ block: "end" }), ms));
    return () => timers.forEach(clearTimeout);
  }, [chatId, chat.data?.id]);

  const onDecided = (c: ChatCard) => {
    qc.setQueryData<EngineerChat>(["agent", "engineer-chat", chatId], (old) => old && {
      ...old,
      transcript: old.transcript.map((e) => (e.role === "assistant"
        ? { ...e, parts: e.parts.map((p) => (p.type === "card" && p.card.action_id === c.action_id ? { ...p, card: c } : p)) } : e)),
    });
  };

  async function send(text: string) {
    const question = text.trim();
    if (!question || busy || !plant) return;
    setDraft("");
    setLive({ question, parts: [], status: "Thinking…", error: null });
    let id = chatId;
    try {
      if (!id) {
        const created = await agent<EngineerChat>("/engineer/chats", { method: "POST", body: JSON.stringify({ plant_id: plant.id }) });
        id = created.id;
        qc.setQueryData(["agent", "engineer-chat", id], created);
        open(id);
      }
      setLiveChatId(id);
      await agentStream(`/engineer/chats/${id}/messages`, { text: question }, (ev) => {
        setLive((l) => {
          if (!l) return l;
          if (ev.type === "text") {
            const parts = [...l.parts];
            const last = parts[parts.length - 1];
            if (last?.type === "text") parts[parts.length - 1] = { type: "text", text: last.text + ev.delta };
            else parts.push({ type: "text", text: ev.delta });
            return { ...l, parts, status: null };
          }
          if (ev.type === "card") return { ...l, parts: [...l.parts, { type: "card", card: ev.card }], status: null };
          if (ev.type === "status") return { ...l, status: ev.text };
          if (ev.type === "error") return { ...l, error: ev.message, status: null };
          return l;
        });
      });
      await qc.invalidateQueries({ queryKey: ["agent", "engineer-chat", id] });
      qc.invalidateQueries({ queryKey: ["agent", "engineer-chats"] });
      setLive((l) => (l?.error ? l : null));
    } catch (e) {
      setLive((l) => l && { ...l, error: e instanceof Error ? e.message : String(e), status: null });
    } finally {
      setLiveChatId(null);
    }
  }

  const empty = !chatId && !live;
  const off = status.data && !status.data.active;
  const title = chat.data?.title || (live && !chatId ? live.question : null);
  const rows = (list.data ?? []).filter((c) => !search.trim() || (c.title ?? "").toLowerCase().includes(search.trim().toLowerCase()));
  const groups = groupByDay(rows);
  const deleting = (list.data ?? []).find((c) => c.id === confirmDelete);

  return (
    <div className={`assistant-app${collapsed ? " collapsed" : ""}`}>
      <aside className="assistant-side" aria-label="Conversations">
        <div className="assistant-side-head">
          <Link to="/" className="assistant-brand" aria-label="Home">
            <BrandMark size={30} />
            <span>AI Support <em>Assistant</em></span>
          </Link>
          <Tooltip label="Hide conversations" withArrow>
            <ActionIcon variant="default" size="md" aria-label="Hide conversations" onClick={() => setCollapsed(true)}>
              <IconLayoutSidebarLeftCollapse size={16} />
            </ActionIcon>
          </Tooltip>
        </div>
        <div className="assistant-side-actions">
          <Button fullWidth variant="default" justify="flex-start" leftSection={<IconMessagePlus size={16} />}
            onClick={() => { setLive(null); open(null); }} disabled={busy}>New conversation</Button>
          <TextInput leftSection={<IconSearch size={14} />} placeholder="Search conversations…" value={search}
            onChange={(e) => setSearch(e.currentTarget.value)} aria-label="Search conversations" />
        </div>
        <div className="assistant-side-list">
          {groups.map(([label, items]) => (
            <div key={label}>
              <div className="label assistant-group">{label}</div>
              {items.map((c) => (
                <div key={c.id} className={`assistant-item${c.id === chatId ? " on" : ""}`}>
                  <UnstyledButton className="assistant-item-open" onClick={() => { setLive(null); open(c.id); }} disabled={busy}>
                    <Text size="sm" truncate fw={c.id === chatId ? 600 : 400}>{c.title}</Text>
                    <Text size="xs" c="dimmed" className="mono">{fromNow(c.last_at)} · {usd(c.cost_usd)}</Text>
                  </UnstyledButton>
                  <Tooltip label="Delete conversation" withArrow>
                    <ActionIcon className="assistant-item-delete" variant="subtle" color="gray" size="md"
                      aria-label={`Delete conversation ${c.title}`} onClick={() => setConfirmDelete(c.id)}>
                      <IconTrash size={15} />
                    </ActionIcon>
                  </Tooltip>
                </div>
              ))}
            </div>
          ))}
          {list.isPending && <Loader size="sm" m="md" />}
          {list.data && rows.length === 0 && <Text size="sm" c="dimmed" p="md">{search ? "No conversation matches." : "No conversations yet."}</Text>}
        </div>
      </aside>

      <section className="assistant-main">
        <header className="assistant-top">
          <Group gap={10} wrap="nowrap" style={{ minWidth: 0 }}>
            {collapsed && (
              <Tooltip label="Show conversations" withArrow>
                <ActionIcon variant="default" size="md" aria-label="Show conversations" onClick={() => setCollapsed(false)}>
                  <IconLayoutSidebarLeftExpand size={16} />
                </ActionIcon>
              </Tooltip>
            )}
            <Button component={Link} to="/" variant="default" size="xs" leftSection={<IconArrowLeft size={13} />} className="assistant-back">Home</Button>
            <Text fw={600} truncate className="assistant-title">{title ?? "New conversation"}</Text>
          </Group>
          <Group gap={8} wrap="nowrap">
            {chat.data && <Text size="xs" c="dimmed" className="mono">{usd(chat.data.cost_usd)} Claude</Text>}
            <Button component={Link} to="/ai-support?view=console" variant="default" size="xs" leftSection={<IconSettings size={14} />}>Agent console</Button>
          </Group>
        </header>
        {off && (
          <Alert color="amber" icon={<IconRobot />} m="md" mb={0}>
            {status.data?.key_configured ? "The AI kill switch is on: the assistant can't answer." : "No ANTHROPIC_API_KEY configured: the assistant is idle."}
          </Alert>
        )}
        <div className="assistant-scroll">
          <div className="assistant-thread">
            {empty ? (
              <div className="chat-empty">
                <span className="brand-mark" style={{ width: 52, height: 52 }}><IconRobot size={26} color="#fff" stroke={1.6} /></span>
                <h2 className="display" style={{ fontSize: "2.1rem", marginTop: 18 }}>How can I help, <em>{(me.display_name ?? me.username).split(" ")[0]}?</em></h2>
                <Text c="dimmed" maw={560} ta="center" mt={8}>
                  Ask about tickets, SLAs, machines, what the AI agents did or the SAP / ServiceNow / Jira interfaces. Answers
                  come with live cards; tickets are created, changed or cancelled only when you confirm.
                </Text>
                <div className="starter-grid">
                  {STARTERS.map((s) => <button key={s} type="button" className="starter" onClick={() => send(s)} disabled={off}>{s}</button>)}
                </div>
              </div>
            ) : (
              <Stack gap={22} py="lg">
                {chat.isPending && chatId && !live && <Loader size="sm" />}
                {transcript.map((e, i) => (e.role === "engineer"
                  ? <EngineerTurn key={i} text={e.text} />
                  : <AssistantTurn key={i} parts={e.parts} chatId={chatId!} onDecided={onDecided}
                      footer={i === transcript.length - 1 && e.suggestions && !live ? (
                        <Group gap={6}>{e.suggestions.map((s) => <button key={s} type="button" className="starter small" onClick={() => send(s)} disabled={off}>{s}</button>)}</Group>
                      ) : undefined} />))}
                {live && (
                  <>
                    <EngineerTurn text={live.question} />
                    <AssistantTurn parts={live.parts} chatId={liveChatId ?? chatId ?? ""} onDecided={onDecided}
                      footer={live.error ? <Text size="sm" c="red.7">{live.error}</Text>
                        : live.status ? <Group gap={8}><Loader size={14} type="dots" /><Text size="sm" c="dimmed">{live.status}</Text></Group> : undefined} />
                  </>
                )}
                <div ref={bottom} />
              </Stack>
            )}
          </div>
        </div>
        <div className="assistant-composer">
          <form className="assistant-input" onSubmit={(e) => { e.preventDefault(); send(draft); }}>
            <Textarea autosize minRows={1} maxRows={6} variant="unstyled" style={{ flex: 1 }} value={draft} disabled={off}
              placeholder={busy ? "The assistant is answering…" : "Ask about tickets, machines, SLAs… (Enter to send, Shift+Enter for a new line)"}
              aria-label="Message to the AI support assistant"
              onChange={(e) => setDraft(e.currentTarget.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(draft); } }} />
            <ActionIcon type="submit" size={40} radius="md" aria-label="Send" disabled={!draft.trim() || busy || off} loading={busy}>
              <IconArrowUp size={18} />
            </ActionIcon>
          </form>
        </div>
      </section>

      <Modal opened={Boolean(confirmDelete)} onClose={() => setConfirmDelete(null)} title="Delete conversation?" centered size="sm">
        <Text size="sm">"{deleting?.title}" and its answers are removed from your history. Tickets and changes made from it
          stay as they are.</Text>
        <Group justify="flex-end" mt="lg">
          <Button variant="default" onClick={() => setConfirmDelete(null)}>Cancel</Button>
          <Button color="red" loading={remove.isPending} onClick={() => remove.mutate(confirmDelete!, { onSuccess: () => setConfirmDelete(null) })}>Delete</Button>
        </Group>
      </Modal>
    </div>
  );
}
