---
title: CNC lathe (TURN-01, TURN-02): faults, alarms and fixes
kind: MACHINE_GUIDE
equipment_type: CNC_LATHE
---
Sample guide for the demo 2-axis CNC lathes (spindle drive with DC link, hydraulic chuck and tailstock, turret,
chip conveyor, flood coolant). Alarm numbers are the plant's sample codes.

## Alarm 2031: spindle drive fault (overcurrent / fuse)
Symptom: spindle stops, alarm 2031 "Spindle drive alarm", often at acceleration or under heavy cut; drive fuse blown.
Likely causes, most likely first:
1. Motor cable damaged (chafed in the drag chain or cable duct, coolant ingress in the connector): short to earth.
2. Spindle motor winding insulation breaking down (damp, contaminated with coolant).
3. Mechanical overload: spindle bearings failing, brake not releasing, chips packed around the spindle, too heavy a cut.
4. Drive itself: DC-link capacitors aged, cooling fan / filter clogged (drive overheats), loose power terminals.
5. Wrong fuse fitted (rating or class): check against the drive label.
Checks (maintenance, after LOTO and waiting for the DC link to discharge):
- Insulation test (megger, 500 V) of motor and cable, phase to phase and phase to earth: > 100 MOhm is good; < 1 MOhm
  = cable or motor fault. Test motor and cable separately to find which.
- Inspect cable in the drag chain and the motor connector for damage and coolant.
- Turn the spindle by hand: smooth, no noise, no drag. Check the brake releases.
- Drive fault log: sub-code and current at trip; clean drive cooling fan and filter; check terminal torque.
Fix: replace the damaged part (cable, connector, motor or bearings) before fitting a new fuse. Replacing only the fuse
lets it come back: repeat 2031 alarms on the same machine mean the cause is still there.
Parts: drive fuse (per drive label), motor cable, bearing 6205 (SP-BEARING-6205) for the spindle motor fan-side
bearing. Reason code: ELECTRICAL.

## Alarm 1402: chuck pressure low / chuck not clamped
Symptom: cycle does not start or stops, "chuck clamp not confirmed", part may slip.
Causes: hydraulic pressure low (check the gauge: nominal 30-40 bar for the chuck), clamp pressure switch set wrong or
faulty, leaking chuck cylinder seal, dirty chuck jaws, wrong jaw stroke for the part.
Checks: hydraulic unit level and pressure; pressure switch setting; clean chuck and jaws; watch for oil at the rotary
union. Fix: seal kit for the cylinder (SP-SEAL-KIT-CYL), adjust switch, clean chuck. Reason code: HYDRAULIC.

## Alarm 1510: turret not indexed / turret lock
Symptom: turret stops between stations, "turret clamp not confirmed".
Causes: chips in the turret coupling, clamp sensor dirty or misadjusted, low hydraulic pressure, worn coupling.
Checks: clean coupling area, check sensor gap (1-2 mm), pressure. Reason code: MECHANICAL.

## Alarm 2120 / 2121: axis following error X / Z
Causes: axis blocked (crash, chips, way cover jammed), way lubrication empty, servo motor or encoder fault, ball screw
bearing. Checks: way-lube level (monthly plan), look for collision damage, move the axis slowly in jog. After a crash:
check alignment and do not run production until checked. Reason code: MECHANICAL or ELECTRICAL.

## Coolant and chips
Coolant concentration 6-8 % (refractometer); low concentration causes rust and poor surface; foam = wrong mix or
contamination. Chip conveyor jammed: stop, LOTO, clear; frequent jams = wrong chip form (adjust cutting data).
Reason code: COOLANT.

## Preventive maintenance
Monthly lubrication and chip conveyor check (calendar, 30 days): way-lube level, conveyor, coolant concentration.
Spindle and axis inspection by running hours: run-out <= 5 um, axis backlash, tool changer, hydraulic filter when
dP > 2 bar. Overdue preventive maintenance increases breakdowns: check it in MES before blaming the machine.
