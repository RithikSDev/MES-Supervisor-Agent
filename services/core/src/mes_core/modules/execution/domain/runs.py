"""Operation run state machine and time accounting (pure).

A run is one operation being worked on one machine:

    (start) -> SETUP --start production--> RUNNING
    SETUP/RUNNING --pause(reason)--> PAUSED --resume--> back to SETUP/RUNNING
    any active --report--> same state (partial confirmation)
    any active --complete--> ENDED (final confirmation)   --stop--> ENDED (no final; operation stays open)

Time in each state accumulates when the state is left. Reports book the setup/run minutes accumulated
since the previous report, so SAP gets activity times split across partial confirmations.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from mes_core.shared.errors import InvalidTransition


class RunStatus(StrEnum):
    SETUP = "SETUP"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    ENDED = "ENDED"


ACTIVE = frozenset({RunStatus.SETUP, RunStatus.RUNNING, RunStatus.PAUSED})

PAUSE_REASONS: dict[str, str] = {
    "MATERIAL_SHORTAGE": "Material shortage",
    "MACHINE_ISSUE": "Machine issue",
    "QUALITY_CHECK": "Quality check",
    "WAITING_INSTRUCTIONS": "Waiting for instructions",
    "TOOL_CHANGE": "Tool change",
    "BREAK": "Break",
    "OTHER": "Other",
}

SCRAP_REASONS: dict[str, str] = {
    "DIMENSION": "Out of tolerance",
    "SURFACE": "Surface defect",
    "MATERIAL_DEFECT": "Material defect",
    "SETUP_SCRAP": "Setup / first-off scrap",
    "HANDLING": "Handling damage",
    "OTHER": "Other",
}


@dataclass(frozen=True)
class Clock:
    status: RunStatus
    phase_started_at: datetime
    paused_from: RunStatus | None = None
    setup_s: float = 0
    run_s: float = 0
    paused_s: float = 0
    setup_reported_s: float = 0
    run_reported_s: float = 0

    def _close_phase(self, at: datetime) -> Clock:
        dt = max((at - self.phase_started_at).total_seconds(), 0)
        if self.status == RunStatus.SETUP:
            return replace(self, setup_s=self.setup_s + dt, phase_started_at=at)
        if self.status == RunStatus.RUNNING:
            return replace(self, run_s=self.run_s + dt, phase_started_at=at)
        if self.status == RunStatus.PAUSED:
            return replace(self, paused_s=self.paused_s + dt, phase_started_at=at)
        return self

    def totals_at(self, at: datetime) -> Clock:
        """Accumulated times including the currently open phase (without changing state)."""
        return self._close_phase(at)


def _require(clock: Clock, allowed: set[RunStatus], action: str) -> None:
    if clock.status not in allowed:
        raise InvalidTransition(f"cannot {action}: run is {clock.status}", code="invalid_run_transition")


def start(phase: RunStatus, at: datetime) -> Clock:
    if phase not in (RunStatus.SETUP, RunStatus.RUNNING):
        raise InvalidTransition("a run starts in SETUP or RUNNING")
    return Clock(status=phase, phase_started_at=at)


def start_production(c: Clock, at: datetime) -> Clock:
    _require(c, {RunStatus.SETUP}, "start production")
    return replace(c._close_phase(at), status=RunStatus.RUNNING)


def pause(c: Clock, at: datetime) -> Clock:
    _require(c, {RunStatus.SETUP, RunStatus.RUNNING}, "pause")
    return replace(c._close_phase(at), status=RunStatus.PAUSED, paused_from=c.status)


def resume(c: Clock, at: datetime) -> Clock:
    _require(c, {RunStatus.PAUSED}, "resume")
    return replace(c._close_phase(at), status=c.paused_from or RunStatus.RUNNING, paused_from=None)


def report(c: Clock, at: datetime) -> tuple[Clock, float, float]:
    """Book activity time since the last report. Returns (clock, setup_minutes, run_minutes)."""
    _require(c, set(ACTIVE), "report quantities")
    now = c._close_phase(at)
    setup_min = (now.setup_s - now.setup_reported_s) / 60
    run_min = (now.run_s - now.run_reported_s) / 60
    return replace(now, setup_reported_s=now.setup_s, run_reported_s=now.run_s), setup_min, run_min


def end(c: Clock, at: datetime) -> Clock:
    _require(c, set(ACTIVE), "end the run")
    return replace(c._close_phase(at), status=RunStatus.ENDED, paused_from=None)
