---
title: MES user guide: work orders, schedule board, dispatching
screen: /work-orders
---
## Work orders (/work-orders)
- Production orders are created and released in SAP; the ERP adapter brings them into MES within seconds (status
  IMPORTED or RELEASED). You don't create orders in MES.
- The tiles filter by status (open, released, scheduled, dispatched, in progress, on hold, late, completed); search by
  order number or material. Open an order for its operations, components (with backflush), confirmations (and their
  SAP posting status), runs, quality and genealogy.
- Status flow: RELEASED -> SCHEDULED (in a published schedule) -> DISPATCHED (sent to the line) -> IN_PROGRESS ->
  COMPLETED. ON_HOLD = quality hold; a supervisor resumes it once quality released it.
- Priority (1 highest) changes where the scheduler places the order.

## Schedule board (/schedule)
1. "New draft from published" (or a new draft) creates a draft schedule for the horizon (14 days).
2. "Auto-sequence" places operations on machines by priority and due date, respecting shift calendars, maintenance
   windows (hatched) and machines that are down. "Reset unlocked" re-plans everything not locked.
3. Drag operations to change machine or time; lock an operation to keep it in place.
4. Check the "Conflicts" list (late orders, overlaps, missing material) and "Unscheduled".
5. "Publish" needs an e-signature (your password and a reason). Published orders become SCHEDULED.
Only time when all lines are off shift is shaded.

## Dispatch
A supervisor dispatches a scheduled order (work order page: Dispatch) so it appears in the terminal queue of its line.

## Confirmations to SAP
Every quantity an operator confirms is posted to SAP (with backflush of components and the goods receipt at the last
operation). The work order and the Integrations page show POSTED, sending (retrying) or FAILED. A rejected posting
raises an automatic L2 ticket; the SAP simulator (demo) can inject faults.
