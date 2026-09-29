"""Mock Jira Software: project MES for L3 (code bugs / changes), a simple workflow, comments, and an MCP server whose
tools are named like Atlassian's (createJiraIssue, transitionJiraIssue, addCommentToJiraIssue,
searchJiraIssuesUsingJql, ...). Supported JQL: `project = X`, `labels = "x"`, `updated >= "YYYY-MM-DD HH:MM"`,
joined with AND, optional `ORDER BY updated ASC|DESC`.

Headless: used only by the automated tests (the demo connects to a real Jira Cloud site).
"""

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from mock_itsm.common import BearerKey, JsonState, fail, mcp_lifespan, mount_mcp, now

INTEGRATION_USER = "mes-integration"
PROJECT = "MES"
ISSUE_TYPES = ("Bug", "Task", "Story")
PRIORITIES = ("Highest", "High", "Medium", "Low", "Lowest")
STATUSES = ("To Do", "In Progress", "In Review", "Done")
TRANSITIONS = {  # name -> (from statuses, to)
    "Start progress": (("To Do",), "In Progress"),
    "Send to review": (("In Progress",), "In Review"),
    "Done": (("In Progress", "In Review"), "Done"),
    "Reopen": (("In Review", "Done"), "To Do"),
}
DEVELOPERS = {"dana.dev": "Dana (Core Dev)", "sam.sme": "Sam (SME)"}


def _empty() -> dict[str, Any]:
    return {"issues": {}, "next_id": 10001, "next_number": 1}


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")


class Comment(BaseModel):
    id: str
    author: str
    body: str
    created: str


class ChangeItem(BaseModel):
    field: str
    from_value: str | None
    to_value: str | None


class Change(BaseModel):
    id: str
    author: str
    created: str
    items: list[ChangeItem]


class Issue(BaseModel):
    id: str
    key: str
    summary: str
    description: str
    issuetype: str
    priority: str
    status: str
    labels: list[str]
    reporter: str
    assignee: str | None
    resolution: str | None
    created: str
    updated: str
    updated_by: str
    comments: list[Comment]
    changelog: list[Change]
    url: str


class Transition(BaseModel):
    name: str
    to: str


class Jira:
    def __init__(self, state_file: str | None = None, public_url: str | None = None) -> None:
        self.state = JsonState(Path(state_file) if state_file else None)
        self.state.load(_empty)
        self.public_url = (public_url or os.environ.get("JIRA_PUBLIC_URL") or "http://localhost:8060").rstrip("/")

    @property
    def issues(self) -> dict[str, dict[str, Any]]:
        return self.state.data["issues"]

    def view(self, i: dict[str, Any]) -> Issue:
        return Issue(**i, url=f"{self.public_url}/browse/{i['key']}")

    def find(self, key: str) -> dict[str, Any]:
        i = self.issues.get(key) or next((x for x in self.issues.values() if x["id"] == key), None)
        if i is None:
            raise fail("NOT_FOUND", f"issue {key} does not exist")
        return i

    def _next_id(self) -> str:
        n = self.state.data["next_id"]
        self.state.data["next_id"] += 1
        return str(n)

    def _change(self, i: dict[str, Any], user: str, items: list[dict[str, Any]]) -> None:
        t = iso(now())
        i["changelog"].append({"id": self._next_id(), "author": user, "created": t, "items": items})
        i["updated"], i["updated_by"] = t, user

    def create(self, user: str, *, project: str, issuetype: str, summary: str, description: str, priority: str,
               labels: list[str]) -> dict:
        if project != PROJECT:
            raise fail("INVALID_ARGUMENT", f"project {project} does not exist (use {PROJECT})")
        if issuetype not in ISSUE_TYPES:
            raise fail("INVALID_ARGUMENT", f"issue type must be one of {ISSUE_TYPES}")
        if priority not in PRIORITIES:
            raise fail("INVALID_ARGUMENT", f"priority must be one of {PRIORITIES}")
        if not summary.strip():
            raise fail("INVALID_ARGUMENT", "summary is required")
        with self.state.lock:
            key = f"{PROJECT}-{self.state.data['next_number']}"
            self.state.data["next_number"] += 1
            t = iso(now())
            i = {"id": self._next_id(), "key": key, "summary": summary[:255], "description": description,
                 "issuetype": issuetype, "priority": priority, "status": "To Do", "labels": list(labels),
                 "reporter": user, "assignee": None, "resolution": None, "created": t, "updated": t, "updated_by": user,
                 "comments": [], "changelog": []}
            self.issues[key] = i
            self.state.save()
            return i

    def transition(self, user: str, key: str, name: str) -> dict:
        with self.state.lock:
            i = self.find(key)
            if name not in TRANSITIONS:
                raise fail("INVALID_ARGUMENT", f"unknown transition {name}")
            frm, to = TRANSITIONS[name]
            if i["status"] == to:
                return i
            if i["status"] not in frm:
                raise fail("CONFLICT", f"transition '{name}' is not available from {i['status']}")
            self._change(i, user, [{"field": "status", "from_value": i["status"], "to_value": to}])
            i["status"] = to
            i["resolution"] = "Done" if to == "Done" else None
            self.state.save()
            return i

    def edit(self, user: str, key: str, fields: dict[str, Any]) -> dict:
        with self.state.lock:
            i = self.find(key)
            items = []
            for f in ("summary", "description", "priority", "assignee", "labels"):
                if f in fields and fields[f] is not None and fields[f] != i[f]:
                    if f == "priority" and fields[f] not in PRIORITIES:
                        raise fail("INVALID_ARGUMENT", f"priority must be one of {PRIORITIES}")
                    items.append({"field": f, "from_value": str(i[f]), "to_value": str(fields[f])})
                    i[f] = fields[f]
            if items:
                self._change(i, user, items)
                self.state.save()
            return i

    def comment(self, user: str, key: str, body: str) -> dict:
        if not body.strip():
            raise fail("INVALID_ARGUMENT", "comment body is empty")
        with self.state.lock:
            i = self.find(key)
            c = {"id": self._next_id(), "author": user, "body": body, "created": iso(now())}
            i["comments"].append(c)
            i["updated"], i["updated_by"] = c["created"], user
            self.state.save()
            return c

    def search(self, jql: str, limit: int) -> list[dict]:
        text = jql.strip()
        order = "ASC"
        if m := re.search(r"\s+ORDER\s+BY\s+updated\s*(ASC|DESC)?\s*$", text, re.I):
            order = (m.group(1) or "ASC").upper()
            text = text[: m.start()]
        preds = []
        for clause in [c.strip() for c in re.split(r"\s+AND\s+", text, flags=re.I) if c.strip()]:
            if m := re.fullmatch(r'project\s*=\s*"?(\w+)"?', clause, re.I):
                preds.append(lambda i, v=m.group(1): i["key"].startswith(v + "-"))
            elif m := re.fullmatch(r'labels\s*=\s*"?([\w.:-]+)"?', clause, re.I):
                preds.append(lambda i, v=m.group(1): v in i["labels"])
            elif m := re.fullmatch(r'updated\s*>=\s*"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})"', clause, re.I):
                since = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
                preds.append(lambda i, v=since: datetime.fromisoformat(i["updated"]) >= v)
            else:
                raise fail("INVALID_ARGUMENT", f"unsupported JQL clause: {clause}")
        rows = [i for i in self.issues.values() if all(p(i) for p in preds)]
        rows.sort(key=lambda i: i["updated"], reverse=order == "DESC")
        return rows[:limit]


def build_mcp(jira: Jira) -> MCPServer:
    mcp = MCPServer("jira", instructions=f"Jira Software (mock): project {PROJECT} for MES L3 bugs and changes. "
                                         f"Writes are attributed to {INTEGRATION_USER}.")
    u = INTEGRATION_USER

    @mcp.tool()
    def createJiraIssue(projectKey: str, issueTypeName: str, summary: str, description: str = "",  # noqa: N802,N803
                        priority: str = "Medium", labels: list[str] | None = None) -> Issue:
        """Create an issue. Put a unique label on it to find it again (idempotency)."""
        jira.state.check_fault()
        return jira.view(jira.create(u, project=projectKey, issuetype=issueTypeName, summary=summary,
                                     description=description, priority=priority, labels=labels or []))

    @mcp.tool()
    def getJiraIssue(issueIdOrKey: str) -> Issue:  # noqa: N802,N803
        """An issue with its comments and changelog."""
        jira.state.check_fault()
        return jira.view(jira.find(issueIdOrKey))

    @mcp.tool()
    def editJiraIssue(issueIdOrKey: str, fields: dict[str, Any]) -> Issue:  # noqa: N802,N803
        """Change summary, description, priority, assignee or labels."""
        jira.state.check_fault()
        return jira.view(jira.edit(u, issueIdOrKey, fields))

    @mcp.tool()
    def getTransitionsForJiraIssue(issueIdOrKey: str) -> list[Transition]:  # noqa: N802,N803
        """Workflow transitions available from the issue's current status."""
        i = jira.find(issueIdOrKey)
        return [Transition(name=n, to=to) for n, (frm, to) in TRANSITIONS.items() if i["status"] in frm]

    @mcp.tool()
    def transitionJiraIssue(issueIdOrKey: str, transition: str) -> Issue:  # noqa: N802,N803
        """Move the issue through the workflow (by transition name). A transition to the current status is a no-op."""
        jira.state.check_fault()
        return jira.view(jira.transition(u, issueIdOrKey, transition))

    @mcp.tool()
    def addCommentToJiraIssue(issueIdOrKey: str, commentBody: str) -> Comment:  # noqa: N802,N803
        """Add a comment."""
        jira.state.check_fault()
        return Comment(**jira.comment(u, issueIdOrKey, commentBody))

    @mcp.tool()
    def searchJiraIssuesUsingJql(jql: str, maxResults: int = 50) -> list[Issue]:  # noqa: N802,N803
        """Search with JQL (subset: project, labels, updated >= "YYYY-MM-DD HH:MM", ORDER BY updated)."""
        jira.state.check_fault()
        return [jira.view(i) for i in jira.search(jql, maxResults)]

    return mcp


def create_app(jira: Jira | None = None) -> FastAPI:
    jira = jira or Jira(os.environ.get("JIRA_STATE_FILE"))
    mcp = build_mcp(jira)
    app = FastAPI(title="Mock Jira", lifespan=mcp_lifespan(mcp))
    app.state.jira = jira
    app.state.mcp = mcp
    app.add_middleware(BearerKey, key=os.environ.get("JIRA_MCP_KEY", "jira-mcp-key"))
    @app.get("/_mock/health")
    async def health() -> dict:
        return {"status": "UP"}

    @app.get("/_mock/issues")
    async def mock_issues() -> list[dict]:
        return list(jira.issues.values())

    @app.post("/_mock/issues/{key}/act")
    async def mock_act(key: str, body: dict) -> dict:
        """Act as a developer (tests / smoke): {"user": "dana.dev", "transition": "Done", "comment": "..."}."""
        user = body.get("user", "dana.dev")
        if body.get("comment"):
            jira.comment(user, key, body["comment"])
        if body.get("transition"):
            jira.transition(user, key, body["transition"])
        return jira.find(key)

    @app.put("/_mock/faults")
    async def faults(body: dict) -> dict:
        jira.state.faults.fail_next = int(body.get("fail_next", 0))
        return {"fail_next": jira.state.faults.fail_next}

    @app.post("/_mock/reset")
    async def reset() -> dict:
        with jira.state.lock:
            jira.state.data = _empty()
            jira.state.save()
        return {"ok": True}

    mount_mcp(app, mcp)
    return app
