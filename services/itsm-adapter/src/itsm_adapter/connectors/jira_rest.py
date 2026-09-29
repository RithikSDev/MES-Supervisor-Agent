"""Jira Cloud over REST API v3 (works with a free Jira Cloud site).

  search       POST /rest/api/3/search/jql            (the current JQL search endpoint)
  issues       POST /rest/api/3/issue, GET /rest/api/3/issue/{key}
  comments     GET/POST /rest/api/3/issue/{key}/comment   (bodies in Atlassian Document Format)
  changelog    GET /rest/api/3/issue/{key}/changelog
  who am I     GET /rest/api/3/myself                 (our own account id: our changes are not echoed back)
Auth: basic auth with the account e-mail and an API token (id.atlassian.com -> Security -> API tokens).

JQL time filters are relative (`updated >= "-15m"`), so the site/user time zone doesn't matter.
"""

import math
from datetime import UTC, datetime
from typing import Any

import httpx

from itsm_adapter.calls import begin, ms
from itsm_adapter.errors import ItsmError, ItsmErrorKind, from_response, from_transport

ISSUE_FIELDS = ["summary", "status", "priority", "labels", "updated", "issuetype"]


def adf(text: str) -> dict[str, Any]:
    """Plain text -> Atlassian Document Format (one paragraph per line)."""
    return {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]} if line else {"type": "paragraph"}
        for line in text.split("\n")
    ]}


def adf_text(node: Any) -> str:
    """Atlassian Document Format -> plain text."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    parts = [adf_text(c) for c in node.get("content", [])]
    sep = "\n" if node.get("type") in ("doc", "bulletList", "orderedList") else ""
    return sep.join(p for p in parts if p is not None)


def iso(jira_ts: str) -> str:
    """'2026-09-24T10:00:00.000+0000' -> ISO 8601 with a colon offset."""
    return datetime.strptime(jira_ts, "%Y-%m-%dT%H:%M:%S.%f%z").isoformat()


class JiraRest:
    system = "jira"
    mode = "rest"

    def __init__(self, site_url: str, email: str, api_token: str, project: str, *, timeout_s: float = 15.0,
                 http: httpx.AsyncClient | None = None) -> None:
        self.site = self.endpoint = site_url.rstrip("/")
        self.email = email
        self.project = project
        self._http = http or httpx.AsyncClient(timeout=timeout_s)
        self._auth = httpx.BasicAuth(email, api_token)
        self._me: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.site and self.email and self.project)

    async def _call(self, method: str, path: str, what: str, *, params: dict[str, Any] | None = None,
                    json: Any = None) -> Any:
        entry, started = begin(self.system, f"{method} {path}", {"what": what})
        try:
            resp = await self._http.request(method, f"{self.site}/rest/api/3{path}", params=params, json=json,
                                            auth=self._auth, headers={"Accept": "application/json"})
        except httpx.TransportError as e:
            entry.update(outcome="TRANSPORT", ms=ms(started))
            raise from_transport(self.system, what, e, write=method != "GET") from e
        entry.update(ms=ms(started), status=resp.status_code, outcome="OK" if resp.status_code < 400 else "ERROR")
        if resp.status_code >= 400:
            try:
                body = resp.json()
                detail = "; ".join([*body.get("errorMessages", []), *(f"{k}: {v}" for k, v in (body.get("errors") or {}).items())])
            except ValueError:
                detail = resp.text[:300]
            entry["error"] = (detail or resp.reason_phrase)[:300]
            raise from_response(self.system, what, resp, detail or resp.reason_phrase)
        return resp.json() if resp.content else None

    async def is_mine(self, author_id: str) -> bool:
        if self._me is None:
            self._me = (await self._call("GET", "/myself", "who am I"))["accountId"]
        return author_id == self._me

    # ------------------------------------------------------------------ normalization
    def _issue(self, raw: dict[str, Any], comments: list[dict[str, Any]] | None = None,
               changelog: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        f = raw.get("fields") or {}
        return {
            "id": raw["id"], "key": raw["key"], "summary": f.get("summary"),
            "status": (f.get("status") or {}).get("name"), "priority": (f.get("priority") or {}).get("name"),
            "labels": f.get("labels") or [], "updated": iso(f["updated"]) if f.get("updated") else None,
            "url": f"{self.site}/browse/{raw['key']}", "comments": comments or [], "changelog": changelog or [],
        }

    async def _comments(self, key: str) -> list[dict[str, Any]]:
        body = await self._call("GET", f"/issue/{key}/comment", "read comments", params={"maxResults": 100, "orderBy": "created"})
        return [{"id": c["id"], "author": (c.get("author") or {}).get("displayName"),
                 "author_id": (c.get("author") or {}).get("accountId"), "body": adf_text(c.get("body")),
                 "created": iso(c["created"])} for c in body.get("comments", [])]

    async def _changelog(self, key: str) -> list[dict[str, Any]]:
        body = await self._call("GET", f"/issue/{key}/changelog", "read changelog", params={"maxResults": 100})
        return [{"id": h["id"], "author": (h.get("author") or {}).get("displayName"),
                 "author_id": (h.get("author") or {}).get("accountId"), "created": iso(h["created"]),
                 "items": [{"field": i.get("field"), "from_value": i.get("fromString"), "to_value": i.get("toString")}
                           for i in h.get("items", [])]} for h in body.get("values", [])]

    async def _search(self, jql: str, limit: int) -> list[dict[str, Any]]:
        body = await self._call("POST", "/search/jql", "search", json={"jql": jql, "fields": ISSUE_FIELDS, "maxResults": limit})
        return body.get("issues", [])

    # ------------------------------------------------------------------ port
    async def find_by_label(self, label: str) -> dict[str, Any] | None:
        rows = await self._search(f'project = "{self.project}" AND labels = "{label}"', 1)
        if not rows:
            return None
        return self._issue(rows[0], comments=await self._comments(rows[0]["key"]))

    async def create(self, *, issue_type: str, summary: str, description: str, priority: str,
                     labels: list[str]) -> dict[str, Any]:
        fields: dict[str, Any] = {"project": {"key": self.project}, "issuetype": {"name": issue_type},
                                  "summary": summary[:255], "description": adf(description), "labels": labels,
                                  "priority": {"name": priority}}
        try:
            created = await self._call("POST", "/issue", "create issue", json={"fields": fields})
        except ItsmError as e:
            # team-managed projects often don't show Priority on the create screen: create without it
            if e.kind != ItsmErrorKind.PERMANENT or "priority" not in e.message.lower():
                raise
            fields.pop("priority")
            created = await self._call("POST", "/issue", "create issue", json={"fields": fields})
        raw = await self._call("GET", f"/issue/{created['key']}", "read issue", params={"fields": ",".join(ISSUE_FIELDS)})
        return self._issue(raw)

    async def comment(self, key: str, text: str) -> None:
        await self._call("POST", f"/issue/{key}/comment", "add comment", json={"body": adf(text)})

    async def changed_since(self, since: datetime) -> list[dict[str, Any]]:
        minutes = max(1, math.ceil((datetime.now(UTC) - since).total_seconds() / 60) + 1)
        rows = await self._search(f'project = "{self.project}" AND labels = "mes" AND updated >= "-{minutes}m" '
                                  "ORDER BY updated ASC", 100)
        return [self._issue(r, comments=await self._comments(r["key"]), changelog=await self._changelog(r["key"]))
                for r in rows]

    async def issue_types(self) -> list[str]:
        project = await self._call("GET", f"/project/{self.project}", "read project")
        return [t["name"] for t in project.get("issueTypes", [])]

    async def aclose(self) -> None:
        await self._http.aclose()

    async def health(self) -> dict[str, Any]:
        me = await self._call("GET", "/myself", "who am I")
        project = await self._call("GET", f"/project/{self.project}", "read project")
        types = [t["name"] for t in project.get("issueTypes", [])]
        return {"detail": [f"REST v3 as {me.get('displayName')}", f"project {project.get('key')}: {', '.join(types)}"]}
