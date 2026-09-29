---
title: MES user guide: shop-floor terminal (operators and technicians)
screen: /terminals
---
## Set up a terminal (admin)
Admin workspace -> Terminals: each terminal belongs to a line. "Enroll" creates a one-time enrolment link/code for the
device; open it on the touch screen once. Operators need a PIN (Terminals -> Operators -> set PIN); a PIN can be
flagged "technician" for maintenance technician rights on the terminal. Too many wrong PINs lock the PIN for a while
(unlock on the Terminals page).

## Operator workflow on the terminal
1. Log in with the PIN (one person per terminal: a new login ends the previous session). Idle logout after 10 min.
2. Queue: dispatched operations of the line. Pick one, choose the machine, start SETUP, then start RUN (green).
   A run can't start if the machine is DOWN or in maintenance.
3. Pause with a reason (material missing, machine issue, quality, break, ...); resume later. "Machine issue" offers to
   call maintenance (reports a breakdown: the machine goes DOWN).
4. Confirm quantities with the keypad: yield, scrap (with reason), rework. The last confirmation finishes the
   operation. First-off inspection may be required before production continues.
5. Offline: the header shows OFFLINE and "n to sync"; actions are queued and sent when the network is back.

## Get help (terminal)
"Get help" -> "Talk to the support assistant": the AI assistant asks what happens, suggests fixes and raises a ticket
(severity from the impact: line stopped = P1). Or use the form: machine problem (breakdown), MES problem (ticket),
quality problem (defect). The ticket number is shown; supervisors see it in Support tickets.

## Technician mode
With a technician PIN: Maintenance tab -> take a notification, start the maintenance order, work the checklist, book
spare parts (from the MES store), record findings and labour, and sign off with the e-signature. Sign-off releases the
machine (AVAILABLE) and closes the SAP PM order.
