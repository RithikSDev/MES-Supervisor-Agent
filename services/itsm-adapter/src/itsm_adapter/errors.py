from enum import StrEnum

import httpx


class ItsmErrorKind(StrEnum):
    RETRYABLE = "RETRYABLE"  # definitely not applied; try again later
    AMBIGUOUS = "AMBIGUOUS"  # may have been applied (timeout); the next delivery looks it up first anyway
    PERMANENT = "PERMANENT"  # rejected (bad data, missing group, wrong credentials): needs a human


class ItsmError(Exception):
    def __init__(self, kind: ItsmErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message

    def __str__(self) -> str:
        return f"{self.kind}: {self.message}"


def from_response(system: str, what: str, resp: httpx.Response, detail: str) -> ItsmError:
    """HTTP error -> kind. 429/5xx: retry; 401/403: credentials or roles; other 4xx: rejected."""
    code = resp.status_code
    if code in (401, 403):
        return ItsmError(ItsmErrorKind.PERMANENT, f"{system} {what}: HTTP {code} (check the integration user and its "
                                                  f"roles): {detail}")
    if code == 429 or code >= 500:
        return ItsmError(ItsmErrorKind.RETRYABLE, f"{system} {what}: HTTP {code}: {detail}")
    return ItsmError(ItsmErrorKind.PERMANENT, f"{system} {what}: HTTP {code}: {detail}")


def from_transport(system: str, what: str, e: Exception, *, write: bool) -> ItsmError:
    if isinstance(e, httpx.ConnectError | httpx.ConnectTimeout) or not write:
        return ItsmError(ItsmErrorKind.RETRYABLE, f"{system} {what}: {e!r}")
    return ItsmError(ItsmErrorKind.AMBIGUOUS, f"{system} {what}: no response: {e!r}")
