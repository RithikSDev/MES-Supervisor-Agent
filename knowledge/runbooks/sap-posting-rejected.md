---
id: sap-posting-rejected
title: SAP rejected a posting (automatic L2 ticket) - L2
category: INTEGRATION
symptoms: automatic ticket "SAP rejected confirmation" or "goods movement"; posting FAILED or DEAD
---
## Diagnosis (L2)
- The ticket's integration reference names the message; get_integration_errors (sap) lists failed messages, and
  get_sap_message shows the payload, SAP's answer and every attempt.
- Read SAP's error text (see the architecture guide: TECO, locked, master data, posting period, quantity).
- compare_order_with_sap for the order: SAP status (e.g. TECO) versus MES status explains most rejections.
- search_logs on erp-adapter-worker for the message id or order to see the SAP calls.

## Fix
- Transient (locked, timeout, SAP unavailable): retry_sap_message. Resolve once POSTED.
- Business rejection (TECO, master data, posting period): nothing to retry yet. Record the root cause, add a note for
  the human L2 / SAP team with the exact decision needed (for example: reopen order 1000123 in SAP, then retry
  message X), and leave the ticket open for them. Do not escalate business decisions to L3.
- The payload itself is wrong because of MES (wrong quantity, unit, material): record the root cause as CODE_BUG and
  escalate to L3 with the payload and the expected values.
