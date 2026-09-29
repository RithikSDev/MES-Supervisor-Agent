---
title: Machine troubleshooting: safety rules and general approach (all machines)
kind: MACHINE_GUIDE
---
Sample plant knowledge for the demo machines. Replace or extend with the OEM manuals (upload them under AI Support
Assistant -> Agent console -> Knowledge).

## Safety first (always)
- Before opening guards, electrical cabinets, hydraulic or pneumatic lines: lock-out / tag-out (LOTO). Isolate main
  power, air and hydraulic supply, bleed stored energy (accumulators, air receivers, springs, raised axes), put your
  own lock and tag, and verify zero energy by trying to start.
- Electrical cabinets and drives: only qualified electricians. DC-link capacitors of spindle and axis drives stay
  charged for minutes after power-off: wait the time on the drive label (typically 5 min) and measure before touching.
- Never bridge interlocks, door switches or E-stops to "test". Never run a spindle with the door open.
- Hot surfaces (spindle, hydraulic unit, washer tank at 60 C), sharp chips, coolant mist: gloves and glasses.
- Operators do only the operator checks in these guides (reset, visual checks, clean, report). Anything behind a guard
  or panel is for maintenance.

## General approach to a machine failure
1. Make it safe and stop production on the machine (in MES: pause the run with reason "Machine issue", or the operator
   reports the breakdown on the terminal: the machine goes DOWN and a notification is created).
2. Read the exact alarm number and text from the control, and when it happened (on start, under load, at a certain
   rpm / position). A photo of the alarm screen helps.
3. Check the machine's history in MES: earlier breakdowns with the same alarm, what fixed them, and how long ago the
   last preventive maintenance was. A fault that comes back after every repair means the root cause was not fixed
   (example: a drive fuse that blows again: find why the current is too high, don't just replace the fuse).
4. Work from the simple and likely to the complex: supplies (air, power, hydraulic pressure, coolant level), then
   sensors and switches, then actuators, then the drive / control.
5. Record findings and parts used on the maintenance order; sign it off in technician mode so MES releases the
   machine. Book spare parts to the maintenance order (goods issue 261).

## Downtime reason codes in MES
MECHANICAL (bearings, belts, guides, tool changer), ELECTRICAL (drives, fuses, motors, sensors, cabling), HYDRAULIC
(hydraulic / pneumatic: pressure, leaks, cylinders, valves), CONTROLS (PLC, CNC, software, parameters), TOOLING
(tools, fixtures, clamping), COOLANT (coolant, lubrication), UTILITIES (air, power, water supply), OTHER.

## When to escalate
- Same fault three times in a week, or a repair that does not hold: raise it to the maintenance engineer (root cause
  analysis), ticket P2 or higher.
- Anything with smoke, burning smell, arcing, a crash (collision) or a hydraulic leak on hot parts: stop, isolate,
  call the maintenance engineer and the shift supervisor at once.
- Suspected software / parameter problem on the CNC or PLC: machine builder service (OEM hotline) with the alarm log.
