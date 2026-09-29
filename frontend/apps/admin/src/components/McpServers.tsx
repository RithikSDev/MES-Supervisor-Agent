import {
  Alert, Badge, Button, Checkbox, Drawer, Group, PasswordInput, SimpleGrid, Stack, Switch, Table, Text, TextInput, Textarea,
} from "@mantine/core";
import { agent } from "@mes/api-client";
import { IconDatabase, IconKey, IconPlus } from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useMe } from "../auth";
import { fromNow } from "../lib/format";

type Server = {
  id: string; name: string; label: string; description: string | null; url: string; token_set: boolean; enabled: boolean;
  modes: string[]; tools: { name: string; description: string }[] | null; last_check_at: string | null;
  last_error: string | null; version: number; updated_by: string; updated_at: string;
};
type Servers = { vault_ready: boolean; vault_error: string | null; servers: Server[] };
type TestResult = { ok: boolean; error: string | null; tools: { name: string; description: string }[] };

const MODES: [string, string][] = [
  ["engineer", "AI Support Assistant (engineers)"], ["l2", "AI L2 agent"], ["ticket", "AI L1 agent"],
  ["chat", "Operator chat at the terminal"],
];

/** MCP servers the AI agents read live data from (MES database, SAP, ...): admins add, test and scope them. */
export function McpServers() {
  const isAdmin = useMe().roles.includes("admin");
  const q = useQuery({ queryKey: ["agent", "mcp-servers"], queryFn: () => agent<Servers>("/mcp-servers"), refetchInterval: 15_000 });
  const [open, setOpen] = useState<Server | "new" | null>(null);
  return (
    <Stack>
      <Group justify="space-between">
        <Text size="sm" c="dimmed" maw={900}>
          The AI support agents read live data from these systems over MCP (Model Context Protocol), read-only. Each
          server's tools are offered only to the agents ticked for it; every query is logged in the run's steps.
        </Text>
        {isAdmin && <Button leftSection={<IconPlus size={14} />} onClick={() => setOpen("new")}>Add MCP server</Button>}
      </Group>
      {q.data && !q.data.vault_ready && (
        <Alert color="amber" icon={<IconKey />}>{q.data.vault_error}. Tokens can't be stored until ITSM_SECRET_KEY is set.</Alert>
      )}
      <SimpleGrid cols={{ base: 1, md: 2 }}>
        {(q.data?.servers ?? []).map((s) => (
          <div key={s.id} className="panel" style={{ padding: 14 }}>
            <Group justify="space-between" wrap="nowrap" align="flex-start">
              <Group gap={10} wrap="nowrap">
                <span className="chat-card-icon"><IconDatabase size={15} /></span>
                <div>
                  <Text fw={600}>{s.label} <Text span size="xs" c="dimmed" className="mono">{s.name}</Text></Text>
                  <Text size="xs" c="dimmed" className="mono">{s.url}</Text>
                </div>
              </Group>
              <Group gap={6}>
                {!s.enabled ? <Badge color="gray" variant="light">off</Badge>
                  : s.last_error ? <Badge color="red" variant="light">unreachable</Badge>
                  : s.last_check_at ? <Badge color="green" variant="light">connected</Badge> : <Badge color="gray" variant="light">not used yet</Badge>}
                {isAdmin && <Button size="compact-xs" variant="default" onClick={() => setOpen(s)}>Configure</Button>}
              </Group>
            </Group>
            {s.description && <Text size="sm" mt={8}>{s.description}</Text>}
            <Group gap={4} mt={8}>
              {s.modes.map((m) => <Badge key={m} size="xs" variant="outline" color="brand">{MODES.find((x) => x[0] === m)?.[1] ?? m}</Badge>)}
            </Group>
            {s.tools && s.tools.length > 0 && (
              <Text size="xs" c="dimmed" mt={8}>Tools: <span className="mono">{s.tools.map((t) => t.name).join(", ")}</span></Text>
            )}
            <Text size="xs" c={s.last_error ? "red.7" : "dimmed"} mt={4}>
              {s.last_error ? `${s.last_error} · ` : ""}{s.last_check_at ? `checked ${fromNow(s.last_check_at)}` : "checked when an agent first uses it"}
            </Text>
          </div>
        ))}
      </SimpleGrid>
      {q.data?.servers.length === 0 && <Text c="dimmed" size="sm">No MCP servers yet.</Text>}
      <ServerDrawer server={open} onClose={() => setOpen(null)} vaultReady={q.data?.vault_ready ?? false} />
    </Stack>
  );
}

function ServerDrawer({ server, onClose, vaultReady }: { server: Server | "new" | null; onClose: () => void; vaultReady: boolean }) {
  const qc = useQueryClient();
  const existing = server && server !== "new" ? server : null;
  const [f, setF] = useState({ name: "", label: "", description: "", url: "", token: "", enabled: true, modes: ["engineer", "l2"] });
  const [result, setResult] = useState<TestResult | null>(null);
  useEffect(() => {
    if (!server) return;
    setF(existing ? { name: existing.name, label: existing.label, description: existing.description ?? "", url: existing.url,
      token: "", enabled: existing.enabled, modes: existing.modes } : { name: "", label: "", description: "", url: "", token: "",
      enabled: true, modes: ["engineer", "l2"] });
    setResult(null);
  }, [server]); // eslint-disable-line react-hooks/exhaustive-deps
  const done = () => { qc.invalidateQueries({ queryKey: ["agent", "mcp-servers"] }); onClose(); };
  const test = useMutation({
    mutationFn: () => agent<TestResult>("/mcp-servers/test", { method: "POST", body: JSON.stringify({ url: f.url, token: f.token || null, server_id: existing?.id ?? null }) }),
    onSuccess: setResult, meta: { silent: true },
  });
  const save = useMutation({
    mutationFn: () => agent(existing ? `/mcp-servers/${existing.id}` : "/mcp-servers", {
      method: existing ? "PUT" : "POST",
      body: JSON.stringify({ ...f, description: f.description || null, token: f.token || null }),
    }),
    onSuccess: done, meta: { silent: true },
  });
  const remove = useMutation({ mutationFn: () => agent(`/mcp-servers/${existing!.id}`, { method: "DELETE" }), onSuccess: done });
  const set = (k: string, v: unknown) => setF((x) => ({ ...x, [k]: v }));
  return (
    <Drawer opened={Boolean(server)} onClose={onClose} position="right" size="lg"
      title={<Text className="page-title" fz="1.4rem">MCP <em>server</em></Text>}>
      <Stack gap="md">
        <SimpleGrid cols={2}>
          <TextInput label="Label" required value={f.label} onChange={(e) => set("label", e.currentTarget.value)} placeholder="Machine data" />
          <TextInput label="Name" required description="tool prefix: lowercase, digits, _" value={f.name}
            onChange={(e) => set("name", e.currentTarget.value.toLowerCase())} placeholder="machines" />
        </SimpleGrid>
        <TextInput label="Address (streamable HTTP)" required value={f.url} onChange={(e) => set("url", e.currentTarget.value)}
          placeholder="https://mcp.example.com/mcp" />
        <PasswordInput label="Bearer token" value={f.token} onChange={(e) => set("token", e.currentTarget.value)} autoComplete="new-password"
          description={existing?.token_set ? "stored encrypted; leave empty to keep" : "optional; stored encrypted, never shown again"}
          placeholder={existing?.token_set ? "••••••••  (unchanged)" : ""} />
        <Textarea label="What it offers (shown to admins)" autosize minRows={2} value={f.description} onChange={(e) => set("description", e.currentTarget.value)} />
        <div>
          <Text size="sm" fw={500} mb={6}>Agents that may use it</Text>
          <Stack gap={6}>
            {MODES.map(([m, label]) => (
              <Checkbox key={m} label={label} checked={f.modes.includes(m)}
                onChange={(e) => set("modes", e.currentTarget.checked ? [...f.modes, m] : f.modes.filter((x) => x !== m))} />
            ))}
          </Stack>
        </div>
        <Switch label="Enabled" checked={f.enabled} onChange={(e) => set("enabled", e.currentTarget.checked)} />
        <div className="panel" style={{ padding: 14 }}>
          <Group justify="space-between" mb={result ? 8 : 0}>
            <Text fw={600}>Test connection</Text>
            <Button size="xs" variant="default" disabled={!f.url} loading={test.isPending} onClick={() => test.mutate()}>Test</Button>
          </Group>
          {result && (result.ok ? (
            <Table verticalSpacing={4}><Table.Tbody>
              {result.tools.map((t) => <Table.Tr key={t.name}><Table.Td className="mono">{t.name}</Table.Td><Table.Td><Text size="xs" lineClamp={2}>{t.description}</Text></Table.Td></Table.Tr>)}
            </Table.Tbody></Table>
          ) : <Text size="sm" c="red.7">{result.error}</Text>)}
          {!result && <Text size="xs" c="dimmed">Connects and lists the server's tools. Nothing is saved.</Text>}
        </div>
        {!vaultReady && f.token && <Alert color="amber">The token can't be stored: ITSM_SECRET_KEY is not set.</Alert>}
        {save.error && <Alert color="red">{save.error.message}</Alert>}
        <Group justify="space-between">
          {existing ? <Button color="red" variant="subtle" loading={remove.isPending} onClick={() => remove.mutate()}>Remove</Button> : <span />}
          <Group gap={8}>
            <Button variant="default" onClick={onClose}>Cancel</Button>
            <Button disabled={!f.name || !f.label || !f.url || !f.modes.length} loading={save.isPending} onClick={() => save.mutate()}>Save</Button>
          </Group>
        </Group>
      </Stack>
    </Drawer>
  );
}
