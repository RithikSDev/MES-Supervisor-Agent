"""Thin client for the MES core API. The MCP server holds no data and no business rules: every tool is a call to
the core API as the `service-account-mes-mcp` identity (Keycloak client credentials, realm role `ai_agent`), so
core's permissions, plant scoping and audit trail apply to everything an agent does."""

import time
from functools import lru_cache
from typing import Any

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    core_api_url: str = "http://localhost:8000/api/v1"
    token_url: str = "http://localhost:8080/realms/mes/protocol/openid-connect/token"
    client_id: str = "mes-mcp"
    client_secret: str = "mes-mcp-secret"
    plant_code: str = "P1000"
    # key MCP clients present as `Authorization: Bearer <key>`
    mcp_api_key: str = "mes-mcp-demo-key"
    admin_ui_url: str = "http://localhost:3080"


@lru_cache
def get_settings() -> Settings:
    return Settings()


class CoreError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class Core:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self.s = settings
        self._http = http or httpx.AsyncClient(timeout=15)
        self._token: str | None = None
        self._token_exp = 0.0
        self._plant: dict[str, Any] | None = None

    async def _auth(self) -> dict[str, str]:
        if not self._token or time.monotonic() > self._token_exp - 30:
            r = await self._http.post(self.s.token_url, data={
                "grant_type": "client_credentials", "client_id": self.s.client_id, "client_secret": self.s.client_secret})
            r.raise_for_status()
            body = r.json()
            self._token, self._token_exp = body["access_token"], time.monotonic() + float(body.get("expires_in", 60))
        return {"Authorization": f"Bearer {self._token}"}

    async def request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json: Any = None) -> Any:
        r = await self._http.request(method, f"{self.s.core_api_url}{path}", params=params, json=json,
                                     headers=await self._auth())
        if r.status_code >= 400:
            try:
                err = r.json()
                raise CoreError(r.status_code, err.get("code") or str(r.status_code), err.get("message") or err.get("detail") or r.text)
            except ValueError:
                raise CoreError(r.status_code, str(r.status_code), r.text[:300]) from None
        return r.json() if r.content else None

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params={k: v for k, v in params.items() if v is not None})

    async def post(self, path: str, body: Any) -> Any:
        return await self.request("POST", path, json=body)

    async def plant(self) -> dict[str, Any]:
        if self._plant is None:
            plants = await self.get("/plants")
            self._plant = next((p for p in plants if p["code"] == self.s.plant_code), plants[0])
        return self._plant

    async def plant_id(self) -> str:
        return (await self.plant())["id"]
