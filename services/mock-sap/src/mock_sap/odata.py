"""Just enough OData V2 to look like SAP Gateway: JSON verbose format, /Date()/ literals, $filter/$orderby/paging."""

import re
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi.responses import JSONResponse


def sap_date(d: date | None) -> str | None:
    """Edm.DateTime as SAP emits it for date-only fields: midnight UTC in epoch millis."""
    if d is None:
        return None
    ms = int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)
    return f"/Date({ms})/"


def sap_datetimeoffset(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return f"/Date({int(dt.timestamp() * 1000)}+0000)/"


def dec(value: Decimal | float | int, places: int = 3) -> str:
    return f"{Decimal(value):.{places}f}"


def flag(b: bool) -> str:
    return "X" if b else ""


def odata_error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": {"lang": "en", "value": message},
                "innererror": {"application": {"component_id": "PP-SFC"}, **extra},
            }
        },
    )


# ---------------------------------------------------------------- $filter

_CLAUSE = re.compile(
    r"^\s*(?P<field>\w+)\s+(?P<op>eq|ne|gt|ge|lt|le)\s+(?P<value>.+?)\s*$", re.IGNORECASE
)
_LITERAL = re.compile(r"^(?P<kind>datetimeoffset|datetime)?'(?P<body>.*)'$", re.IGNORECASE | re.DOTALL)


class FilterError(ValueError):
    pass


def _parse_literal(raw: str) -> Any:
    m = _LITERAL.match(raw)
    if m:
        body = m.group("body").replace("''", "'")
        kind = (m.group("kind") or "").lower()
        if kind in ("datetimeoffset", "datetime"):
            dt = datetime.fromisoformat(body.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        return body
    if raw in ("true", "false"):
        return raw == "true"
    try:
        return Decimal(raw)
    except Exception as e:
        raise FilterError(f"unsupported literal {raw!r}") from e


def parse_filter(expr: str | None) -> list[tuple[str, str, Any]]:
    """Supports conjunctions of simple comparisons: `A eq 'x' and B ge datetimeoffset'...'`."""
    if not expr:
        return []
    clauses = re.split(r"\s+and\s+", expr.strip(), flags=re.IGNORECASE)
    out = []
    for c in clauses:
        c = c.strip()
        while c.startswith("(") and c.endswith(")"):
            c = c[1:-1].strip()
        m = _CLAUSE.match(c)
        if not m:
            raise FilterError(f"unsupported filter clause {c!r}")
        out.append((m.group("field"), m.group("op").lower(), _parse_literal(m.group("value"))))
    return out


_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: a is not None and a > b,
    "ge": lambda a, b: a is not None and a >= b,
    "lt": lambda a, b: a is not None and a < b,
    "le": lambda a, b: a is not None and a <= b,
}


def apply_query(
    rows: Iterable[Any],
    *,
    getter: Callable[[Any, str], Any],
    filter_expr: str | None,
    orderby: str | None,
    top: int | None,
    skip: int | None,
) -> tuple[list[Any], int]:
    """Returns (page, total_count_before_paging)."""
    clauses = parse_filter(filter_expr)
    result = []
    for r in rows:
        ok = True
        for field, op, value in clauses:
            actual = getter(r, field)
            if isinstance(value, Decimal) and actual is not None and not isinstance(actual, Decimal):
                actual = Decimal(str(actual))
            if not _OPS[op](actual, value):
                ok = False
                break
        if ok:
            result.append(r)
    if orderby:
        for part in reversed([p.strip() for p in orderby.split(",")]):
            name, _, direction = part.partition(" ")
            result.sort(key=lambda r, n=name: (getter(r, n) is None, getter(r, n)), reverse=direction == "desc")
    total = len(result)
    start = skip or 0
    end = start + top if top is not None else None
    return result[start:end], total


def collection(results: list[dict], count: int | None = None, next_link: str | None = None) -> dict:
    d: dict[str, Any] = {"results": results}
    if count is not None:
        d["__count"] = str(count)
    if next_link:
        d["__next"] = next_link
    return {"d": d}
