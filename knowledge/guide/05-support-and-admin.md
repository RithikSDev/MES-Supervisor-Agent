---
title: MES user guide: support tickets, AI support, alerts, analytics and administration
screen: /tickets
---
## Support tickets (/tickets)
- Severity P1 (line stop: respond 15 min, fix 2 h), P2 (degraded, workaround: 30 min / 6 h), P3 (minor: 2 h / 24 h),
  P4 (request: 8 h / scheduled). Suggested from the impact; can be changed with a reason.
- Tiers: L1 first line, L2 application/database/SAP engineering, L3 developers (Jira). All tickets start at L1 except
  automatic interface alarms (L2).
- Ticket drawer: details, SLA clocks (on hold pauses the resolution clock), activity from MES, AI, ServiceNow and Jira,
  links to the ServiceNow incident and Jira issue, the AI panel (runs, root cause, proposals, take over, run again).
  Actions: acknowledge, in progress, on hold, escalate, change severity, resolve, add a note.
- Changes sync both ways with ServiceNow (incident) and at L3 with Jira (issue).

## AI Support Assistant (/ai-support)
- Assistant: ask in plain language ("bring up the open P1 tickets", "why did MILL-02 stop?", "is order 1000123 the
  same in SAP?"). Answers come with cards (ticket lists, a ticket, approvals, SLA, alerts, AI runs, machines,
  interfaces). Changes are prepared as a confirmation card and run with your rights only when you press Confirm.
- Agent console: what the AI agents did (runs step by step, cost), approvals of AI proposals, operator chats, the
  policy (per action: off / suggest / auto, kill switch, cost limits), evaluation, and the knowledge documents.

## Alerts & on-call (/notifications)
Alerts to the on-call rota (e-mail, Teams, the bell): P1 raised, SLA at risk / missed, escalations, AI approvals
waiting, AI handovers, agent paused. Unacknowledged alerts escalate to the backup and then the supervisor. Edit the
rota and the rules (admins).

## Support analytics (/support-analytics)
Tickets per day, SLA met per severity, time to respond / resolve, categories, recurring machines, and the AI
engineers' share, approvals, cost and ratings. Choose 7 / 30 / 90 days.

## Administration
- Integrations (/integration): SAP postings and inbound orders (retry), ServiceNow / Jira (admins: Set up /
  Configure: address, user, password or API token, groups, project; Test, then Save), AI data connections (MCP
  servers the AI reads live data from: MES database, SAP, others; admins add, test and choose which agents use them).
- Knowledge (AI support -> Agent console -> Knowledge): upload OEM manuals (PDF), fault-code lists (CSV) and notes.
- Audit trail (/audit): every change with who, when, before and after, reason and e-signature.
- Terminals (/terminals): enrolment, operators, PINs.
- SAP simulator (/sap-simulator, demo only): create orders in the mock SAP, see confirmations, inject faults.
