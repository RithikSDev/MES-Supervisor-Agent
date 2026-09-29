---
id: scanner-not-reading
title: Barcode scanner does not read or drops out
category: HARDWARE
symptoms: scanner beeps but nothing appears; scanner light off; scanner disconnects every few minutes; wrong characters
---
## Operator steps (chat)
1. Ask whether the scanner lights up when the trigger is pressed.
2. No light: check the cable is plugged into the terminal (USB) or the cradle has power; for a wireless scanner put
   it back into the cradle for 10 seconds.
3. Light but no text on screen: make sure the input field on the terminal is selected (tap it), then scan again.
4. Scans some labels but not others: the label may be damaged or printed faintly (see runbook blank-labels); try a
   known-good label.
5. Drops out every few minutes (wireless): move the cradle closer or away from metal; if it still drops, it is a
   hardware fault.
6. Until fixed the operator can type numbers on the terminal keypad, so production does not need to stop.

## Diagnosis (support)
- search_tickets for "scanner" on the same terminal/line. Several drop-outs in a week point to a failing unit.
- A scanner fault is not a line stop when keypad entry works: severity P2 (degraded) at most.

## Fix / escalate
- Fixed by reseating/cradle/field focus: resolve with the cause.
- Hardware fault (no light with power, repeated drop-outs): escalate to L2 so the hardware team swaps the unit.
