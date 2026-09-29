---
title: MES user guide: getting started, roles and navigation
screen: /
---
## What MES does here
The MES runs production of plant P1000 (line L1 machining, line L2 assembly). Production orders come from SAP; MES
schedules them on the machines, operators run them on shop-floor terminals, MES confirms quantities and goods
movements back to SAP, tracks quality, genealogy (lots and serials) and maintenance, and handles support tickets with
AI support engineers (ServiceNow for L1/L2, Jira for L3).

## Signing in and navigation (admin workspace, http://localhost:3080)
- Sign in with your plant account (Keycloak). The landing page shows every module you may use as a card; filter with
  the chips Production, Quality & Trace, Support, Admin. "Needs attention" lists open P1/P2 tickets, missed SLAs and
  AI proposals waiting for approval.
- Inside a module: "<- Home" returns to the cards; "Modules" (top right) jumps straight to another module.
- The header shows the plant, the alert bell (open alerts for the on-call rota), the plant clock and your account
  (roles and permissions). Sign out top right.

## Roles
- operator: shop-floor terminal work (runs, confirmations, defects, get help); reads orders.
- supervisor: plans (schedule board, publish with e-signature, dispatch), manages tickets, approves AI proposals,
  resumes orders after a quality hold.
- quality engineer: inspections, nonconformances, usage decisions.
- maintenance technician: maintenance notifications and orders (also on the terminal in technician mode).
- admin: everything above plus integrations (SAP, ServiceNow/Jira, AI data connections), AI policy, terminals, PINs.
A button you don't see or that is disabled usually means your role lacks the permission.

## The two user interfaces
- Admin workspace (desktop): planning, analytics, support, administration.
- Operator terminal (http://localhost:3081, touch screen at the line): enrolled per terminal, operators log in with a
  PIN. See the guide "Shop-floor terminal".
