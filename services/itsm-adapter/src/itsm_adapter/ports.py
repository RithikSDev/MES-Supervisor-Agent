"""The two ITSM ports. TicketSync and the poller only see these; the transports behind them are interchangeable:

  - REST (default): ServiceNow Table API, Jira Cloud REST v3 (`connectors/servicenow_rest.py`, `connectors/jira_rest.py`)
  - MCP: the same operations as MCP tool calls (`connectors/mcp.py`; used with MCP servers and the test mocks)

Normalized records (plain dicts):

  incident: sys_id, number, state ("1" New "2" In Progress "3" On Hold "6" Resolved "7" Closed "8" Canceled),
            priority, impact, urgency ("1".."5"), assignment_group (group name), assigned_to ({sys_id, name} or
            None), correlation_id, close_notes,
            sys_updated_on ("YYYY-MM-DD HH:MM:SS", UTC), sys_updated_by (user name), url
  journal:  sys_id, element ("work_notes" | "comments"), value, sys_created_on (UTC), sys_created_by
  issue:    id, key, summary, status, priority, labels, updated (ISO 8601), url,
            comments [{id, author, author_id, body, created}],
            changelog [{id, author, author_id, created, items [{field, from_value, to_value}]}]
"""

from datetime import datetime
from typing import Any, Protocol


class ServiceNowPort(Protocol):
    system: str
    mode: str
    endpoint: str  # instance / MCP server URL (for the monitor)

    @property
    def configured(self) -> bool: ...

    def is_mine(self, user_name: str) -> bool:
        """Was this change made by the integration user (i.e. it came from MES)?"""
        ...

    async def find_by_correlation(self, correlation_id: str) -> dict[str, Any] | None: ...

    async def create(self, fields: dict[str, Any]) -> dict[str, Any]:
        """fields: short_description, description, impact, urgency, category, assignment_group (name),
        correlation_id, cmdb_ci (optional, name), opened_at ("YYYY-MM-DD HH:MM:SS" UTC, optional), assigned_to
        (user sys_id, optional)"""
        ...

    async def update(self, sys_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        """changes: any of state, impact, urgency, assignment_group (name), assigned_to (user sys_id, "" = nobody),
        close_notes"""
        ...

    async def find_user(self, email: str | None, user_name: str | None) -> dict[str, Any] | None:
        """A user by e-mail, else user name: {sys_id, user_name, name, email}; None if there is none."""
        ...

    async def get_user(self, sys_id: str) -> dict[str, Any] | None: ...

    async def journal(self, sys_id: str, since: datetime | None = None) -> list[dict[str, Any]]: ...

    async def add_work_note(self, sys_id: str, text: str) -> None: ...

    async def changed_since(self, since: datetime) -> list[dict[str, Any]]:
        """Incidents linked to MES (correlation id MES:...) updated at or after `since`, oldest first."""
        ...

    async def health(self) -> dict[str, Any]: ...


class JiraPort(Protocol):
    system: str
    mode: str
    endpoint: str

    @property
    def configured(self) -> bool: ...

    async def is_mine(self, author_id: str) -> bool: ...

    async def find_by_label(self, label: str) -> dict[str, Any] | None: ...

    async def create(self, *, issue_type: str, summary: str, description: str, priority: str,
                     labels: list[str]) -> dict[str, Any]: ...

    async def comment(self, key: str, text: str) -> None: ...

    async def changed_since(self, since: datetime) -> list[dict[str, Any]]:
        """Issues labelled `mes` updated at or after `since`, oldest first, with comments and changelog."""
        ...

    async def health(self) -> dict[str, Any]: ...
