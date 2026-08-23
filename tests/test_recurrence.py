"""Tests for the recurring-series model and the DST bugs only series have.

The storage tests are the point. A weekly series stored as a UTC instant plus a
7-day interval drifts by an hour at every DST transition, silently, and the
patient told "Tuesdays at 9" misses a session. Nothing errors, so only a test
that constructs the transition will find it.
"""

import os
import sys
from datetime import date

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

import recurrence as RC
from scheduling import Scheduler

NY = "America/New_York"


def _weekly(hour=9, minute=0, anchor="2024-10-01", byday="TU", count=8,
            tz=NY):
    return RC.Series("s1", "pr-1", "pt-1", "vt-1", anchor, hour, minute, tz,
                     f"FREQ=WEEKLY;BYDAY={byday};COUNT={count}")


# --------------------------------------------------------------------------
# RRULE parsing
# --------------------------------------------------------------------------

def test_a_weekly_rule_parses():
    r = RC.parse_rrule("FREQ=WEEKLY;BYDAY=TU,TH;COUNT=6")
    assert r["freq"] == "WEEKLY" and r["count"] == 6
    assert set(r["byday"]) == {1, 3}


def test_an_unsupported_rrule_part_is_refused_not_ignored():
    """Ignoring an unrecognised part is how a series silently recurs on the
    wrong days: the rule still runs, meaning something else."""
    with pytest.raises(RC.RecurrenceError) as e:
        RC.parse_rrule("FREQ=MONTHLY;BYMONTHDAY=15")
    assert "FREQ" in str(e.value) or "BYMONTHDAY" in str(e.value)


def test_count_and_until_together_are_refused():
    with pytest.raises(RC.RecurrenceError) as e:
        RC.parse_rrule("FREQ=WEEKLY;COUNT=3;UNTIL=20241231")
    assert "mutually exclusive" in str(e.value)


def test_an_unknown_weekday_is_refused():
    with pytest.raises(RC.RecurrenceError):
        RC.parse_rrule("FREQ=WEEKLY;BYDAY=XX")


def test_a_rule_round_trips_through_format():
    text = "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;COUNT=4"
    assert RC.parse_rrule(RC.format_rrule(RC.parse_rrule(text))) \
        == RC.parse_rrule(text)


# --------------------------------------------------------------------------
# expansion
# --------------------------------------------------------------------------

def test_a_weekly_series_lands_on_the_right_weekday():
    dates = _weekly().local_dates()
    assert len(dates) == 8
    assert all(d.weekday() == 1 for d in dates)


def test_interval_2_skips_a_week():
    s = RC.Series("s", "p", "q", "v", "2024-10-01", 9, 0, NY,
                  "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU;COUNT=4")
    dates = s.local_dates()
    assert (dates[1] - dates[0]).days == 14


def test_until_bounds_the_series():
    s = RC.Series("s", "p", "q", "v", "2024-10-01", 9, 0, NY,
                  "FREQ=WEEKLY;BYDAY=TU;UNTIL=20241022")
    assert max(s.local_dates()) <= date(2024, 10, 22)


# --------------------------------------------------------------------------
# the DST behaviour that only series have
# --------------------------------------------------------------------------

def test_the_wall_clock_is_held_across_a_transition():
    """09:00 local before AND after. The UTC instants are deliberately not
    evenly spaced -- a series whose UTC instants are evenly spaced across a
    transition is a series whose local times are wrong."""
    occ = _weekly().occurrences(Scheduler)
    before = [o for o in occ if o["local_date"] < "2024-11-03"]
    after = [o for o in occ if o["local_date"] > "2024-11-03"]
    assert before and after
    assert all(o["tzname"] == "EDT" and o["utc_offset_hours"] == -4.0
               for o in before)
    assert all(o["tzname"] == "EST" and o["utc_offset_hours"] == -5.0
               for o in after)
    assert before[-1]["utc"].hour == 13
    assert after[0]["utc"].hour == 14


def test_the_naive_utc_representation_drifts_by_an_hour():
    """THE BUG THIS FILE EXISTS FOR, measured. Nothing errors; the local time
    just moves, and the patient misses a session."""
    s = _weekly()
    drift = RC.storage_drift(s, Scheduler)
    assert drift, "no transition inside the range; the test proves nothing"
    assert all(r["drift_minutes"] == -60 for r in drift)
    assert all(r["naive_local"].startswith("08:00") for r in drift)


def test_no_drift_when_no_transition_falls_inside_the_series():
    """The control. Without it, `storage_drift` returning rows would not be
    evidence that it detects transitions rather than always complaining."""
    s = _weekly(anchor="2024-10-01", count=3)      # all before 3 Nov
    assert RC.storage_drift(s, Scheduler) == []


def test_a_series_in_a_zone_without_dst_never_drifts():
    s = _weekly(tz="UTC")
    assert RC.storage_drift(s, Scheduler) == []


def test_a_nonexistent_occurrence_is_reported_not_silently_shifted():
    """Spring forward: 02:30 on the transition day never happens. A scheduler
    that silently books 03:30 has moved an appointment without telling anyone."""
    s = RC.Series("s", "p", "q", "v", "2024-03-03", 2, 30, NY,
                  "FREQ=WEEKLY;BYDAY=SU;COUNT=3")
    occ = s.occurrences(Scheduler)
    kinds = {o["local_date"]: o["kind"] for o in occ}
    assert kinds["2024-03-10"] == "nonexistent"
    assert next(o for o in occ if o["local_date"] == "2024-03-10")["utc"] is None


def test_an_ambiguous_occurrence_reports_both_instants():
    """Fall back: 01:30 happens twice, an hour apart, and they are genuinely
    different instants."""
    s = RC.Series("s", "p", "q", "v", "2024-10-27", 1, 30, NY,
                  "FREQ=WEEKLY;BYDAY=SU;COUNT=3")
    occ = {o["local_date"]: o for o in s.occurrences(Scheduler)}
    row = occ["2024-11-03"]
    assert row["kind"] == "ambiguous"
    assert len(row["instants"]) == 2
    assert (row["instants"][1] - row["instants"][0]).total_seconds() == 3600


# --------------------------------------------------------------------------
# moving a series
# --------------------------------------------------------------------------

def test_a_move_without_a_mode_is_refused():
    """The two readings differ on exactly the occurrences hardest to notice, so
    guessing is how half a series ends up an hour out."""
    with pytest.raises(RC.AmbiguousMove):
        _weekly().move(30, mode="", scheduler_cls=Scheduler)


def test_a_wall_clock_move_changes_the_rule_for_every_occurrence():
    moved, _utc = _weekly().move(30, mode="wall_clock", scheduler_cls=Scheduler)
    assert (moved.hour, moved.minute) == (9, 30)
    assert all(o["local_time"] == "09:30"
               for o in moved.occurrences(Scheduler))


def test_the_two_modes_agree_for_an_ordinary_shift():
    """MEASURED, AND IT CORRECTED ME. I first wrote that the modes diverge
    'once a transition is inside the range'. They do not: adding a constant to
    a correctly-resolved instant preserves its local time, so an 8-week series
    spanning a transition sees no disagreement at all."""
    assert RC.compare_moves(_weekly(), 30, Scheduler) == []


def test_the_two_modes_disagree_on_an_ambiguous_occurrence():
    """The narrower, harder case. A series at 01:30 on fall-back Sunday moved
    60 minutes: wall clock gives 02:30 EST, absolute gives the SECOND 01:30 --
    an hour of elapsed time later, same wall clock."""
    s = RC.Series("s", "p", "q", "v", "2024-10-27", 1, 30, NY,
                  "FREQ=WEEKLY;BYDAY=SU;COUNT=3")
    rows = RC.compare_moves(s, 60, Scheduler)
    assert len(rows) == 1
    r = rows[0]
    assert r["date"] == "2024-11-03"
    assert r["wall_clock_local"].startswith("02:30")
    assert r["absolute_local"].startswith("01:30")
    assert r["differ_by_minutes"] == -60


def test_a_wall_clock_move_across_midnight_is_refused():
    """It would change which DAY each occurrence falls on, and therefore which
    BYDAY the rule means."""
    with pytest.raises(RC.RecurrenceError) as e:
        _weekly(hour=23, minute=30).move(
            60, mode="wall_clock", scheduler_cls=Scheduler)
    assert "midnight" in str(e.value)


def test_an_absolute_move_leaves_the_rule_alone():
    s = _weekly()
    same, instants = s.move(30, mode="absolute", scheduler_cls=Scheduler)
    assert (same.hour, same.minute) == (9, 0)
    original = [o["utc"] for o in s.occurrences(Scheduler)]
    assert all((b - a).total_seconds() == 1800
               for a, b in zip(original, instants))
