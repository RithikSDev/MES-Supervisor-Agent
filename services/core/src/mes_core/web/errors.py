import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from mes_core.shared.errors import DomainError

log = logging.getLogger(__name__)


def _sqlstate(e: DBAPIError) -> str | None:
    orig = e.orig
    for candidate in (orig, getattr(orig, "__cause__", None)):
        if candidate is None:
            continue
        code = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if code:
            return str(code)
    return None


def _error(status: int, code: str, message: str, details: object = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": message, "details": details})


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_error(_: Request, e: DomainError) -> JSONResponse:
        return _error(e.status_code, e.code, e.message, e.details)

    @app.exception_handler(StaleDataError)
    async def stale(_: Request, e: StaleDataError) -> JSONResponse:
        return _error(409, "stale_version", "the record was changed concurrently; reload and try again")

    @app.exception_handler(IntegrityError)
    async def integrity(_: Request, e: IntegrityError) -> JSONResponse:
        state = _sqlstate(e)
        if state == "23P01":
            return _error(409, "schedule_overlap", "that slot overlaps another operation on the same machine")
        if state == "23505":
            return _error(409, "duplicate", "a record with the same key already exists")
        if state == "23503":
            return _error(409, "reference_violation", "a referenced record does not exist or is still in use")
        if state == "23514":
            return _error(422, "check_violation", "a value violates a data rule")
        log.exception("unexpected integrity error", exc_info=e)
        return _error(409, "integrity_error", "the change violates a data rule")

    @app.exception_handler(DBAPIError)
    async def dbapi(_: Request, e: DBAPIError) -> JSONResponse:
        if _sqlstate(e) == "42501":
            log.error("write rejected by audit/permission guard: %s", e.orig)
            return _error(500, "audit_guard", "write rejected by the audit guard")
        log.exception("database error", exc_info=e)
        return _error(500, "database_error", "database error")
