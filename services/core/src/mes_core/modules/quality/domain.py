"""Quality rules (pure)."""

from dataclasses import dataclass
from decimal import Decimal

DEFECT_CODES: dict[str, str] = {
    "DIMENSION": "Dimension out of tolerance",
    "SURFACE": "Surface defect / scratch",
    "LEAK": "Leak / pressure test failure",
    "ASSEMBLY": "Wrong or incomplete assembly",
    "MATERIAL": "Material defect",
    "DOCUMENTATION": "Missing certificate / documentation",
    "FUNCTION": "Function test failure",
    "OTHER": "Other",
}
SEVERITIES = ("MINOR", "MAJOR", "CRITICAL")
DISPOSITIONS: dict[str, str] = {
    "USE_AS_IS": "Use as is (concession)",
    "REWORK": "Rework",
    "SCRAP": "Scrap",
    "RETURN_TO_SUPPLIER": "Return to supplier",
}


@dataclass(frozen=True)
class Characteristic:
    id: object
    kind: str  # QUANTITATIVE | QUALITATIVE
    lower_limit: Decimal | None
    upper_limit: Decimal | None


def conforming(c: Characteristic, value: Decimal | None, ok: bool | None) -> bool:
    if c.kind == "QUALITATIVE":
        if ok is None:
            raise ValueError("a qualitative characteristic needs OK / not OK")
        return ok
    if value is None:
        raise ValueError("a quantitative characteristic needs a measured value")
    if c.lower_limit is not None and value < c.lower_limit:
        return False
    return not (c.upper_limit is not None and value > c.upper_limit)


def evaluate(characteristic_ids: list[object], sample_size: int, results: dict[tuple[object, int], bool]) -> str | None:
    """PASSED / FAILED once every characteristic has a result for every sample, else None (still open).

    A single nonconforming result fails the lot immediately (no point measuring the rest)."""
    if any(not ok for ok in results.values()):
        return "FAILED"
    complete = all((cid, n) in results for cid in characteristic_ids for n in range(1, sample_size + 1))
    return "PASSED" if complete else None
