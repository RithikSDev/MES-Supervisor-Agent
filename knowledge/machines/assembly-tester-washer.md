---
title: Assembly stations, pressure/function tester and deburr/wash cell: faults and fixes
kind: MACHINE_GUIDE
---
Sample guide for line L2 assembly (ASSY-01, ASSY-02: pneumatic presses, screwdrivers with torque control, scanners),
the pressure/function tester TEST-01 and the deburr/wash cell DEB-01 on line L1.

## Assembly stations (equipment type ASSEMBLY)
### Press force not reached / press fault
Causes: air pressure low (needs 6 bar at the station; check the regulator and the main air supply), valve sticking,
cylinder seal worn, part or component out of tolerance, force sensor drift. Checks: air gauge at the station; manual
press test with a master part; force curve on the HMI. Parts: cylinder seal kit (SP-SEAL-KIT-CYL). Reason code:
HYDRAULIC (pneumatic) or UTILITIES if the plant air is low everywhere.
### Screwdriver torque NOK
Causes: worn bit, wrong program, screw or thread out of spec, cable/controller fault. Checks: replace bit, check the
selected program for the variant, test on a torque analyser. Reason code: TOOLING.
### Part not detected / scanner
Sensor dirty or misaligned, wrong nest for the variant; scanner reading problems: see the runbook "scanner not
reading". Reason code: CONTROLS.

## Pressure / function tester TEST-01 (equipment type TESTER)
### Leak test fails on good parts (false rejects)
Causes: seal of the test fixture worn or dirty, temperature difference (parts warm from washing), reference volume
leak, calibration drift. Checks: test the master (good) part: if it fails, the tester is the problem, not the parts;
clean and check fixture seals; let parts cool to room temperature; check the reference pressure gauge.
Fix: replace fixture seals, recalibrate (quarterly calibration plan). Do not release parts tested while the tester
was suspect: block them in quality (nonconformance) and retest. Reason code: TOOLING or CONTROLS.
### Tester does not start / pressure not built
Air supply below 6 bar, valve fault, safety door switch. Reason code: HYDRAULIC (pneumatic) or UTILITIES.

## Deburr / wash cell DEB-01 (equipment type WASHER)
### Parts not clean / chips left, or wash pressure low
Causes: filter bag clogged (weekly change), spray nozzles blocked, pump wear, low tank level, detergent concentration.
Checks: filter bag (SP-FILTER-WASH, 50 um), nozzles, tank level and temperature (60 C), pump pressure. Reason code:
COOLANT (wash fluid) or MECHANICAL (pump).
### Tank temperature low / heater fault
Heater contactor, thermostat, level switch (heater is off at low level). Reason code: ELECTRICAL.
### Weekly maintenance
Drain and clean the tank strainer, replace the filter bag, check spray nozzles (plan "Weekly washer filter change").
Overdue: expect unclean parts and pump damage.
