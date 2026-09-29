---
id: sap-posting-failed
title: Confirmation or goods movement not posted to SAP
category: INTEGRATION
symptoms: confirmation shows failed or sending to SAP for a long time; SAP does not show the quantity; goods receipt missing in SAP; automatic ticket "SAP rejected"
---
## Operator steps (chat)
1. Reassure the operator: the quantity is saved in MES; production can continue. Postings are sent to SAP in the
   background and retried automatically.
2. Ask for the order number and operation if they are not in the context.
3. Create a ticket so support can look at the SAP queue.

## Diagnosis (support)
- get_integration_errors (optionally with the order number) lists SAP messages that are waiting to retry, failed or
  dead, with the last error and the number of attempts.
- RETRY_WAIT with a timeout, "locked by user" or HTTP 5xx: SAP was busy; the adapter retries by itself. Nothing to do
  unless it has been waiting for more than 15 minutes.
- FAILED or DEAD because SAP was unreachable or locked (transient reason): a retry is appropriate.
- FAILED because SAP rejected the content (order already technically completed/TECO, material or batch unknown,
  posting period closed, quantity exceeds the order): a retry will fail the same way. This needs L2 (SAP team).
- The adapter reconciles before every retry (it looks the posting up in SAP first), so a retry never posts twice.

## Fix / escalate
- Transient failure: retry_sap for that message id, then note the result on the ticket; resolve once it is POSTED.
- Business rejection: escalate to L2 with the message id, the SAP error text and what it means.
