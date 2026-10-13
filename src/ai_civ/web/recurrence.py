"""RRULE handling for the schedule: validation (a broken rule must never reach the database, where it would blow up the
Orchestrator later) and expansion into concrete occurrences. Pure module: no I/O.

Rules are expanded in the event's OWN time zone with a timezone-aware ``dtstart``, so "every Monday 09:00" stays 09:00 local
across daylight-saving changes (the stored instants shift, the wall clock does not).

Accepted: a plain RRULE value (``FREQ=WEEKLY;BYDAY=MO,WE``), with or without the ``RRULE:`` prefix, FREQ in DAILY..YEARLY (a
SECONDLY/MINUTELY/HOURLY rule is a denial of service, not a schedule). Refused: DTSTART / EXDATE / RDATE lines (the start is the
event's own ``starts_at``; exceptions are rows with ``parent_event_id``), a rule that yields nothing, and a UNTIL that is not UTC
(``...Z``): dateutil raises on those at expansion time, long after the row was saved.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dateutil.rrule import rrule, rrulestr

ALLOWED_FREQ = frozenset({"DAILY", "WEEKLY", "MONTHLY", "YEARLY"})
MAX_RULE_CHARS = 200
MAX_OCCURRENCES_PER_SERIES = 500  # in one window
RULE_RE = re.compile(r"^[A-Z0-9=;,+\-:]+$")


class RuleError(ValueError):
    """The rule cannot be saved. The message is Russian and shown to the user as-is."""


def normalize(raw: str) -> str:
    rule = raw.strip().upper().removeprefix("RRULE:")
    if not rule or len(rule) > MAX_RULE_CHARS or not RULE_RE.match(rule):
        raise RuleError("Не понял правило повторения.")
    parts = dict(part.split("=", 1) for part in rule.split(";") if "=" in part)
    if len(parts) != rule.count(";") + 1:
        raise RuleError("Не понял правило повторения.")
    if parts.get("FREQ") not in ALLOWED_FREQ:
        raise RuleError("Повторять можно раз в день, неделю, месяц или год.")
    for forbidden in ("DTSTART", "EXDATE", "RDATE", "EXRULE", "BYSECOND", "BYMINUTE", "BYHOUR"):
        if forbidden in parts:
            raise RuleError("Время начала берётся из самого события, исключения задаются отдельно.")
    until = parts.get("UNTIL")
    if until is not None and not until.endswith("Z"):
        raise RuleError("Дата окончания повторов должна быть в UTC (с буквой Z на конце).")
    return rule


def build(rule: str, starts_at: datetime, timezone: str) -> rrule:
    """``starts_at`` (any aware datetime) becomes the local first occurrence in ``timezone``."""
    local_start = starts_at.astimezone(ZoneInfo(timezone))
    try:
        built = rrulestr(rule, dtstart=local_start)
    except (ValueError, TypeError, KeyError) as exc:
        raise RuleError(f"Не понял правило повторения: {exc}") from None
    if not isinstance(built, rrule):
        raise RuleError("Не понял правило повторения.")
    return built


def validate(raw: str, starts_at: datetime, timezone: str) -> str:
    """The normalized rule, or ``RuleError``. Proves the rule really generates dates by taking the first two."""
    rule = normalize(raw)
    built = build(rule, starts_at, timezone)
    try:
        first = list(_take(built, 2))
    except (ValueError, TypeError, OverflowError) as exc:
        raise RuleError(f"Не понял правило повторения: {exc}") from None
    if not first:
        raise RuleError("По этому правилу не получается ни одного события.")
    return rule


def _take(built: rrule, n: int):
    for index, moment in enumerate(built):
        if index >= n:
            return
        yield moment


def occurrences(
    rule: str, starts_at: datetime, timezone: str, duration: timedelta, window_start: datetime, window_end: datetime
) -> list[datetime]:
    """Starts of the occurrences that OVERLAP [window_start, window_end), as aware datetimes in the event's zone.
    At most MAX_OCCURRENCES_PER_SERIES (a window is at most about a year; the cap is a seat belt)."""
    built = build(rule, starts_at, timezone)
    found: list[datetime] = []
    for moment in built.xafter(window_start - duration, inc=True):
        if moment >= window_end:
            break
        found.append(moment)
        if len(found) >= MAX_OCCURRENCES_PER_SERIES:
            break
    return found


def is_occurrence(rule: str, starts_at: datetime, timezone: str, candidate: datetime) -> bool:
    """Is ``candidate`` (an instant) exactly one of the series' starts? Used before an occurrence is cancelled."""
    built = build(rule, starts_at, timezone)
    local = candidate.astimezone(ZoneInfo(timezone))
    return any(moment == local for moment in built.between(local - timedelta(seconds=1), local + timedelta(seconds=1), inc=True))
