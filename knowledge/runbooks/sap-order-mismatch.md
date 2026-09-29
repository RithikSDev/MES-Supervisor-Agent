---
id: sap-order-mismatch
title: Order in MES differs from SAP (status, quantity, dates) - L2
category: INTEGRATION
symptoms: order shows the wrong status or quantity; order closed in SAP but open in MES; order missing in MES
---
## Diagnosis (L2)
- compare_order_with_sap: MES order next to the SAP snapshots the adapter received and the postings MES sent.
- No recent SAP snapshot although SAP changed the order: check get_service_health (SAP polling last success) and
  search_logs on erp-adapter-worker for the order number.
- A snapshot was received but MES did not apply it: get_event_failures and search_logs on core-worker (level error)
  around the received time; trace_request with `event:<id>` if a log line names the event.

## Fix
- Polling or a one-off failed event: resync_order (re-reads the order from SAP; needs approval), then verify with
  compare_order_with_sap and resolve.
- The event keeps failing in core (same traceback each time): record the root cause and escalate to L3 with the
  traceback and the order as reproduction.
