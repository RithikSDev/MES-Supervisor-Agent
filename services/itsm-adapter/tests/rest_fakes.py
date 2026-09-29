"""In-memory fakes of the ServiceNow Table API and Jira Cloud REST v3, served through `httpx.MockTransport`.

They implement only what the REST connectors use, but with the real wire formats: basic auth, `sysparm_query`
encoded queries (incl. RELATIVEGE), `sysparm_display_value=all`, sys_journal_field, JQL with relative `updated`,
Atlassian Document Format bodies, changelog pages and the usual error bodies.
"""

import base64
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx

from mock_itsm.servicenow import PRIORITY

SN_STATES = {"1": "New", "2": "In Progress", "3": "On Hold", "6": "Resolved", "7": "Closed", "8": "Canceled"}


def _basic(request: httpx.Request) -> tuple[str, str] | None:
    h = request.headers.get("authorization", "")
    if not h.startswith("Basic "):
        return None
    user, _, pw = base64.b64decode(h[6:]).decode().partition(":")
    return user, pw


def _sn_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


class FakeServiceNow:
    base = "https://dev00000.service-now.com"

    def __init__(self, user: str = "mes.integration", password: str = "pw", groups: tuple[str, ...] = ()) -> None:
        self.user, self.password = user, password
        self.groups: dict[str, str] = {uuid4().hex: g for g in groups}  # sys_id -> name
        self.incidents: dict[str, dict[str, Any]] = {}
        self.users: dict[str, dict[str, Any]] = {}  # sys_id -> {sys_id, user_name, name, email, active}
        self.roles: list[tuple[str, str]] = []  # (user sys_id, role)
        self.members: list[tuple[str, str]] = []  # (user sys_id, group sys_id)
        self.journal: list[dict[str, Any]] = []
        self.fail_next: list[int] = []  # status codes returned by the next requests
        self.requests: list[str] = []
        self._seq = 10000

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def group(self, name: str) -> str:
        return next(k for k, v in self.groups.items() if v == name)

    def add_user(self, user_name: str, name: str, email: str = "") -> str:
        uid = uuid4().hex
        self.users[uid] = {"sys_id": uid, "user_name": user_name, "name": name, "email": email, "active": "true"}
        return uid

    def by_correlation(self, cid: str) -> dict[str, Any]:
        return next(i for i in self.incidents.values() if i["correlation_id"] == cid)

    # ------------------------------------------------------------------ engineer actions (not through the API)
    def act(self, user: str, sys_id: str, changes: dict[str, Any] | None = None, work_note: str | None = None) -> None:
        self._write(user, sys_id, dict(changes or {}), work_note)

    def _write(self, user: str, sys_id: str, changes: dict[str, Any], note: str | None) -> dict[str, Any]:
        inc = self.incidents[sys_id]
        if changes.get("assigned_to") and changes["assigned_to"] not in self.users:
            raise ValueError("Invalid reference: assigned_to")
        if changes.get("state") in ("6", "7") and not ((changes.get("close_code") or inc["close_code"])
                                                        and (changes.get("close_notes") or inc["close_notes"])):
            raise ValueError("Resolution code and resolution notes are mandatory")
        inc.update({k: str(v) for k, v in changes.items() if k in inc})
        inc["priority"] = PRIORITY[(inc["impact"], inc["urgency"])]
        if note:
            self.journal.append({"sys_id": uuid4().hex, "name": "incident", "element_id": sys_id, "element": "work_notes", "value": note,
                                 "sys_created_on": _sn_now(), "sys_created_by": user})
        inc["sys_updated_on"], inc["sys_updated_by"] = _sn_now(), user
        return inc

    # ------------------------------------------------------------------ HTTP
    def _err(self, status: int, message: str, detail: str = "") -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": message, "detail": detail}, "status": "failure"})

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url.path}")
        if self.fail_next:
            return self._err(self.fail_next.pop(0), "Service Unavailable")
        if _basic(request) != (self.user, self.password):
            return self._err(401, "User Not Authenticated", "Required to provide Auth information")
        m = re.fullmatch(r"/api/now/table/(\w+)(?:/(\w+))?", request.url.path)
        if not m:
            return self._err(400, "Invalid table")
        table, sys_id = m.groups()
        p = request.url.params
        body = json.loads(request.content) if request.content else {}
        rows = {"incident": self.incidents, "sys_user_group": None, "sys_journal_field": None, "sys_user": None,
                "sys_user_has_role": None, "sys_user_grmember": None}
        if table not in rows:
            return self._err(403, "User Not Authorized")
        if request.method == "GET":
            return httpx.Response(200, json={"result": [self._out(table, r, p) for r in self._query(table, p.get("sysparm_query", ""))]
                                             [: int(p.get("sysparm_limit", 10000))]})
        if table == "sys_user_group" and request.method == "POST":
            gid = uuid4().hex
            self.groups[gid] = body["name"]
            return httpx.Response(201, json={"result": {"sys_id": gid, "name": body["name"]}})
        if table == "sys_user" and request.method == "POST":
            uid = self.add_user(body["user_name"], f"{body['first_name']} {body['last_name']}", body.get("email", ""))
            return httpx.Response(201, json={"result": self.users[uid]})
        if table in ("sys_user_has_role", "sys_user_grmember") and request.method == "POST":
            (self.roles if table == "sys_user_has_role" else self.members).append(
                (body["user"], body["role"] if table == "sys_user_has_role" else body["group"]))
            return httpx.Response(201, json={"result": {"sys_id": uuid4().hex}})
        if table == "incident" and request.method == "POST":
            if body["assignment_group"] not in self.groups:
                return self._err(400, "Invalid reference", "assignment_group")
            self._seq += 1
            sid = uuid4().hex
            self.incidents[sid] = {
                "sys_id": sid, "number": f"INC00{self._seq}", "state": "1", "impact": body["impact"],
                "urgency": body["urgency"], "priority": PRIORITY[(body["impact"], body["urgency"])],
                "assignment_group": body["assignment_group"], "correlation_id": body["correlation_id"],
                "correlation_display": body.get("correlation_display", ""), "contact_type": body.get("contact_type", ""),
                "short_description": body["short_description"], "description": body.get("description", ""),
                "category": body.get("category", ""), "close_code": "", "close_notes": "", "hold_reason": "",
                "assigned_to": body.get("assigned_to", ""), "opened_at": body.get("opened_at") or _sn_now(),
                "sys_updated_on": _sn_now(), "sys_updated_by": self.user,
            }
            return httpx.Response(201, json={"result": self._out("incident", self.incidents[sid], p)})
        if table == "incident" and request.method == "PATCH":
            if sys_id not in self.incidents:
                return self._err(404, "No Record found")
            note = body.pop("work_notes", None)
            try:
                inc = self._write(self.user, sys_id, body, note)
            except ValueError as e:
                return self._err(403, "Operation Failed", str(e))
            return httpx.Response(200, json={"result": self._out("incident", inc, p)})
        return self._err(405, "Method not allowed")

    def _query(self, table: str, q: str) -> list[dict[str, Any]]:
        rows = (list(self.incidents.values()) if table == "incident"
                else [{"sys_id": k, "name": v} for k, v in self.groups.items()] if table == "sys_user_group"
                else list(self.users.values()) if table == "sys_user"
                else [{"sys_id": f"r{i}", "user": u, "role.name": r} for i, (u, r) in enumerate(self.roles)]
                if table == "sys_user_has_role"
                else [{"sys_id": f"m{i}", "user": u, "group": g} for i, (u, g) in enumerate(self.members)]
                if table == "sys_user_grmember"
                else list(self.journal))
        order = None
        for term in filter(None, q.split("^")):
            if term.startswith("ORDERBY"):
                order = term[7:]
            elif m := re.fullmatch(r"(\w+)RELATIVEGE@minute@ago@(\d+)", term):
                since = (datetime.now(UTC) - timedelta(minutes=int(m[2]))).strftime("%Y-%m-%d %H:%M:%S")
                rows = [r for r in rows if r[m[1]] >= since]
            elif m := re.fullmatch(r"(\w+)STARTSWITH(.*)", term):
                rows = [r for r in rows if str(r.get(m[1], "")).startswith(m[2])]
            elif m := re.fullmatch(r"(\w+)IN(.*)", term):
                rows = [r for r in rows if r.get(m[1]) in m[2].split(",")]
            elif m := re.fullmatch(r"(\w+)ISNOTEMPTY", term):
                rows = [r for r in rows if r.get(m[1])]
            elif m := re.fullmatch(r"([\w.]+)=(.*)", term):
                rows = [r for r in rows if str(r.get(m[1], "")) == m[2]]
            else:
                raise AssertionError(f"unsupported query term {term}")
        return sorted(rows, key=lambda r: r[order]) if order else rows

    def _out(self, table: str, r: dict[str, Any], p: httpx.QueryParams) -> dict[str, Any]:
        fields = p.get("sysparm_fields")
        out = {k: v for k, v in r.items() if not fields or k in fields.split(",")}
        if table == "incident" and p.get("sysparm_display_value") == "all":
            disp = {"assignment_group": self.groups.get(r["assignment_group"], ""), "state": SN_STATES.get(r["state"], ""),
                    "assigned_to": self.users.get(r.get("assigned_to") or "", {}).get("name", "")}
            out = {k: {"value": v, "display_value": disp.get(k, v)} for k, v in out.items()}
        return out


def _jira_ts(dt: datetime | None = None) -> str:
    return (dt or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%S.") + f"{(dt or datetime.now(UTC)).microsecond // 1000:03d}+0000"


def _adf_to_text(doc: dict[str, Any]) -> str:
    return "\n".join("".join(t.get("text", "") for t in para.get("content", [])) for para in doc.get("content", []))


def _adf(text: str) -> dict[str, Any]:
    return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


class FakeJira:
    site = "https://example.atlassian.net"
    STATUSES = {"Start progress": "In Progress", "Done": "Done"}

    def __init__(self, email: str = "mes@example.com", token: str = "tok", project: str = "MES",
                 priority_on_screen: bool = True) -> None:
        self.email, self.token, self.project = email, token, project
        self.account_id = "acc-mes-integration"
        self.priority_on_screen = priority_on_screen
        self.issues: dict[str, dict[str, Any]] = {}
        self.requests: list[str] = []
        self._seq = 0

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def by_label(self, label: str) -> dict[str, Any]:
        return next(i for i in self.issues.values() if label in i["labels"])

    # ------------------------------------------------------------------ developer actions
    def act(self, account: str, key: str, *, transition: str | None = None, comment: str | None = None) -> None:
        i = self.issues[key]
        if comment:
            self._comment(account, i, _adf(comment))
        if transition:
            to = self.STATUSES[transition]
            i["changelog"].append({"id": str(10000 + len(i["changelog"]) + 100 * int(i["id"])), "created": _jira_ts(),
                                   "author": {"accountId": account, "displayName": account},
                                   "items": [{"field": "status", "fromString": i["status"], "toString": to}]})
            i["status"] = to
            i["updated"] = _jira_ts()

    def _comment(self, account: str, i: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        c = {"id": str(20000 + sum(len(x["comments"]) for x in self.issues.values())), "body": body, "created": _jira_ts(),
             "author": {"accountId": account, "displayName": "MES Integration" if account == self.account_id else account}}
        i["comments"].append(c)
        i["updated"] = _jira_ts()
        return c

    # ------------------------------------------------------------------ HTTP
    def _raw(self, i: dict[str, Any]) -> dict[str, Any]:
        return {"id": i["id"], "key": i["key"], "fields": {
            "summary": i["summary"], "status": {"name": i["status"]}, "priority": {"name": i["priority"]},
            "labels": i["labels"], "updated": i["updated"], "issuetype": {"name": i["issuetype"]}}}

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/rest/api/3")
        self.requests.append(f"{request.method} {path}")
        if _basic(request) != (self.email, self.token):
            return httpx.Response(401, text="Client must be authenticated to access this resource.")
        body = json.loads(request.content) if request.content else {}
        if path == "/myself":
            return httpx.Response(200, json={"accountId": self.account_id, "displayName": "MES Integration"})
        if m := re.fullmatch(r"/project/(\w+)", path):
            if m[1] != self.project:
                return httpx.Response(404, json={"errorMessages": [f"No project could be found with key '{m[1]}'."], "errors": {}})
            return httpx.Response(200, json={"key": self.project, "issueTypes": [{"name": "Bug"}, {"name": "Task"}, {"name": "Epic"}]})
        if path == "/search/jql" and request.method == "POST":
            return httpx.Response(200, json={"issues": [self._raw(i) for i in self._jql(body["jql"])][: body.get("maxResults", 50)]})
        if path == "/issue" and request.method == "POST":
            f = body["fields"]
            errors = {}
            if f["project"]["key"] != self.project:
                errors["project"] = "valid project is required"
            if "priority" in f and not self.priority_on_screen:
                errors["priority"] = "Field 'priority' cannot be set. It is not on the appropriate screen, or unknown."
            if errors:
                return httpx.Response(400, json={"errorMessages": [], "errors": errors})
            self._seq += 1
            key = f"{self.project}-{self._seq}"
            self.issues[key] = {"id": str(self._seq), "key": key, "summary": f["summary"], "status": "To Do",
                                "priority": (f.get("priority") or {}).get("name", "Medium"), "labels": f["labels"],
                                "issuetype": f["issuetype"]["name"], "description": _adf_to_text(f["description"]),
                                "updated": _jira_ts(), "comments": [], "changelog": []}
            return httpx.Response(201, json={"id": str(self._seq), "key": key})
        if m := re.fullmatch(r"/issue/([\w-]+)(/comment|/changelog)?", path):
            i = self.issues.get(m[1])
            if i is None:
                return httpx.Response(404, json={"errorMessages": ["Issue does not exist or you do not have permission to see it."]})
            if m[2] == "/comment" and request.method == "POST":
                return httpx.Response(201, json=self._comment(self.account_id, i, body["body"]))
            if m[2] == "/comment":
                return httpx.Response(200, json={"comments": i["comments"], "total": len(i["comments"])})
            if m[2] == "/changelog":
                return httpx.Response(200, json={"values": i["changelog"], "isLast": True})
            return httpx.Response(200, json=self._raw(i))
        return httpx.Response(404, json={"errorMessages": ["not found"]})

    def _jql(self, jql: str) -> list[dict[str, Any]]:
        where, _, order = jql.partition(" ORDER BY ")
        rows = list(self.issues.values())
        for cond in where.split(" AND "):
            m = re.fullmatch(r'\s*(\w+)\s*(=|>=)\s*"?([^"]*)"?\s*', cond)
            assert m, cond
            field, op, v = m.groups()
            if field == "project":
                rows = [i for i in rows if i["key"].startswith(f"{v}-")]
            elif field == "labels":
                rows = [i for i in rows if v in i["labels"]]
            elif field == "updated" and op == ">=":
                since = _jira_ts(datetime.now(UTC) - timedelta(minutes=int(v.removeprefix("-").removesuffix("m"))))
                rows = [i for i in rows if i["updated"] >= since]
            else:
                raise AssertionError(cond)
        return sorted(rows, key=lambda i: i["updated"], reverse="DESC" in order)
