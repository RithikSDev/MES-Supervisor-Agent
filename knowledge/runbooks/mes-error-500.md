---
id: mes-error-500
title: MES screen shows an unexpected error (HTTP 500 / "something went wrong") - L2
category: MES_APP
symptoms: error message on a screen; action fails with an unexpected error; terminal shows "not accepted" with a server error
---
## Diagnosis (L2)
- search_logs on core-api with level error around the reported time (minutes back from the ticket time); the line
  has the exception and a request_id.
- trace_request with that request id: what the request did across services and where it failed.
- get_recent_changes around the time: a record changed just before may explain the failing state.
- Check whether it repeats: search_logs with the exception text over the last days, search_tickets for the error.

## Fix / escalate
- Caused by data (one record in an unexpected state) and there is a supported action to fix it: propose it.
- A traceback from MES code (TypeError, KeyError, AttributeError, a failed assertion) that repeats: record the root
  cause as CODE_BUG and escalate to L3 with the steps to reproduce, the request id, the exception and the log lines.
- A one-off infrastructure blip (database restart, timeout) that no longer occurs: record it and resolve.
