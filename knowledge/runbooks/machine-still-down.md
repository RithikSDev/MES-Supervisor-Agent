---
id: machine-still-down
title: Machine repaired but still shown as DOWN or in maintenance
category: MES_APP
symptoms: machine works again but MES shows DOWN; cannot start a run because the machine is down; technician finished but status did not change
---
## Operator steps (chat)
1. Ask whether a maintenance technician worked on the machine and whether it is really running normally now.
2. If the technician is still on site, the clean way is that they sign off the maintenance order in technician mode;
   that releases the machine.

## Diagnosis (support)
- get_machine_status: status, open downtime, open notifications and maintenance orders.
- Machine DOWN with a maintenance order IN_PROGRESS: the technician has not signed off. The technician (or their
  supervisor) must complete the order; do not bypass it.
- Machine DOWN with no maintenance order in progress (breakdown reported, fixed by the operator, e.g. a cleared jam):
  reporting the machine back up is appropriate.

## Fix / escalate
- No order in progress and the operator confirms it runs: machine_back_up with a note of what was fixed.
- Order in progress: note on the ticket that the technician must sign off; resolve once they have, or escalate if the
  technician is not reachable and production waits.
