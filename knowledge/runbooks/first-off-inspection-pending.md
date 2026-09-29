---
id: first-off-inspection-pending
title: Report quantity locked until the first-off inspection passes
category: MES_APP
symptoms: Report qty button locked; "first-off inspection required"; cannot report good quantity after starting production
---
## Operator steps (chat)
1. Explain that for some operations MES requires a first-off inspection: the first part is measured and entered
   before good quantity can be reported. It is by design.
2. On the operation screen tap "Enter results", measure the listed characteristics and enter them with the keypad.
3. If the result passes, Report qty unlocks. If it fails, the order goes on quality hold (see runbook order-on-hold).

## Diagnosis (support)
- get_work_order shows the operation; get_quality_status shows open inspection problems and nonconformances.

## Fix / escalate
- Explained and the operator entered the results: close as fixed / resolve as "not a fault".
- Inspection plan asks for a characteristic that cannot be measured at the line: change request to quality (P4).
