---
id: order-on-hold
title: Order cannot be started or resumed (quality hold)
category: MES_APP
symptoms: red hold banner on the operation; start or resume refused; order shows ON_HOLD; "order is on quality hold"
---
## Operator steps (chat)
1. Explain that a quality hold is intentional: a first-off or in-process inspection failed or a critical defect was
   reported, so MES stops the order until quality has looked at it. It is not a system fault.
2. The operator should pause with reason "Quality check" if not already paused, and inform the supervisor/quality
   engineer. Once the supervisor resumes the order, the terminal opens a repeat check.

## Diagnosis (support)
- get_work_order shows the order status, the hold and the operations.
- get_quality_status lists the open nonconformances for the order (defect, measured value, who reported it).

## Fix / escalate
- Hold for a real quality reason: resolve the ticket as "not a fault" with the explanation and the nonconformance
  number (the release is a quality/supervisor decision in MES, not a support action).
- Hold without any open nonconformance or failed inspection: that is unexpected; escalate to L2 with the order.
