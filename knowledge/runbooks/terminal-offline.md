---
id: terminal-offline
title: Terminal shows OFFLINE or "n to sync"
category: MES_APP
symptoms: header says OFFLINE; actions waiting "to sync"; terminal slow; network lost at the line
---
## Operator steps (chat)
1. Explain that the terminal keeps working offline: starts, pauses and quantity reports are saved on the terminal and
   sent in order when the network is back ("n to sync" counts them). Nothing is lost.
2. Ask how long it has been offline and whether other terminals on the line are offline too.
3. Check the network cable at the back of the terminal (or Wi-Fi symbol); if offline more than 15 minutes, raise a ticket.

## Diagnosis (support)
- get_terminal_status shows whether the terminal's session is active.
- Several terminals of one line offline at once: network switch/Wi-Fi (escalate to L2 infrastructure).

## Fix / escalate
- Back online and synced: resolve.
- Offline for a long time or the whole line: escalate to L2 with the terminals affected.
