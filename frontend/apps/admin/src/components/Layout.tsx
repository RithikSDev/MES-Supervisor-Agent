import { Badge, Group, Menu, Select, Stack, Text, UnstyledButton } from "@mantine/core";
import { itsm } from "@mes/api-client";
import { IconChevronDown, IconDatabaseImport, IconLogout, IconRefreshAlert } from "@tabler/icons-react";
import { useQuery } from "@tanstack/react-query";
import dayjs from "dayjs";
import { useEffect, useState } from "react";
import { useAuth } from "react-oidc-context";
import { Outlet, useLocation, useSearchParams } from "react-router";
import { useMe } from "../auth";
import { GROUP_ROUTE, type ModuleGroup, moduleFor, moduleName } from "../modules";
import { AlertBell } from "../pages/NotificationsPage";
import { useModuleCounts } from "./ModuleGrid";
import { usePlant } from "./plant";
import { DemoTickets } from "./DemoTickets";
import { ResetDemoData } from "./ResetDemoData";
import { Sidebar } from "./Sidebar";

const SIDEBAR_COLLAPSED_KEY = "mes-sidebar-collapsed";

const HIDDEN_ROLES = ["offline_access", "uma_authorization"];
export const roleNames = (roles: string[]) => roles.filter((r) => !r.startsWith("default-") && !HIDDEN_ROLES.includes(r));

/** The MES mark (unchanged logo), on a navy tile. */
export function BrandMark({ size = 38 }: { size?: number }) {
  return (
    <span className="brand-mark" style={{ width: size, height: size }}>
      <svg width={size * 0.6} height={size * 0.6} viewBox="0 0 32 32" aria-hidden>
        <path d="M5 25V7l11 10 11-10v18" stroke="#f2a900" strokeWidth="3.2" fill="none" strokeLinejoin="round" />
      </svg>
    </span>
  );
}

function PlantClock({ tz }: { tz?: string }) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  const time = tz ? now.toLocaleTimeString("en-GB", { timeZone: tz }) : dayjs(now).format("HH:mm:ss");
  return (
    <Stack gap={0} align="flex-end">
      <Text className="mono" fw={500} size="sm">{time}</Text>
      <Text size="10px" c="dimmed">{tz ?? "local"}</Text>
    </Stack>
  );
}

type Conn = { system: string; configured: boolean; reachable: boolean };

function Connections() {
  const q = useQuery({
    queryKey: ["itsm-status"],
    queryFn: () => itsm<{ connections: Conn[] }>("/api/v1/itsm/status"),
    refetchInterval: 30_000,
    retry: false,
  });
  const label: Record<string, string> = { servicenow: "ServiceNow", jira: "Jira" };
  return (
    <span className="grp">
      {(q.data?.connections ?? []).map((c) => (
        <span key={c.system}>
          <span className="conn-dot" style={{ background: !c.configured ? "#7d8ca3" : c.reachable ? "#3dbb7b" : "#e5484d" }} />
          {label[c.system] ?? c.system} {!c.configured ? "off" : c.reachable ? "connected" : "unreachable"}
        </span>
      ))}
    </span>
  );
}

export function Layout() {
  const me = useMe();
  const auth = useAuth();
  const { pathname } = useLocation();
  const [search] = useSearchParams();
  const { plants, plant, setPlantId } = usePlant();
  const current = moduleFor(pathname);
  const currentGroup = !current
    ? (Object.entries(GROUP_ROUTE) as [ModuleGroup, string][]).find(([, route]) => route === pathname)?.[0]
    : undefined;
  const name = me.display_name ?? me.username;
  const roles = roleNames(me.roles);
  const [resetOpen, setResetOpen] = useState(false);
  const [demoOpen, setDemoOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(() => {
    try { return localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "1"; } catch { return false; }
  });
  const { counts, alertsEscalated } = useModuleCounts();
  const toggleCollapsed = () => setCollapsed((v) => {
    try { localStorage.setItem(SIDEBAR_COLLAPSED_KEY, v ? "0" : "1"); } catch { /* private browsing etc. */ }
    return !v;
  });

  // the AI Support Assistant is a full-screen app (its own sidebar and header); its agent console is a normal page
  if (pathname === "/ai-support" && !search.get("tab") && search.get("view") !== "console") {
    return <Outlet />;
  }

  return (
    <div className="shell">
      <div className="band top">
        <span className="dot" /> Live off the shop floor · support run by agentic AI <span className="dot" />
      </div>
      <div className="shell-body">
        <Sidebar permissions={me.permissions} counts={counts} alertsEscalated={alertsEscalated} collapsed={collapsed} onToggle={toggleCollapsed} />
        <div className="shell-content">
          <header className="shell-header">
            <Group justify="space-between" wrap="nowrap" gap="lg">
              {plants.length > 1 ? (
                <Select size="xs" w={220} aria-label="Plant" allowDeselect={false} value={plant?.id ?? null}
                  data={plants.map((p) => ({ value: p.id, label: `${p.code} · ${p.name}` }))} onChange={(v) => v && setPlantId(v)} />
              ) : <Text size="xs" c="dimmed" className="mono">{plant ? `${plant.code} · ${plant.name}` : "Workspace"}</Text>}
              <Group gap="lg" wrap="nowrap">
                <AlertBell />
                <PlantClock tz={plant?.timezone} />
                <Menu position="bottom-end" width={280}>
                  <Menu.Target>
                    <UnstyledButton className="pill" aria-label="Account">
                      <span className="avatar">{name.slice(0, 1).toUpperCase()}</span>
                      <span>{name}</span>
                      <span className="role">· {roles[0] ?? "user"}</span>
                      <IconChevronDown size={13} />
                    </UnstyledButton>
                  </Menu.Target>
                  <Menu.Dropdown>
                    <Menu.Label>Roles</Menu.Label>
                    <Text size="xs" px="xs" pb="xs">{roles.join(" · ") || "—"}</Text>
                    <Menu.Label>Permissions</Menu.Label>
                    <Group gap={4} px="xs" pb="xs">
                      {me.permissions.map((p) => <Badge key={p} size="xs" variant="outline" color="navy.6">{p}</Badge>)}
                    </Group>
                    {me.permissions.includes("system:reset") && (
                      <>
                        <Menu.Divider />
                        <Menu.Label>Administration</Menu.Label>
                        <Menu.Item leftSection={<IconDatabaseImport size={15} />} onClick={() => setDemoOpen(true)}>
                          Load demo tickets…
                        </Menu.Item>
                        <Menu.Item color="red" leftSection={<IconRefreshAlert size={15} />} onClick={() => setResetOpen(true)}>
                          Reset demo data…
                        </Menu.Item>
                      </>
                    )}
                  </Menu.Dropdown>
                </Menu>
                <UnstyledButton className="pill plain" onClick={() => auth.signoutRedirect()}>
                  <IconLogout size={14} /> Sign out
                </UnstyledButton>
              </Group>
            </Group>
          </header>

          <main className={`shell-main${current?.to === "/schedule" ? " wide" : ""}`}>
            {(current || currentGroup) && (
              <div className="crumbs" style={{ marginBottom: 16 }}>
                <span style={{ color: "var(--mes-navy)", fontWeight: 600 }}>{current ? moduleName(current) : currentGroup}</span>
              </div>
            )}
            <Outlet />
          </main>

          <footer className="band bottom">
            <span className="grp">
              <span>© {new Date().getFullYear()} · MES</span>
              <span className="sep" />
              <span>Confidential &amp; proprietary</span>
            </span>
            <span>Authorized use only</span>
            <Connections />
          </footer>
        </div>
      </div>

      <ResetDemoData opened={resetOpen} onClose={() => setResetOpen(false)} />
      <DemoTickets opened={demoOpen} onClose={() => setDemoOpen(false)} />
    </div>
  );
}

/** Page title: the last word is set in italic blue, like the landing page cards. */
export function PageHeader({ title, sub, right }: { title: string; sub?: React.ReactNode; right?: React.ReactNode }) {
  const i = title.lastIndexOf(" ");
  return (
    <Group justify="space-between" align="flex-end" mb="lg" wrap="nowrap">
      <div>
        <div className="page-title">
          {i > 0 ? <>{title.slice(0, i)} <em>{title.slice(i + 1)}</em></> : <em>{title}</em>}
        </div>
        {sub && <Text size="sm" c="dimmed" mt={4} maw={900}>{sub}</Text>}
      </div>
      {right}
    </Group>
  );
}
