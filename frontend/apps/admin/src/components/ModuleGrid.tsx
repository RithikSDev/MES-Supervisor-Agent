import { Badge, SimpleGrid } from "@mantine/core";
import { type AgentProposal, agent, core, notify, unwrap } from "@mes/api-client";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import { useMe } from "../auth";
import type { Module } from "../modules";
import { usePlant } from "./plant";

/** Open-ticket / pending-approval / alert counts shown as badges on module cards — shared by Home and the category pages.
 * `enabled` skips the queries on category pages whose modules never use these counts (only Support's do). */
export function useModuleCounts(enabled = true) {
  const me = useMe();
  const { plant } = usePlant();
  const pid = plant?.id;
  const canTickets = me.permissions.includes("ticket:read");
  const canAgent = me.permissions.includes("ticket:manage");
  const tickets = useQuery({
    queryKey: ["tickets", pid, "open"],
    queryFn: () => unwrap(core.GET("/api/v1/tickets", { params: { query: { plant_id: pid!, open_only: true } } })),
    enabled: enabled && Boolean(pid) && canTickets,
    refetchInterval: 15_000,
  });
  const proposals = useQuery({
    queryKey: ["agent", "proposals", "PENDING"],
    queryFn: () => agent<AgentProposal[]>("/proposals?status=PENDING"),
    enabled: enabled && canAgent,
    refetchInterval: 15_000,
    retry: false,
  });
  const alerts = useQuery({
    queryKey: ["notify", "summary"],
    queryFn: () => notify<{ open: number; escalated: number }>("/summary"),
    enabled: enabled && canTickets,
    refetchInterval: 15_000,
    retry: false,
  });
  const openTickets = tickets.data ?? [];
  const counts: Record<string, number | undefined> = {
    "/tickets": openTickets.length || undefined,
    "/ai-support": proposals.data?.length || undefined,
    "/notifications": alerts.data?.open || undefined,
  };
  return { counts, openTickets, proposals: proposals.data ?? [], ticketsPending: tickets.isPending, alertsEscalated: alerts.data?.escalated };
}

/** The module-card grid used by each category page. `cols` = the group's own module count (capped for
 * sanity) so the row always divides evenly — no lone card stranded alone on its own row with a wide void
 * beside it (what a plain auto-fit grid does whenever the count doesn't fill a full row). */
export function ModuleCardGrid({ items, allModules, counts, alertsEscalated }: {
  items: Module[]; allModules: Module[]; counts: Record<string, number | undefined>; alertsEscalated?: number;
}) {
  return (
    <SimpleGrid cols={{ base: 1, sm: Math.min(items.length, 2), lg: Math.min(items.length, 5) }} spacing={22} className="rise">
      {items.map((m) => {
        const n = allModules.indexOf(m) + 1;
        const count = counts[m.to];
        return (
          <Link key={m.to} to={m.to} className={`module-card${m.demo ? " demo" : ""}`} aria-label={m.title.filter(Boolean).join(" ")}>
            <span className="icon"><m.icon size={22} stroke={1.7} /></span>
            <span className="num">{String(n).padStart(2, "0")}</span>
            {count !== undefined && (
              <Badge className="count" size="sm" variant="light" color={m.to === "/notifications" && alertsEscalated ? "red" : "brand"}>
                {count} {m.to === "/ai-support" ? "to approve" : "open"}
              </Badge>
            )}
            <div className="title">{m.title[0]}{m.title[0] ? " " : ""}<em>{m.title[1]}</em></div>
            <div className="desc">{m.desc}</div>
          </Link>
        );
      })}
    </SimpleGrid>
  );
}
