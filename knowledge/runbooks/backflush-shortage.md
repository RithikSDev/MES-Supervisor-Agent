---
id: backflush-shortage
title: Quantity report rejected because of missing material (backflush)
category: DATA
symptoms: report quantity refused; "insufficient stock for backflush"; components not at the line; shortage
---
## Operator steps (chat)
1. Explain that MES consumes components automatically when good quantity is reported (backflush) and refuses the
   report if the line-side stock is not there, so stock never goes negative.
2. The material handler or supervisor must move the components to the line (Materials > Shortages > Replenish in the
   admin UI). After that the operator reports again.

## Diagnosis (support)
- get_shortages shows the materials with demand vs available and the affected orders and lines.
- A shortage where stock exists in the warehouse but not line-side: a transfer is needed (supervisor action).
- No stock anywhere: purchasing/planning issue, not an MES fault.

## Fix / escalate
- Explain and resolve as "not a fault" with the material and what to do, or keep it open ON the supervisor if the line
  is stopped (severity stays).
- Stock shown in MES but physically not there (or the reverse): data problem, escalate to L2.
