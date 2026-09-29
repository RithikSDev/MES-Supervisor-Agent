---
id: servicenow-sync-failed
title: Ticket not reaching ServiceNow or Jira (sync failed)
category: INTEGRATION
symptoms: ticket shows "sync failed"; no INC number; Jira issue missing after L3 escalation
---
## Diagnosis (support)
- get_integration_errors with system "itsm" lists ticket sync messages that failed, with the error.
- HTTP 401/403: the integration user's credentials or roles; an administrator must fix the connection (escalate L2).
- HTTP 429/5xx or timeouts: ServiceNow/Jira was busy; a retry is appropriate.
- "assignment group does not exist": configuration (escalate L2).

## Fix / escalate
- Transient failure: retry_itsm for the message id; resolve the sync ticket once the incident number appears.
- Credentials or configuration: escalate to L2 with the error.
