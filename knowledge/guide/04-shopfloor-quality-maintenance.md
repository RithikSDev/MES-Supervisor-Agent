---
title: MES user guide: line status, materials, quality, traceability, maintenance
screen: /maintenance
---
## Line status (/line-status)
Live state of every machine (running, setup, paused with reason, idle), the order and operator, and work in progress
waiting per work center. Updates live (LIVE badge).

## Materials & stock (/materials)
Stock by lot, storage location and stock type (unrestricted, quality inspection, blocked); goods movements with SAP
movement types (101 receipt, 261 issue to order or maintenance order, 311 transfer); shortages = component demand of
released orders vs available stock, per line and order. Backflushed components are issued automatically with each
confirmation.

## Quality (/quality)
- Inspection lots: incoming (usage decision ACCEPTED / REJECTED) and in-process (first-off inspection). Record results
  per characteristic; out-of-tolerance creates a nonconformance.
- Nonconformances (NC): severity (minor / major / critical), disposition (use as is, rework, scrap). A critical NC or a
  failed first-off puts the order on quality hold (ON_HOLD); release it with the usage decision / disposition, then a
  supervisor resumes the order.

## Traceability (/traceability)
Search a lot, serial number or order: backward (which component lots went into it) and forward (where a lot went).
Blocked lots and open NCs are highlighted. Serial numbers are assigned at goods receipt.

## Maintenance (/maintenance)
- Machines: status (AVAILABLE, DOWN, MAINTENANCE), MTBF, MTTR, failures, running hours; "Report problem".
- Notifications (M1 breakdown, M2 request) -> maintenance orders (PM01 corrective, PM02 preventive), mirrored to SAP PM
  with the same numbers. Open an order to see the checklist, parts, labour and the e-signed release.
- Downtime log: breakdowns (unplanned) and planned maintenance, with reason codes.
- Preventive plans: by calendar days or running hours; the worker creates PM02 orders when due.
- A machine repaired but still DOWN: the maintenance order must be signed off (technician mode) or a supervisor
  reports it back up.
