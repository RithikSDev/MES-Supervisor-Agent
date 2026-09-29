import { Badge, Tooltip, UnstyledButton } from "@mantine/core";
import { IconChevronLeft, IconChevronRight, IconHome2 } from "@tabler/icons-react";
import { Link, useLocation } from "react-router";
import { GROUPS, GROUP_ICON, GROUP_ROUTE, moduleName, visibleModules } from "../modules";
import { BrandMark } from "./Layout";

/** Persistent left navigation — every module, grouped, always visible. Replaces the old
 * "Home lists everything, breadcrumb + Modules dropdown inside a page" pattern. */
export function Sidebar({
  permissions, counts, alertsEscalated, collapsed, onToggle,
}: {
  permissions: string[]; counts: Record<string, number | undefined>; alertsEscalated?: number;
  collapsed: boolean; onToggle: () => void;
}) {
  const { pathname } = useLocation();
  const modules = visibleModules(permissions);

  return (
    <nav className={`sidebar${collapsed ? " collapsed" : ""}`}>
      <Link to="/" className="sidebar-brand">
        <BrandMark size={collapsed ? 30 : 34} />
        {!collapsed && <span className="sidebar-brand-name">MES</span>}
      </Link>

      <Tooltip label="Home" position="right" disabled={!collapsed}>
        <Link to="/" className={`sidebar-link sidebar-home${pathname === "/" ? " active" : ""}`}>
          <IconHome2 size={17} stroke={1.8} />
          {!collapsed && <span>Home</span>}
        </Link>
      </Tooltip>

      <div className="sidebar-scroll">
        {GROUPS.map((g) => {
          const items = modules.filter((m) => m.group === g);
          if (!items.length) return null;
          const GroupIcon = GROUP_ICON[g];
          const groupActive = pathname === GROUP_ROUTE[g];
          return (
            <div key={g} className="sidebar-group">
              {collapsed ? (
                <Tooltip label={g} position="right">
                  <Link to={GROUP_ROUTE[g]} className={`sidebar-group-label collapsed${groupActive ? " active" : ""}`}>
                    <GroupIcon size={15} stroke={1.8} />
                  </Link>
                </Tooltip>
              ) : (
                <Link to={GROUP_ROUTE[g]} className={`sidebar-group-label${groupActive ? " active" : ""}`}>
                  <GroupIcon size={13} stroke={1.8} /> {g}
                </Link>
              )}
              {items.map((m) => {
                const active = pathname === m.to || pathname.startsWith(`${m.to}/`);
                const count = counts[m.to];
                const link = (
                  <Link key={m.to} to={m.to} className={`sidebar-link${active ? " active" : ""}`}>
                    <m.icon size={16} stroke={1.8} />
                    {!collapsed && (
                      <>
                        <span>{moduleName(m)}</span>
                        {count !== undefined && (
                          <Badge size="xs" variant="light" color={m.to === "/notifications" && alertsEscalated ? "red" : "brand"} ml="auto">
                            {count}
                          </Badge>
                        )}
                      </>
                    )}
                  </Link>
                );
                return collapsed ? <Tooltip key={m.to} label={moduleName(m)} position="right">{link}</Tooltip> : link;
              })}
            </div>
          );
        })}
      </div>

      <UnstyledButton className="sidebar-collapse" onClick={onToggle} aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}>
        {collapsed ? <IconChevronRight size={15} /> : <><IconChevronLeft size={15} /><span>Collapse</span></>}
      </UnstyledButton>
    </nav>
  );
}
