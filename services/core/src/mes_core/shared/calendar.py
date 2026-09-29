"""Working-time arithmetic (pure). Shift patterns -> merged working intervals -> add N working minutes."""

import bisect
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True, order=True)
class Interval:
    start: datetime
    end: datetime

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60

    def overlaps(self, other: Interval) -> bool:
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True)
class ShiftDef:
    start_time: time
    end_time: time  # <= start_time means the shift ends the next day
    days_of_week: tuple[int, ...]  # ISO weekday of the shift *start*: 1=Mon .. 7=Sun


def merge(intervals: list[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for iv in sorted(intervals):
        if out and iv.start <= out[-1].end:
            if iv.end > out[-1].end:
                out[-1] = Interval(out[-1].start, iv.end)
        else:
            out.append(iv)
    return out


def complement(intervals: list[Interval], start: datetime, end: datetime) -> list[Interval]:
    gaps, cursor = [], start
    for iv in merge(intervals):
        if iv.end <= start or iv.start >= end:
            continue
        if iv.start > cursor:
            gaps.append(Interval(cursor, iv.start))
        cursor = max(cursor, iv.end)
    if cursor < end:
        gaps.append(Interval(cursor, end))
    return gaps


def expand_shifts(shifts: list[ShiftDef], tz: ZoneInfo, start: datetime, end: datetime) -> list[Interval]:
    """Concrete working intervals within [start, end), in UTC-aware datetimes, merged."""
    day: date = start.astimezone(tz).date() - timedelta(days=1)
    last: date = end.astimezone(tz).date()
    out = []
    while day <= last:
        for sh in shifts:
            if day.isoweekday() not in sh.days_of_week:
                continue
            s = datetime.combine(day, sh.start_time, tz)
            e_day = day if sh.end_time > sh.start_time else day + timedelta(days=1)
            e = datetime.combine(e_day, sh.end_time, tz)
            s, e = max(s, start), min(e, end)
            if s < e:
                out.append(Interval(s, e))
        day += timedelta(days=1)
    return merge(out)


class WorkingCalendar:
    def __init__(self, intervals: list[Interval]) -> None:
        self.intervals = merge(intervals)
        self._ends = [iv.end for iv in self.intervals]

    def next_working(self, t: datetime) -> datetime | None:
        """The first instant >= t that is inside working time."""
        i = bisect.bisect_right(self._ends, t)
        if i >= len(self.intervals):
            return None
        return max(t, self.intervals[i].start)

    def add_working(self, t: datetime, minutes: float) -> datetime | None:
        """End time after consuming `minutes` of working time from t. None if the calendar runs out."""
        remaining = timedelta(minutes=minutes)
        i = bisect.bisect_right(self._ends, t)
        cursor = t
        while i < len(self.intervals):
            iv = self.intervals[i]
            cursor = max(cursor, iv.start)
            available = iv.end - cursor
            if remaining <= available:
                return cursor + remaining
            remaining -= available
            i += 1
        return None

    def is_working(self, t: datetime) -> bool:
        i = bisect.bisect_right(self._ends, t)
        return i < len(self.intervals) and self.intervals[i].start <= t


def subtract(intervals: list[Interval], blocked: list[Interval]) -> list[Interval]:
    """Working intervals minus blocked time (e.g. planned maintenance, a machine that is down)."""
    out: list[Interval] = []
    blocks = merge(blocked)
    for iv in merge(intervals):
        pieces = [iv]
        for b in blocks:
            nxt = []
            for p in pieces:
                if not p.overlaps(b):
                    nxt.append(p)
                    continue
                if p.start < b.start:
                    nxt.append(Interval(p.start, b.start))
                if b.end < p.end:
                    nxt.append(Interval(b.end, p.end))
            pieces = nxt
        out.extend(pieces)
    return out
