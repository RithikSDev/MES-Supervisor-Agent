---
id: blank-labels
title: Label printer prints blank or faint labels
category: HARDWARE
symptoms: labels come out empty, faint or partly printed; printer feeds but nothing is printed; wrong label size
---
## Operator steps (chat)
1. Ask what the label looks like: completely empty, faint/partial, or printed on the wrong place.
2. Completely empty: the ribbon is usually out or inserted inside-out. Ask the operator to open the printer cover and
   check the ribbon is present and the ink side faces the label. Close the cover firmly until it clicks.
3. Faint or partial: press and hold the printer's FEED button for 2 seconds to recalibrate the label gap (it feeds a
   few blank labels, that is expected).
4. Ask them to reprint the last label from the terminal (operation screen, reprint) and confirm.
5. If one of these fixes it, confirm with the operator and close the conversation as fixed.

## Diagnosis (support)
- search_tickets for "label printer blank" on the same line: repeated tickets within days point to worn printhead
  or a bad ribbon batch, which needs the hardware team (escalate), not another recalibration.
- Blank labels on every terminal of a line at the same time point to the label template, not the printer (escalate
  to L2: template/print service).

## Fix / escalate
- Ribbon or calibration fixed it: resolve with what was done.
- Printhead damage, repeated failures, or all printers of a line affected: escalate to L2 with the evidence.
- Line stopped for labels (no labels = no shipping): keep severity P1 while unresolved.
