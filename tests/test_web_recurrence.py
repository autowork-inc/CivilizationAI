"""RRULE validation and expansion (pure): a broken rule must never reach the database, expansion keeps local wall time across DST."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ai_civ.web import constitution, recurrence
from ai_civ.web.recurrence import RuleError

CPH = ZoneInfo("Europe/Copenhagen")
MON_9 = datetime(2026, 3, 23, 9, 0, tzinfo=CPH)  # a Monday, before the DST change of 2026-03-29


def test_a_plain_rule_is_accepted_with_or_without_the_prefix_and_normalised():
    assert recurrence.validate("freq=weekly;byday=mo", MON_9, "Europe/Copenhagen") == "FREQ=WEEKLY;BYDAY=MO"
    assert recurrence.validate("RRULE:FREQ=DAILY;COUNT=3", MON_9, "Europe/Copenhagen") == "FREQ=DAILY;COUNT=3"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "nonsense",
        "FREQ=SECONDLY",
        "FREQ=MINUTELY;INTERVAL=1",
        "FREQ=HOURLY",
        "FREQ=WEEKLY;BYHOUR=9",
        "DTSTART=20260101T000000Z;FREQ=DAILY",
        "FREQ=DAILY;EXDATE=20260101",
        "FREQ=WEEKLY\nFREQ=DAILY",
        "FREQ=DAILY;" + "X" * 300,
        "FREQ=DAILY;COUNT=abc",
        "FREQ=WEEKLY;BYDAY=XX",
        "FREQ=DAILY;UNTIL=20260101T000000",  # not UTC: dateutil would raise at expansion time
        "FREQ=DAILY;UNTIL=20200101T000000Z",  # ends before it starts: yields nothing
    ],
)
def test_a_broken_or_dangerous_rule_is_refused_before_it_is_saved(raw):
    with pytest.raises(RuleError):
        recurrence.validate(raw, MON_9, "Europe/Copenhagen")


def test_expansion_keeps_the_local_wall_clock_across_the_dst_change():
    window = (datetime(2026, 3, 20, tzinfo=UTC), datetime(2026, 4, 10, tzinfo=UTC))
    found = recurrence.occurrences("FREQ=WEEKLY;BYDAY=MO", MON_9, "Europe/Copenhagen", timedelta(hours=1), *window)
    assert [(m.day, m.hour) for m in found] == [(23, 9), (30, 9), (6, 9)]  # 09:00 local every time
    offsets = [m.utcoffset() for m in found]
    assert offsets[0] == timedelta(hours=1) and offsets[1] == timedelta(hours=2)  # ...which is a different UTC instant
    assert found[0].astimezone(UTC).hour == 8 and found[1].astimezone(UTC).hour == 7


def test_a_window_returns_only_overlapping_occurrences_and_counts_are_respected():
    window = (datetime(2026, 3, 24, tzinfo=UTC), datetime(2026, 3, 26, tzinfo=UTC))
    assert recurrence.occurrences("FREQ=DAILY", MON_9, "Europe/Copenhagen", timedelta(hours=1), *window) != []
    assert len(recurrence.occurrences("FREQ=DAILY;COUNT=3", MON_9, "Europe/Copenhagen", timedelta(hours=1), datetime(2026, 1, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC))) == 3
    assert recurrence.occurrences("FREQ=DAILY;COUNT=3", MON_9, "Europe/Copenhagen", timedelta(hours=1), datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 7, 1, tzinfo=UTC)) == []


def test_an_occurrence_that_started_before_the_window_but_still_runs_is_included():
    started = datetime(2026, 3, 23, 22, 0, tzinfo=CPH)  # a 4-hour event, window opens at 00:00 CPH next day
    found = recurrence.occurrences("FREQ=DAILY", started, "Europe/Copenhagen", timedelta(hours=4), datetime(2026, 3, 23, 23, 0, tzinfo=UTC), datetime(2026, 3, 24, 6, 0, tzinfo=UTC))
    assert found[0].day == 23 and found[0].hour == 22


def test_the_expansion_is_capped():
    huge = recurrence.occurrences("FREQ=DAILY", MON_9, "Europe/Copenhagen", timedelta(hours=1), datetime(2026, 3, 23, tzinfo=UTC), datetime(2030, 1, 1, tzinfo=UTC))
    assert len(huge) == recurrence.MAX_OCCURRENCES_PER_SERIES


def test_is_occurrence_is_exact():
    rule = "FREQ=WEEKLY;BYDAY=MO"
    assert recurrence.is_occurrence(rule, MON_9, "Europe/Copenhagen", datetime(2026, 3, 30, 7, 0, tzinfo=UTC))  # DST shifted: 09:00 CEST
    assert not recurrence.is_occurrence(rule, MON_9, "Europe/Copenhagen", datetime(2026, 3, 30, 8, 0, tzinfo=UTC))  # the old UTC hour
    assert not recurrence.is_occurrence(rule, MON_9, "Europe/Copenhagen", datetime(2026, 3, 31, 7, 0, tzinfo=UTC))


# ---------------------------------------------------------------- principle slugs (they become part of a database key)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Не есть сладкое после шести", "ne_est_sladkoe_posle_shesti"),
        ("Sleep 8 hours!", "sleep_8_hours"),
        ("   ", "rule"),
        ("!!!", "rule"),
        ("8 часов сна", "p_8_chasov_sna"),
    ],
)
def test_slugs_are_database_safe(text, expected):
    assert constitution.slugify(text) == expected


def test_a_long_text_gives_a_bounded_slug_that_still_matches_the_key_check():
    import re

    slug = constitution.slugify("очень длинный принцип " * 20)
    assert len(slug) <= constitution.MAX_SLUG and re.fullmatch(r"[a-z][a-z0-9_]*", slug)
