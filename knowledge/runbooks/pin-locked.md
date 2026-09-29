---
id: pin-locked
title: PIN rejected or PIN entry locked
category: ACCESS
symptoms: wrong PIN message; PIN pad locked after several attempts; new operator has no PIN
---
## Operator steps (chat)
1. Ask whether the terminal says "wrong PIN" or "locked".
2. Locked: the PIN pad unlocks by itself after the lock time shown on the screen (a few minutes). Ask the operator to
   wait and then try once, carefully.
3. Forgotten PIN or new operator: only a supervisor can set a PIN (admin UI, Terminals & operators). The operator
   should ask their supervisor; this is a service request, not a fault.

## Diagnosis (support)
- get_terminal_status shows `pin_locked_until` for the terminal.
- Many lockouts on one terminal in a short time can mean a sticky touch screen (hardware).

## Fix / escalate
- Lock expires by itself: explain, resolve when the operator is in.
- PIN reset needed: the agent cannot set PINs. Resolve as a service request with the instruction to ask the
  supervisor, or escalate to L2 if no supervisor is available and the line is waiting (then keep the severity).
