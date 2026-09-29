---
title: CNC milling machines (MILL-01, MILL-02 5-axis; MILL-03 3-axis): faults, alarms and fixes
kind: MACHINE_GUIDE
equipment_type: CNC_MILL
---
Sample guide for the demo machining centres (motor spindle with oil-air lubrication, automatic tool changer, hydraulic
fixture clamping, rotary/tilt table on the 5-axis machines, through-spindle coolant).

## Alarm 3310: fixture clamp pressure low (hydraulic clamping)
Symptom: cycle does not start or stops with "clamp pressure low"; on MILL-03 recorded as "clamp pressure low".
Causes, most likely first:
1. Leaking clamp cylinder seal (oil at the fixture, pressure drops after clamping).
2. Hydraulic unit: low oil level, clogged filter (dP > 2 bar), pump wear, pressure relief valve drift.
3. Pressure switch misadjusted or faulty; loose connector.
4. Leaking hose or rotary coupling to the fixture.
Checks (LOTO, bleed the accumulator): oil level and colour; filter indicator; clamp and watch the gauge (should hold
60 bar for 1 min without dropping more than 2 bar); inspect cylinders and hoses for oil.
Fix: seal kit (SP-SEAL-KIT-CYL), hydraulic filter element 10 um (SP-FILTER-HYD), adjust or replace the pressure switch.
Verify "60 bar ok" after repair. Reason code: HYDRAULIC.

## Alarm 4012: spindle temperature high
Causes: spindle cooling unit off or low coolant, oil-air lubrication empty or blocked, bearing damage (noise,
vibration), too long heavy cutting. Checks: chiller running and filled; lubrication unit level and pulse; spindle
noise at 3000 rpm; run-out <= 5 um. Bearing damage needs the spindle sent for repair (OEM). Reason code: COOLANT or
MECHANICAL.

## Alarm 4401: tool change error (tool not clamped / gripper position)
Causes: dirty tool holder taper or spindle taper, drawbar clamp force low, gripper worn or misaligned, air pressure
low (unclamp needs 6 bar), wrong tool length data. Checks: clean tapers; air pressure; drawbar force test (kit);
step through the tool change in manual mode. Reason code: MECHANICAL (TOOLING if the tool holder is damaged).

## Alarm 2130-2134: axis following error / encoder (X, Y, Z, A, C)
Causes: collision, chips under way covers, way lubrication empty, servo or encoder fault, cable in the drag chain.
Checks: lube level, jog slowly and listen, drive alarm log. After a collision: check geometry before production.
Reason code: MECHANICAL or ELECTRICAL.

## Poor surface / dimensions out of tolerance (no alarm)
Causes: tool wear, spindle run-out, thermal growth (machine not warmed up), loose fixture, coolant concentration.
Checks: first-off inspection results in MES (quality), tool life counters, warm-up cycle done, run-out. Often a
TOOLING issue rather than a machine fault.

## Preventive maintenance
Monthly: lubrication and chip conveyor. Running hours: spindle run-out, axis backlash, tool changer grippers,
hydraulic filter. Hydraulic oil change yearly.
