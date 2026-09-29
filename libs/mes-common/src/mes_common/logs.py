"""Logging for every service: one JSON object per line on stdout, so the log store (Loki) can filter by service,
level and request id, and people and the AI L2 agent can follow one request across services.

    {"ts": "...", "level": "ERROR", "service": "mes-core-api", "logger": "...", "msg": "...", "request_id": "...",
     "exc": "Traceback ..."}

`request_id_var` carries the current request id (set by the API middleware from X-Request-ID); it is added to every
line logged while handling that request. LOG_FORMAT=text switches back to plain lines for local debugging.
"""

import contextvars
import json
import logging
import os
from datetime import UTC, datetime

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = request_id_var.get()
        if rid:
            entry["request_id"] = rid
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


class _HealthCheckFilter(logging.Filter):
    """Docker health checks hit /health every few seconds: keep them out of the access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "/health/" not in record.getMessage()


def setup_logging(service: str) -> None:
    handler = logging.StreamHandler()
    if os.environ.get("LOG_FORMAT", "json") == "text":
        handler.setFormatter(logging.Formatter(f"%(asctime)s %(levelname)-7s {service} %(name)s: %(message)s"))
    else:
        handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    # uvicorn installs its own handlers: route them through ours (same format, one stream)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    logging.getLogger("uvicorn.access").addFilter(_HealthCheckFilter())
    # aiokafka is chatty at INFO about group rebalances
    logging.getLogger("aiokafka").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per SAP call drowns everything else


def install_request_id(app) -> None:  # type: ignore[no-untyped-def]
    """FastAPI middleware: take X-Request-ID (nginx, or the calling service) or make one; log lines of the request
    carry it; the response returns it. Handlers read it from `request.state.request_id`."""
    from uuid import uuid4

    @app.middleware("http")
    async def _request_id(request, call_next):  # type: ignore[no-untyped-def]
        rid = request.headers.get("x-request-id") or uuid4().hex
        request.state.request_id = rid
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["x-request-id"] = rid
        return response
