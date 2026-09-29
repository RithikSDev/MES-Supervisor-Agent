---
id: terminal-session-stuck
title: Terminal still logged in as someone else / session stuck
category: ACCESS
symptoms: terminal shows another operator's name; previous shift left it logged in; cannot log in, screen stuck on someone else's work
---
## Operator steps (chat)
1. Ask whose name the terminal shows in the header.
2. Normally the operator can simply log in with their own PIN: a new PIN login on a terminal ends the previous
   session automatically. Ask them to tap Log out (or wait for the idle logout after 10 minutes) and enter their PIN.
3. If the screen does not react at all, tap the ONLINE/OFFLINE header once; if it shows OFFLINE see runbook
   terminal-offline.

## Diagnosis (support)
- get_terminal_status for the terminal: shows who is logged in and since when, whether the device is enrolled and
  whether the PIN entry is locked.
- A session that belongs to someone who has gone home, on a terminal that nobody can take over, is the case for
  ending the session.

## Fix / escalate
- Use end_terminal_session with the reason (e.g. "previous shift left T-L1 logged in"). The device stays enrolled; the
  operator then logs in with their PIN.
- Terminal not enrolled ("device not enrolled" screen): an admin must enrol it again under Terminals; escalate to L2.
