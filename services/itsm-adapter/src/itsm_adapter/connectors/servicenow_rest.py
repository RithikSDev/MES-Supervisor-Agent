"""ServiceNow over its standard REST Table API (works with any instance, incl. a free Personal Developer Instance).

  incidents      GET/POST/PATCH /api/now/table/incident
  groups         GET /api/now/table/sys_user_group   (assignment group name -> sys_id, cached)
  users          GET /api/now/table/sys_user         ("Assigned to": MES assignee e-mail / user name -> sys_id)
  work notes     PATCH incident {work_notes: ...}; read back from /api/now/table/sys_journal_field
Auth: basic auth of an integration user (roles: itil, plus read on sys_journal_field and sys_user_group).

Time filters use ServiceNow's relative operators (`RELATIVEGE@minute@ago@N`), which don't depend on the
integration user's time zone; returned `sys_*_on` values are the instance's internal UTC timestamps.
"""

import math
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from itsm_adapter.calls import begin, ms
from itsm_adapter.errors import ItsmError, ItsmErrorKind, from_response, from_transport

INCIDENT_FIELDS = ("sys_id,number,state,priority,impact,urgency,assignment_group,assigned_to,correlation_id,close_notes,"
                   "sys_updated_on,sys_updated_by,short_description")
# incident.hold_reason: 1 Awaiting Caller, 3 Awaiting Problem, 4 Awaiting Vendor, 5 Awaiting Change
HOLD_REASON = "1"


def _minutes_since(since: datetime) -> int:
    return max(1, math.ceil((datetime.now(UTC) - since).total_seconds() / 60) + 1)


class ServiceNowRest:
    system = "servicenow"
    mode = "rest"

    def __init__(self, instance_url: str, user: str, password: str, *, close_code: str = "Solution provided", contact_type: str = "",
                 timeout_s: float = 15.0, http: httpx.AsyncClient | None = None) -> None:
        self.base = self.endpoint = instance_url.rstrip("/")
        self.user = user
        self.close_code = close_code
        self.contact_type = contact_type  # incident.contact_type choice value; empty = not set
        self._http = http or httpx.AsyncClient(timeout=timeout_s)
        self._auth = httpx.BasicAuth(user, password)
        self._groups: dict[str, str] = {}  # name -> sys_id
        self._group_names: dict[str, str] = {}  # sys_id -> name
        self._users: dict[str, dict[str, Any] | None] = {}  # "email:x" / "user:x" / sys_id -> user (None: not there)

    @property
    def configured(self) -> bool:
        return bool(self.base and self.user)

    def is_mine(self, user_name: str) -> bool:
        return user_name == self.user

    # ------------------------------------------------------------------ HTTP
    async def _call(self, method: str, path: str, what: str, *, params: dict[str, Any] | None = None,
                    json: Any = None) -> Any:
        write = method != "GET"
        entry, started = begin(self.system, f"{method} {path}", {"what": what})
        try:
            resp = await self._http.request(method, f"{self.base}{path}", params=params, json=json, auth=self._auth,
                                            headers={"Accept": "application/json"})
        except httpx.TransportError as e:
            entry.update(outcome="TRANSPORT", ms=ms(started))
            raise from_transport(self.system, what, e, write=write) from e
        entry.update(ms=ms(started), status=resp.status_code, outcome="OK" if resp.status_code < 400 else "ERROR")
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error") or {}
                detail = f"{err.get('message')}: {err.get('detail')}" if err else resp.text[:300]
            except ValueError:
                detail = resp.text[:300]
            entry["error"] = str(detail)[:300]
            raise from_response(self.system, what, resp, detail)
        return resp.json().get("result") if resp.content else None

    # ------------------------------------------------------------------ normalization
    def _url(self, sys_id: str) -> str:
        return f"{self.base}/nav_to.do?uri={quote(f'incident.do?sys_id={sys_id}', safe='')}"

    def _incident(self, r: dict[str, Any]) -> dict[str, Any]:
        def val(f: str) -> Any:
            v = r.get(f)
            return v.get("value") if isinstance(v, dict) else v

        def disp(f: str) -> Any:
            v = r.get(f)
            return v.get("display_value") if isinstance(v, dict) else v

        user_id = val("assigned_to")
        group_id = val("assignment_group")
        group = disp("assignment_group") if isinstance(r.get("assignment_group"), dict) else self._group_names.get(group_id or "")
        return {
            "sys_id": val("sys_id"), "number": val("number"), "state": str(val("state") or ""),
            "priority": str(val("priority") or ""), "impact": str(val("impact") or ""), "urgency": str(val("urgency") or ""),
            "assignment_group": group or None, "correlation_id": val("correlation_id") or None,
            "assigned_to": {"sys_id": user_id, "name": disp("assigned_to")} if user_id else None,
            "close_notes": val("close_notes") or None, "sys_updated_on": val("sys_updated_on"),
            "sys_updated_by": val("sys_updated_by"), "url": self._url(val("sys_id")),
        }

    async def group_id(self, name: str) -> str:
        if name not in self._groups:
            rows = await self._call("GET", "/api/now/table/sys_user_group", "find assignment group",
                                    params={"sysparm_query": f"name={name}", "sysparm_fields": "sys_id,name",
                                            "sysparm_limit": 1})
            if not rows:
                raise ItsmError(ItsmErrorKind.PERMANENT,
                                f"servicenow: assignment group '{name}' does not exist (create it, or map the tier to an "
                                "existing group in the itsm-adapter settings)")
            self._groups[name] = rows[0]["sys_id"]
            self._group_names[rows[0]["sys_id"]] = name
        return self._groups[name]

    async def ensure_group(self, name: str, *, create: bool) -> str:
        """'exists' | 'created' | 'missing' (setup check)."""
        try:
            await self.group_id(name)
            return "exists"
        except ItsmError as e:
            if e.kind != ItsmErrorKind.PERMANENT or "does not exist" not in e.message or not create:
                if "does not exist" in e.message:
                    return "missing"
                raise
        await self._call("POST", "/api/now/table/sys_user_group", "create assignment group",
                         json={"name": name, "description": "MES support tier (created by the MES ITSM adapter setup)"})
        return "created"

    async def _user(self, query: str, cache_key: str) -> dict[str, Any] | None:
        if cache_key not in self._users:
            rows = await self._call("GET", "/api/now/table/sys_user", "find user", params={
                "sysparm_query": query, "sysparm_fields": "sys_id,user_name,name,email", "sysparm_limit": 1})
            self._users[cache_key] = rows[0] if rows else None
        return self._users[cache_key]

    async def find_user(self, email: str | None, user_name: str | None) -> dict[str, Any] | None:
        """A ServiceNow user by e-mail, else by user name: {sys_id, user_name, name, email} or None."""
        if email and (u := await self._user(f"email={email}^active=true", f"email:{email.lower()}")):
            return u
        if user_name and (u := await self._user(f"user_name={user_name}^active=true", f"user:{user_name.lower()}")):
            return u
        return None

    async def get_user(self, sys_id: str) -> dict[str, Any] | None:
        return await self._user(f"sys_id={sys_id}", sys_id)

    async def ensure_user(self, user_name: str, name: str, email: str | None, group: str | None, *,
                          create: bool) -> dict[str, Any]:
        """Setup check of one MES support engineer. Assigned to only lists active users with role itil who are in the
        incident's assignment group, so all three are checked. create=True creates a missing user and adds a missing
        itil role / group membership. -> {status: exists | created | missing, user_name, itil, in_group, fixed}."""
        user = await self.find_user(email, user_name)
        fixed: list[str] = []
        if user is None:
            if not create:
                return {"status": "missing", "user_name": None, "itil": None, "in_group": None, "fixed": []}
            first, _, last = name.partition(" ")
            user = await self._call("POST", "/api/now/table/sys_user", "create user", json={
                "user_name": user_name, "first_name": first, "last_name": last or first, "email": email or "",
                "active": "true"}, params={"sysparm_fields": "sys_id,user_name,name,email"})
            self._users.pop(f"email:{(email or '').lower()}", None)
            self._users.pop(f"user:{user_name.lower()}", None)
            status = "created"
        else:
            status = "exists"
        uid = user["sys_id"]
        itil = bool(await self._call("GET", "/api/now/table/sys_user_has_role", "check the itil role", params={
            "sysparm_query": f"user={uid}^role.name=itil", "sysparm_fields": "sys_id", "sysparm_limit": 1}))
        if not itil and create:
            await self._call("POST", "/api/now/table/sys_user_has_role", "give the user the itil role",
                             json={"user": uid, "role": "itil"})
            itil = True
            fixed.append("itil role")
        in_group = None
        if group:
            gid = await self.group_id(group)
            in_group = bool(await self._call("GET", "/api/now/table/sys_user_grmember", "check the group membership",
                                             params={"sysparm_query": f"user={uid}^group={gid}", "sysparm_fields": "sys_id",
                                                     "sysparm_limit": 1}))
            if not in_group and create:
                await self._call("POST", "/api/now/table/sys_user_grmember", "add the user to the group",
                                 json={"user": uid, "group": gid})
                in_group = True
                fixed.append(f"member of {group}")
        return {"status": status, "user_name": user.get("user_name"), "itil": itil, "in_group": in_group,
                "fixed": fixed if status == "exists" else []}

    async def can_read_journal(self) -> bool | None:
        """True: work notes are readable; False: none visible although incidents have some (ACL); None: can't tell."""
        notes = await self._call("GET", "/api/now/table/sys_journal_field", "read work notes",
                                 params={"sysparm_query": "name=incident^element=work_notes", "sysparm_fields": "sys_id",
                                         "sysparm_limit": 1})
        if notes:
            return True
        with_notes = await self._call("GET", "/api/now/table/incident", "read incidents", params={
            "sysparm_query": "work_notesISNOTEMPTY", "sysparm_fields": "sys_id", "sysparm_limit": 1})
        return False if with_notes else None

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ port
    async def find_by_correlation(self, correlation_id: str) -> dict[str, Any] | None:
        rows = await self._call("GET", "/api/now/table/incident", "find incident", params={
            "sysparm_query": f"correlation_id={correlation_id}", "sysparm_fields": INCIDENT_FIELDS,
            "sysparm_display_value": "all", "sysparm_limit": 1})
        return self._incident(rows[0]) if rows else None

    async def create(self, fields: dict[str, Any]) -> dict[str, Any]:
        body = {
            "short_description": fields["short_description"], "description": fields.get("description", ""),
            "impact": str(fields["impact"]), "urgency": str(fields["urgency"]), "category": fields.get("category") or "inquiry",
            "assignment_group": await self.group_id(fields["assignment_group"]),
            "correlation_id": fields["correlation_id"], "correlation_display": "MES",
        }
        if self.contact_type:
            body["contact_type"] = self.contact_type
        if fields.get("opened_at"):  # backdated MES tickets (e.g. demo history) keep their real opening time
            body["opened_at"] = fields["opened_at"]
        if fields.get("assigned_to"):
            body["assigned_to"] = fields["assigned_to"]
        row = await self._call("POST", "/api/now/table/incident", "create incident", json=body,
                               params={"sysparm_fields": INCIDENT_FIELDS, "sysparm_display_value": "all"})
        return self._incident(row)

    async def update(self, sys_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {}
        for k in ("state", "impact", "urgency"):
            if changes.get(k) is not None:
                body[k] = str(changes[k])
        for k in ("short_description", "description", "category"):
            if changes.get(k):
                body[k] = changes[k]
        if changes.get("assignment_group"):
            body["assignment_group"] = await self.group_id(changes["assignment_group"])
            body["assigned_to"] = ""
        if "assigned_to" in changes:  # sys_id, or "" = nobody
            body["assigned_to"] = changes["assigned_to"] or ""
        if body.get("state") == "3":
            body["hold_reason"] = HOLD_REASON
        if body.get("state") in ("6", "7"):
            body["close_code"] = self.close_code
            body["close_notes"] = changes.get("close_notes") or "Resolved in MES"
        row = await self._call("PATCH", f"/api/now/table/incident/{sys_id}", "update incident", json=body,
                               params={"sysparm_fields": INCIDENT_FIELDS, "sysparm_display_value": "all"})
        return self._incident(row)

    async def journal(self, sys_id: str, since: datetime | None = None) -> list[dict[str, Any]]:
        q = f"element_id={sys_id}^elementINwork_notes,comments"
        if since is not None:
            q += f"^sys_created_onRELATIVEGE@minute@ago@{_minutes_since(since)}"
        rows = await self._call("GET", "/api/now/table/sys_journal_field", "read work notes", params={
            "sysparm_query": q + "^ORDERBYsys_created_on", "sysparm_fields": "sys_id,element,value,sys_created_on,sys_created_by",
            "sysparm_limit": 500})
        return [{k: r.get(k) for k in ("sys_id", "element", "value", "sys_created_on", "sys_created_by")} for r in rows or []]

    async def add_work_note(self, sys_id: str, text: str) -> None:
        await self._call("PATCH", f"/api/now/table/incident/{sys_id}", "add work note", json={"work_notes": text},
                         params={"sysparm_fields": "sys_id"})

    async def changed_since(self, since: datetime) -> list[dict[str, Any]]:
        rows = await self._call("GET", "/api/now/table/incident", "read changed incidents", params={
            "sysparm_query": f"correlation_idSTARTSWITHMES:^sys_updated_onRELATIVEGE@minute@ago@{_minutes_since(since)}"
                             "^ORDERBYsys_updated_on",
            "sysparm_fields": INCIDENT_FIELDS, "sysparm_display_value": "all", "sysparm_limit": 200})
        return [self._incident(r) for r in rows or []]

    async def health(self) -> dict[str, Any]:
        await self._call("GET", "/api/now/table/incident", "connect", params={"sysparm_limit": 1, "sysparm_fields": "sys_id"})
        return {"detail": [f"Table API as {self.user}"]}
