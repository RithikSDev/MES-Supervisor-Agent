import {
  IconActivity,
  IconBell,
  IconBinaryTree2,
  IconBox,
  IconBuildingFactory2,
  IconCalendarTime,
  IconChartBar,
  IconClipboardList,
  IconDeviceTablet,
  IconLifebuoy,
  IconPlugConnected,
  IconRobot,
  IconRosetteDiscountCheck,
  IconServer2,
  IconShieldCheck,
  IconTool,
  IconUsers,
  type Icon,
} from "@tabler/icons-react";

export type ModuleGroup = "Production" | "Quality & Trace" | "Support" | "Admin";
export const GROUPS: ModuleGroup[] = ["Production", "Quality & Trace", "Support", "Admin"];

/** Each group is also its own landing page (Home links to these instead of listing every module directly). */
export const GROUP_ROUTE: Record<ModuleGroup, string> = {
  Production: "/production",
  "Quality & Trace": "/quality-trace",
  Support: "/support",
  Admin: "/admin",
};
export const GROUP_ICON: Record<ModuleGroup, Icon> = {
  Production: IconBuildingFactory2,
  "Quality & Trace": IconRosetteDiscountCheck,
  Support: IconLifebuoy,
  Admin: IconShieldCheck,
};
export const GROUP_DESC: Record<ModuleGroup, string> = {
  Production: "Run the floor: work orders, scheduling, live line status, materials and maintenance.",
  "Quality & Trace": "Catch it and trace it: inspections, nonconformances, lot/serial genealogy.",
  Support: "Tickets worked by people and AI, side by side — plus analytics, alerts and the team.",
  Admin: "The plumbing: SAP/ServiceNow/Jira integrations, the audit trail, terminals, the SAP demo.",
};

export type Module = {
  to: string;
  /** title in two parts: the second is set in italic blue */
  title: [string, string];
  desc: string;
  icon: Icon;
  group: ModuleGroup;
  perm?: string;
  demo?: boolean;
};

/** Every workspace module: landing-page cards, the module switcher and the breadcrumb all come from here. */
export const MODULES: Module[] = [
  { to: "/work-orders", title: ["Work", "Orders"], desc: "Orders from SAP, release, progress and confirmations.", icon: IconClipboardList, group: "Production" },
  { to: "/schedule", title: ["Schedule", "Board"], desc: "Plan operations on machines, publish with e-signature.", icon: IconCalendarTime, group: "Production" },
  { to: "/line-status", title: ["Line", "Status"], desc: "Live machine states, runs and work in progress.", icon: IconActivity, group: "Production", perm: "execution:read" },
  { to: "/materials", title: ["Materials &", "Stock"], desc: "Lot stock, movements, staging and shortages.", icon: IconBox, group: "Production", perm: "inventory:read" },
  { to: "/maintenance", title: ["Maintenance &", "Equipment"], desc: "Breakdowns, PM orders, plans, downtime, MTBF/MTTR.", icon: IconTool, group: "Production", perm: "maintenance:read" },
  { to: "/quality", title: ["Quality", "Management"], desc: "Inspections, nonconformances and dispositions.", icon: IconRosetteDiscountCheck, group: "Quality & Trace", perm: "quality:read" },
  { to: "/traceability", title: ["Traceability &", "Genealogy"], desc: "Trace lots and serials forward and backward.", icon: IconBinaryTree2, group: "Quality & Trace", perm: "trace:read" },
  { to: "/tickets", title: ["Support", "Tickets"], desc: "P1-P4 by shop-floor impact, SLA clocks, ServiceNow and Jira.", icon: IconLifebuoy, group: "Support", perm: "ticket:read" },
  { to: "/ai-support", title: ["AI Support", "Assistant"], desc: "Ask the AI support engineer about tickets, SLAs and fixes.", icon: IconRobot, group: "Support", perm: "ticket:manage" },
  { to: "/support-analytics", title: ["Support", "Analytics"], desc: "SLA met, response and resolution times, AI contribution.", icon: IconChartBar, group: "Support", perm: "ticket:read" },
  { to: "/notifications", title: ["Alerts &", "On-call"], desc: "Alerts to the on-call rota, escalation and alert rules.", icon: IconBell, group: "Support", perm: "ticket:read" },
  { to: "/support-team", title: ["Support", "Team"], desc: "Engineers tickets are assigned to, ready in ServiceNow.", icon: IconUsers, group: "Support", perm: "ticket:manage" },
  { to: "/integration", title: ["", "Integrations"], desc: "SAP postings, ServiceNow and Jira sync, retries.", icon: IconPlugConnected, group: "Admin" },
  { to: "/audit", title: ["Audit", "Trail"], desc: "Every change: who, what, when, before and after.", icon: IconShieldCheck, group: "Admin", perm: "audit:read" },
  { to: "/terminals", title: ["", "Terminals"], desc: "Shop-floor terminals, enrolment and operator PINs.", icon: IconDeviceTablet, group: "Admin", perm: "terminal:admin" },
  { to: "/sap-simulator", title: ["SAP", "Simulator"], desc: "Demo only: stands in for S/4HANA.", icon: IconServer2, group: "Admin", demo: true },
];

export const visibleModules = (permissions: string[]) => MODULES.filter((m) => !m.perm || permissions.includes(m.perm));

export const moduleFor = (path: string) => MODULES.find((m) => path === m.to || path.startsWith(`${m.to}/`));

export const moduleName = (m: Module) => m.title.filter(Boolean).join(" ");
