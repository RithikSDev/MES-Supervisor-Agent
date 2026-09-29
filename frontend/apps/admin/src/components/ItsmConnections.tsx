import {
  Alert, Anchor, Badge, Button, Checkbox, Code, Drawer, Group, PasswordInput, SimpleGrid, Stack, Switch, Text, TextInput, Timeline,
} from "@mantine/core";
import { itsm } from "@mes/api-client";
import { IconAlertTriangle, IconCheck, IconKey, IconMinus, IconX } from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { fmtDateTime } from "../lib/format";
import { Link } from "react-router";

export type System = "servicenow" | "jira";
type ConnectionView = {
  system: System; configured: boolean; url: string; username: string; secret_set: boolean; secret_set_at?: string | null;
  secret_set_by?: string | null; enabled: boolean; version?: number; updated_at?: string; updated_by?: string;
  options: Record<string, string>; error?: string | null;
};
type Change = { id: number; system: System; at: string; by: string; changes: string[]; note: string | null };
export type Connections = {
  mode: string; managed_here: boolean; vault_ready: boolean; vault_error: string | null; allow_any_host: boolean;
  servicenow: ConnectionView; jira: ConnectionView; history: Change[];
};
type Check = { name: string; ok: boolean | null; detail: string };
type TestResult = { ok: boolean; checks: Check[] };

const NAME: Record<System, string> = { servicenow: "ServiceNow", jira: "Jira Cloud" };
const FIELDS: Record<System, { key: string; label: string; hint?: string }[]> = {
  servicenow: [
    { key: "group_l1", label: "L1 assignment group", hint: "e.g. MES AI Support" },
    { key: "group_l2", label: "L2 assignment group" },
    { key: "group_l3", label: "L3 assignment group" },
    { key: "close_code", label: "Resolution code", hint: "incident.close_code used when MES resolves" },
    { key: "contact_type", label: "Contact type (optional)", hint: "a choice value of the Contact type field" },
  ],
  jira: [
    { key: "project", label: "Project key", hint: "e.g. MES" },
    { key: "issue_type_bug", label: "Issue type for bugs" },
    { key: "issue_type_task", label: "Issue type for requests" },
  ],
};

export function useConnections() {
  return useQuery({ queryKey: ["itsm-connections"], queryFn: () => itsm<Connections>("/api/v1/itsm/connections"), retry: false });
}

function CheckList({ checks }: { checks: Check[] }) {
  return (
    <Stack gap={4}>
      {checks.map((c) => (
        <Group key={c.name} gap={8} wrap="nowrap" align="flex-start">
          {c.ok === true ? <IconCheck size={16} color="var(--mes-green)" /> : c.ok === false ? <IconX size={16} color="var(--mes-red)" />
            : <IconMinus size={16} color="var(--mes-text-dim)" />}
          <Text size="sm" fw={600} w={130} style={{ flexShrink: 0 }}>{c.name}</Text>
          <Text size="sm" c={c.ok === false ? "red.7" : "dimmed"}>{c.detail}</Text>
        </Group>
      ))}
    </Stack>
  );
}

/** Set up the ServiceNow or Jira connection: test it, then save. The secret is write-only. */
export function ConnectionDrawer({ system, onClose }: { system: System | null; onClose: () => void }) {
  const qc = useQueryClient();
  const q = useConnections();
  const view = system ? q.data?.[system] : undefined;
  const [url, setUrl] = useState("");
  const [username, setUsername] = useState("");
  const [secret, setSecret] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [options, setOptions] = useState<Record<string, string>>({});
  const [tested, setTested] = useState<{ key: string; result: TestResult } | null>(null);
  const [force, setForce] = useState(false);

  useEffect(() => {
    if (!view) return;
    setUrl(view.url);
    setUsername(view.username);
    setSecret("");
    setEnabled(view.configured ? view.enabled : true);
    setOptions(view.options);
    setTested(null);
    setForce(false);
  }, [system, view?.version, view?.configured]); // eslint-disable-line react-hooks/exhaustive-deps

  const body = { url: url.trim(), username: username.trim(), secret: secret || null, enabled, options };
  const key = JSON.stringify(body);
  const current = tested?.key === key ? tested.result : null;

  const test = useMutation({
    mutationFn: (createGroups: boolean) => itsm<TestResult>(`/api/v1/itsm/connections/${system}/test`, {
      method: "POST", body: JSON.stringify({ ...body, create_groups: createGroups }) }),
    onSuccess: (result) => setTested({ key, result }),
    meta: { silent: true },
  });
  const save = useMutation({
    mutationFn: () => itsm<ConnectionView>(`/api/v1/itsm/connections/${system}`, { method: "PUT", body: JSON.stringify(body) }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["itsm-connections"] });
      qc.invalidateQueries({ queryKey: ["itsm-status"] });
      onClose();
    },
    meta: { silent: true },
  });
  const history = useMemo(() => (q.data?.history ?? []).filter((h) => h.system === system).slice(0, 8), [q.data, system]);
  if (!system) return null;
  const needSecret = !view?.secret_set;
  const vaultMissing = q.data && !q.data.vault_ready;
  const canSave = Boolean(url && username && (!needSecret || secret)) && !vaultMissing && (current?.ok || force);
  const missingGroups = system === "servicenow" && current?.checks.some((c) => c.name.endsWith("group") && c.ok === false);

  return (
    <Drawer opened={Boolean(system)} onClose={onClose} position="right" size="lg"
      title={<Text className="page-title" fz="1.4rem">{NAME[system]} <em>connection</em></Text>}>
      <Stack gap="md">
        {vaultMissing && (
          <Alert color="amber" icon={<IconKey />} title="Encryption key missing">
            {q.data?.vault_error} The password / token is stored encrypted with it. Create it once, then restart the ITSM
            containers: <Code block mt={6}>cd services/itsm-adapter{"\n"}uv run python -m itsm_adapter.keygen{"\n"}docker compose up -d itsm-adapter-api itsm-adapter-worker</Code>
          </Alert>
        )}
        {view?.error && <Alert color="red" icon={<IconAlertTriangle />}>{view.error}</Alert>}
        <TextInput label={system === "servicenow" ? "Instance address" : "Site address"} required value={url}
          placeholder={system === "servicenow" ? "https://dev12345.service-now.com" : "https://your-site.atlassian.net"}
          onChange={(e) => setUrl(e.currentTarget.value)} />
        <SimpleGrid cols={2}>
          <TextInput label={system === "servicenow" ? "Integration user" : "Account e-mail"} required value={username}
            description={system === "servicenow" ? "user ID of a user with role itil" : "the account the API token belongs to"}
            onChange={(e) => setUsername(e.currentTarget.value)} autoComplete="off" />
          <PasswordInput label={system === "servicenow" ? "Password" : "API token"} required={needSecret} value={secret}
            description={view?.secret_set ? `stored ${fmtDateTime(view.secret_set_at)} by ${view.secret_set_by}; leave empty to keep` : "stored encrypted; never shown again"}
            placeholder={view?.secret_set ? "••••••••  (unchanged)" : ""} onChange={(e) => setSecret(e.currentTarget.value)}
            autoComplete="new-password" />
        </SimpleGrid>
        <SimpleGrid cols={2}>
          {FIELDS[system].map((f) => (
            <TextInput key={f.key} label={f.label} description={f.hint} value={options[f.key] ?? ""}
              required={!f.label.includes("optional")}
              onChange={(e) => { const v = e.currentTarget.value; setOptions((o) => ({ ...o, [f.key]: v })); }} />
          ))}
        </SimpleGrid>
        <Switch label={`Use this connection (off = tickets wait in MES and go out once it is on again)`} checked={enabled}
          onChange={(e) => setEnabled(e.currentTarget.checked)} />

        <div className="panel" style={{ padding: 14 }}>
          <Group justify="space-between" mb={current || test.error ? 10 : 0}>
            <Text fw={600}>Test connection</Text>
            <Group gap={6}>
              {missingGroups && <Button size="xs" variant="light" loading={test.isPending && test.variables} onClick={() => test.mutate(true)}>Create missing groups</Button>}
              <Button size="xs" variant="default" loading={test.isPending && !test.variables} disabled={!url || !username}
                onClick={() => test.mutate(false)}>Test</Button>
            </Group>
          </Group>
          {current && <CheckList checks={current.checks} />}
          {tested && !current && <Text size="xs" c="dimmed">Settings changed since the last test: test again.</Text>}
          {test.error && <Text size="sm" c="red.7">{test.error.message}</Text>}
          {!tested && !test.error && <Text size="xs" c="dimmed">Signs in and checks the groups, work-note access{system === "jira" ? ", project and issue types" : ""}. Nothing is saved.</Text>}
        </div>

        {system === "servicenow" && view?.configured && <SupportUsers />}

        {save.error && <Alert color="red">{save.error.message}</Alert>}
        <Group justify="space-between">
          <Checkbox size="xs" label="Save without a successful test" checked={force} onChange={(e) => setForce(e.currentTarget.checked)} />
          <Group gap={8}>
            <Button variant="default" onClick={onClose}>Cancel</Button>
            <Button disabled={!canSave} loading={save.isPending} onClick={() => save.mutate()}>Save</Button>
          </Group>
        </Group>
        {view?.configured && url.trim() && url.trim().replace(/\/$/, "") !== view.url && (
          <Alert color="amber" icon={<IconAlertTriangle />}>
            A different instance: tickets already sent to {view.url} stay there (their later changes are not synced);
            new tickets go to the new one.
          </Alert>
        )}

        {history.length > 0 && (
          <div>
            <div className="label" style={{ marginBottom: 8 }}>Changes</div>
            <Timeline bulletSize={10} lineWidth={1}>
              {history.map((h) => (
                <Timeline.Item key={h.id} title={<Text size="sm">{h.by} <Text span size="xs" c="dimmed">{fmtDateTime(h.at)}</Text></Text>}>
                  <Group gap={4}>{h.changes.map((c) => <Badge key={c} size="xs" variant="outline" color="navy.6" tt="none">{c.replace("options.", "")}</Badge>)}</Group>
                  {h.note && <Text size="xs" c="dimmed" mt={2}>{h.note}</Text>}
                </Timeline.Item>
              ))}
            </Timeline>
          </div>
        )}
      </Stack>
    </Drawer>
  );
}

/** The support team's ServiceNow readiness lives on the Support team page. */
function SupportUsers() {
  return (
    <div className="panel" style={{ padding: 14 }}>
      <Text fw={600}>Support team in ServiceNow</Text>
      <Text size="xs" c="dimmed">Check and fix which support engineers can be picked as Assigned to (user, itil role, group) on
        the <Anchor component={Link} to="/support-team" size="xs">Support team</Anchor> page.</Text>
    </div>
  );
}
