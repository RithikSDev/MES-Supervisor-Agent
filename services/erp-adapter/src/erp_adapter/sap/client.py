"""HTTP client for SAP Gateway OData V2: basic auth, CSRF handshake, error classification."""

import logging
from typing import Any

import httpx

from erp_adapter.ports import ErpError, ErpErrorKind

log = logging.getLogger(__name__)

ODATA_ROOT = "/sap/opu/odata/sap"


class SapODataClient:
    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        *,
        timeout_s: float = 5.0,
        retryable_codes: set[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            auth=(user, password),
            timeout=httpx.Timeout(timeout_s, connect=3.0),
            headers={"Accept": "application/json"},
            transport=transport,
        )
        self._csrf: dict[str, str] = {}  # per service
        self._retryable_codes = retryable_codes or set()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(self, service: str, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        url = f"{ODATA_ROOT}/{service}/{path}"
        try:
            resp = await self._http.get(url, params={"$format": "json", **(params or {})})
        except httpx.TransportError as e:
            # Reads have no side effects, so every transport failure is safe to retry.
            raise ErpError(ErpErrorKind.RETRYABLE, f"GET {path}: {e!r}") from e
        self._raise_for_status(resp, f"GET {path}")
        return resp.json()

    async def post(self, service: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{ODATA_ROOT}/{service}/{path}"
        for attempt in (1, 2):
            token = await self._csrf_token(service, refresh=attempt == 2)
            try:
                resp = await self._http.post(url, json=body, headers={"x-csrf-token": token})
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                raise ErpError(ErpErrorKind.RETRYABLE, f"POST {path}: cannot connect: {e!r}") from e
            except httpx.TransportError as e:
                # The request may have reached SAP and been committed; we cannot tell.
                raise ErpError(ErpErrorKind.AMBIGUOUS, f"POST {path}: no response: {e!r}") from e
            if resp.status_code == 403 and resp.headers.get("x-csrf-token", "").lower() == "required" and attempt == 1:
                log.info("CSRF token rejected for %s, refetching", service)
                continue
            self._raise_for_status(resp, f"POST {path}")
            return resp.json()
        raise ErpError(ErpErrorKind.RETRYABLE, "CSRF token repeatedly rejected")

    async def _csrf_token(self, service: str, *, refresh: bool) -> str:
        if refresh or service not in self._csrf:
            try:
                resp = await self._http.get(f"{ODATA_ROOT}/{service}/", headers={"x-csrf-token": "Fetch"})
            except httpx.TransportError as e:
                raise ErpError(ErpErrorKind.RETRYABLE, f"CSRF fetch: {e!r}") from e
            self._raise_for_status(resp, "CSRF fetch")
            token = resp.headers.get("x-csrf-token")
            if not token:
                raise ErpError(ErpErrorKind.RETRYABLE, "SAP did not return a CSRF token")
            self._csrf[service] = token
        return self._csrf[service]

    def _raise_for_status(self, resp: httpx.Response, what: str) -> None:
        if resp.status_code < 400:
            return
        code, message = _parse_error(resp)
        text = f"{what} -> HTTP {resp.status_code}: {message}"
        if resp.status_code in (401, 403):
            kind = ErpErrorKind.PERMANENT  # credentials/authorisation: needs a human
        elif resp.status_code in (502, 504):
            kind = ErpErrorKind.AMBIGUOUS
        elif resp.status_code >= 500 or resp.status_code == 429 or code in self._retryable_codes:
            kind = ErpErrorKind.RETRYABLE
        else:
            kind = ErpErrorKind.PERMANENT
        raise ErpError(kind, text, code=code, http_status=resp.status_code)


def _parse_error(resp: httpx.Response) -> tuple[str | None, str]:
    try:
        err = resp.json()["error"]
        return err.get("code"), err.get("message", {}).get("value", "")
    except Exception:
        return None, resp.text[:300]
