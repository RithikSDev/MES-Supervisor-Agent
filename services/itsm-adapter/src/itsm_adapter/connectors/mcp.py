"""The ports over MCP tool calls (ITSM_MODE=mcp): for ServiceNow / Atlassian MCP servers that expose these tool
names, and for the headless mocks used in tests."""

from datetime import UTC, datetime
from typing import Any

from itsm_adapter.mcp_tools import McpTools


class ServiceNowMcp:
    system = "servicenow"
    mode = "mcp"

    def __init__(self, tools: McpTools, integration_user: str) -> None:
        self.tools = tools
        self.endpoint = tools.url
        self.user = integration_user

    @property
    def configured(self) -> bool:
        return bool(self.tools.url)

    def is_mine(self, user_name: str) -> bool:
        return user_name == self.user

    async def find_by_correlation(self, correlation_id: str) -> dict[str, Any] | None:
        rows = await self.tools.call("list_incidents", {"correlation_id": correlation_id, "limit": 1})
        return rows[0] if rows else None

    async def create(self, fields: dict[str, Any]) -> dict[str, Any]:
        args = {k: v for k, v in fields.items() if k not in ("opened_at", "assigned_to")}  # not in the MCP tool
        return await self.tools.call("create_incident", {**args, "impact": int(fields["impact"]), "urgency": int(fields["urgency"])})

    async def update(self, sys_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        args = {k: v for k, v in changes.items() if v is not None  # the MCP tool can't change these
                and k not in ("short_description", "description", "category", "assigned_to")}
        for k in ("state", "impact", "urgency"):
            if k in args:
                args[k] = int(args[k])
        return await self.tools.call("update_incident", {"sys_id": sys_id, **args})

    async def find_user(self, email: str | None, user_name: str | None) -> dict[str, Any] | None:
        return None  # the MCP tools have no user lookup: assignees stay in MES (and in a work note)

    async def get_user(self, sys_id: str) -> dict[str, Any] | None:
        return None

    async def journal(self, sys_id: str, since: datetime | None = None) -> list[dict[str, Any]]:
        args: dict[str, Any] = {"sys_id": sys_id}
        if since is not None:
            args["since"] = since.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
        return await self.tools.call("get_journal", args)

    async def add_work_note(self, sys_id: str, text: str) -> None:
        await self.tools.call("add_work_note", {"sys_id": sys_id, "text": text})

    async def changed_since(self, since: datetime) -> list[dict[str, Any]]:
        rows = await self.tools.call("list_incidents", {"updated_since": since.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
                                                        "limit": 200})
        return [r for r in rows if (r.get("correlation_id") or "").startswith("MES:")]

    async def health(self) -> dict[str, Any]:
        return {"detail": await self.tools.ping()}


class JiraMcp:
    system = "jira"
    mode = "mcp"

    def __init__(self, tools: McpTools, integration_user: str, project: str) -> None:
        self.tools = tools
        self.endpoint = tools.url
        self.user = integration_user
        self.project = project

    @property
    def configured(self) -> bool:
        return bool(self.tools.url)

    async def is_mine(self, author_id: str) -> bool:
        return author_id == self.user

    @staticmethod
    def _norm(i: dict[str, Any]) -> dict[str, Any]:
        return {**i, "comments": [{**c, "author_id": c["author"]} for c in i.get("comments", [])],
                "changelog": [{**h, "author_id": h["author"]} for h in i.get("changelog", [])]}

    async def find_by_label(self, label: str) -> dict[str, Any] | None:
        rows = await self.tools.call("searchJiraIssuesUsingJql",
                                     {"jql": f'project = {self.project} AND labels = "{label}"', "maxResults": 1})
        return self._norm(rows[0]) if rows else None

    async def create(self, *, issue_type: str, summary: str, description: str, priority: str,
                     labels: list[str]) -> dict[str, Any]:
        return self._norm(await self.tools.call("createJiraIssue", {
            "projectKey": self.project, "issueTypeName": issue_type, "summary": summary, "description": description,
            "priority": priority, "labels": labels}))

    async def comment(self, key: str, text: str) -> None:
        await self.tools.call("addCommentToJiraIssue", {"issueIdOrKey": key, "commentBody": text})

    async def changed_since(self, since: datetime) -> list[dict[str, Any]]:
        rows = await self.tools.call("searchJiraIssuesUsingJql", {
            "jql": f'project = {self.project} AND labels = "mes" AND updated >= "{since.astimezone(UTC):%Y-%m-%d %H:%M}" '
                   "ORDER BY updated ASC", "maxResults": 200})
        return [self._norm(r) for r in rows]

    async def health(self) -> dict[str, Any]:
        return {"detail": await self.tools.ping()}
