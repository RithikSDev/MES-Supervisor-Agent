"""Calls to the MES APIs (core, ERP adapter, ITSM adapter).

By default a call runs as the agent's own identity: Keycloak client credentials of `mes-support-agent`
(realm role `ai_support_agent`). Two cases use someone else's token instead, so MES records the right person:
  - an approved proposal runs with the approver's token (MES checks that person may do it, and audits them);
  - the ticket raised from a terminal chat is created with the operator's terminal session token;
  - in a support engineer's chat every call runs as that engineer (`Api.as_user`): they see and change only what
    MES allows them.
"""

import time
from typing import Any, Literal

import httpx

from mes_common.logs import request_id_var
from support_agent.settings import Settings

Service = Literal["core", "erp", "itsm"]


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message

    def __str__(self) -> str:
        return f"HTTP {self.status}: {self.message}"


class Api:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self.s = settings
        self._http = http or httpx.AsyncClient(timeout=20)
        self._base = {"core": settings.core_api_url, "erp": settings.erp_api_url, "itsm": settings.itsm_api_url}
        self._token: str | None = None
        self._token_exp = 0.0
        self._plant_id: str | None = None

    async def service_token(self) -> str:
        if not self._token or time.monotonic() > self._token_exp - 30:
            r = await self._http.post(self.s.token_url, data={
                "grant_type": "client_credentials", "client_id": self.s.client_id, "client_secret": self.s.client_secret})
            if r.status_code >= 400:
                raise ApiError(r.status_code, f"agent service account login failed: {r.text[:200]}")
            body = r.json()
            self._token, self._token_exp = body["access_token"], time.monotonic() + float(body.get("expires_in", 60))
        return self._token

    async def call(self, service: Service, method: str, path: str, *, params: dict[str, Any] | None = None,
                   json: Any = None, token: str | None = None) -> Any:
        bearer = token or await self.service_token()
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {"Authorization": f"Bearer {bearer}"}
        if rid := request_id_var.get():
            headers["X-Request-ID"] = rid
        r = await self._http.request(method, f"{self._base[service]}{path}", params=clean, json=json, headers=headers)
        if r.status_code >= 400:
            try:
                body = r.json()
                msg = body.get("message") or body.get("detail") or r.text
            except ValueError:
                msg = r.text
            raise ApiError(r.status_code, str(msg)[:500])
        return r.json() if r.content else None

    async def get(self, path: str, service: Service = "core", **params: Any) -> Any:
        return await self.call(service, "GET", path, params=params)

    async def post(self, path: str, body: Any = None, service: Service = "core", token: str | None = None) -> Any:
        return await self.call(service, "POST", path, json=body, token=token)

    async def loki(self, query: str, *, start_ns: int, end_ns: int, limit: int, direction: str = "backward") -> Any:
        """LogQL range query against the log store (no auth inside the stack)."""
        r = await self._http.get(f"{self.s.loki_url}/loki/api/v1/query_range", params={
            "query": query, "start": start_ns, "end": end_ns, "limit": limit, "direction": direction})
        if r.status_code >= 400:
            raise ApiError(r.status_code, f"log store: {r.text[:300]}")
        return r.json()["data"]["result"]

    async def health(self, service: Service) -> tuple[bool, str]:
        """Readiness of a MES service (its /health/ready next to /api/v1)."""
        base = self._base[service].rsplit("/api/v1", 1)[0]
        try:
            r = await self._http.get(f"{base}/health/ready", timeout=5)
            return r.status_code == 200, f"HTTP {r.status_code}"
        except httpx.HTTPError as e:
            return False, type(e).__name__

    async def whoami(self, token: str) -> dict[str, Any]:
        """Who a (terminal or Keycloak) token belongs to, according to MES."""
        return await self.call("core", "GET", "/me", token=token)

    def as_user(self, token: str) -> UserApi:
        return UserApi(self, token)

    async def plant_id(self) -> str:
        if self._plant_id is None:
            plants = await self.get("/plants")
            self._plant_id = next((p for p in plants if p["code"] == self.s.plant_code), plants[0])["id"]
        return self._plant_id

    async def aclose(self) -> None:
        await self._http.aclose()


class UserApi:
    """The same calls, but as one person (their bearer token) unless a call names another token."""

    def __init__(self, api: Api, token: str) -> None:
        self._api, self._user_token, self.s = api, token, api.s

    async def call(self, service: Service, method: str, path: str, *, params: dict[str, Any] | None = None,
                   json: Any = None, token: str | None = None) -> Any:
        return await self._api.call(service, method, path, params=params, json=json, token=token or self._user_token)

    async def get(self, path: str, service: Service = "core", **params: Any) -> Any:
        return await self.call(service, "GET", path, params=params)

    async def post(self, path: str, body: Any = None, service: Service = "core", token: str | None = None) -> Any:
        return await self.call(service, "POST", path, json=body, token=token)

    async def loki(self, query: str, *, start_ns: int, end_ns: int, limit: int, direction: str = "backward") -> Any:
        return await self._api.loki(query, start_ns=start_ns, end_ns=end_ns, limit=limit, direction=direction)

    async def health(self, service: Service) -> tuple[bool, str]:
        return await self._api.health(service)
