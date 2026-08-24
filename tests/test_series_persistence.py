"""The series survives a restart, and a cancellation has a rule to attach to.

THE GAP THIS CLOSES
-------------------
`book_series` wrote appointments and kept the `Series` in process memory. The
appointments survive a restart; the rule that explains them did not. A
cancellation then had nothing to attach an EXDATE to, and the only remaining
move was to DELETE an appointment -- which loses the fact that the series ever
included that date, and leaves the remaining rows looking like a series that
always skipped it.

Every test here works only through STORAGE after booking. Nothing holds the
original `Series` object, because holding it would test the thing that already
worked.
"""

import os
import sys
from datetime import date, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

import recurrence as RC
from scheduling import Scheduler


def _sched():
    s = Scheduler()
    s.add_provider("pr-1", "Dr A", "America/New_York")
    s.add_licence("pr-1", "NY", "2020-01-01", "2030-01-01")
    s.add_patient("pt-1", "A", "NY", "America/New_York")
    s.add_patient("pt-2", "B", "NY", "America/New_York")
    s.add_visit_type("vt-1", 30)
    for wd in range(7):
        s.add_working_hours("pr-1", wd, "00:00", "23:59")
    return s


def _series(series_id="ser-1", weeks=4):
    start = date.today() + timedelta(days=14)
    while start.weekday() != 1:
        start += timedelta(days=1)
    return RC.Series(series_id, "pr-1", "pt-1", "vt-1", start.isoformat(),
                     9, 0, "America/New_York",
                     "FREQ=WEEKLY;BYDAY=TU;COUNT=%d" % weeks)


def test_booking_a_series_persists_the_rule():
    s = _sched()
    RC.book_series(_series(), s, Scheduler)
    assert RC.list_series(s) == ["ser-1"]


def test_the_rule_round_trips_through_storage():
    """Stored as RRULE TEXT, not as expanded occurrences -- an expansion
    cannot survive a DST change and cannot be edited without rewriting every
    row."""
    s = _sched()
    original = _series()
    RC.book_series(original, s, Scheduler)

    back = RC.load_series(s, "ser-1")
    assert back is not None
    assert RC.format_rrule(back.rule) == RC.format_rrule(original.rule)
    assert back.anchor_date == original.anchor_date
    assert back.hour == original.hour and back.minute == original.minute
    assert str(back.tz) == str(original.tz)


def test_appointments_are_linked_back_to_their_series():
    """Without the link there is no way to find the appointment that a given
    occurrence produced, so a cancellation cannot free the slot."""
    s = _sched()
    RC.book_series(_series(), s, Scheduler)
    n = s.con.execute(
        "SELECT COUNT(*) FROM appointment WHERE series_id = 'ser-1'"
    ).fetchone()[0]
    assert n == 4


def test_a_one_off_appointment_has_no_series_id():
    """Most appointments are not part of a series and should not pretend."""
    s = _sched()
    series = _series()
    when = series.occurrences(Scheduler)[0]["utc"]
    s.book("pr-1", "pt-2", "vt-1", when)
    row = s.con.execute(
        "SELECT series_id FROM appointment WHERE patient_id = 'pt-2'"
    ).fetchone()
    assert row[0] is None


# ------------------------------------------------------ the actual scenario
def test_cancelling_after_a_restart_updates_the_stored_rule():
    """THE GAP, EXERCISED.

    Everything after `book_series` goes through storage only -- the original
    Series object is deliberately discarded, which is what a restart does.
    """
    s = _sched()
    RC.book_series(_series(), s, Scheduler)

    reloaded = RC.load_series(s, "ser-1")           # all a restart would have
    dates = [o["local_date"] for o in reloaded.occurrences(Scheduler)]

    result = RC.cancel_occurrence(s, "ser-1", dates[1])
    assert result["appointments_cancelled"], "the slot was never freed"

    after = [o["local_date"]
             for o in RC.load_series(s, "ser-1").occurrences(Scheduler)]
    assert dates[1] not in after
    assert len(after) == len(dates) - 1


def test_the_cancellation_frees_the_actual_slot():
    """Updating the rule alone would leave a booked appointment for a date the
    rule now excludes -- the slot stays occupied and the patient still gets a
    reminder."""
    s = _sched()
    RC.book_series(_series(), s, Scheduler)
    dates = [o["local_date"]
             for o in RC.load_series(s, "ser-1").occurrences(Scheduler)]

    RC.cancel_occurrence(s, "ser-1", dates[1])
    cancelled = s.con.execute(
        "SELECT COUNT(*) FROM appointment WHERE series_id = 'ser-1' "
        "AND status = 'cancelled'").fetchone()[0]
    assert cancelled == 1


def test_the_cancellation_is_audited():
    """A cancellation nobody can trace is indistinguishable from a booking that
    never happened."""
    s = _sched()
    RC.book_series(_series(), s, Scheduler)
    dates = [o["local_date"]
             for o in RC.load_series(s, "ser-1").occurrences(Scheduler)]
    RC.cancel_occurrence(s, "ser-1", dates[1])

    notes = [r[0] for r in s.con.execute(
        "SELECT note FROM appointment_audit WHERE to_status = 'cancelled'"
    ).fetchall()]
    assert any("ser-1" in (n or "") for n in notes)


def test_count_is_still_consumed_before_exdate_after_a_restart():
    """The RFC 5545 semantics must survive the round trip. If they did not, a
    patient who cancelled one week would be booked an extra one -- and the bug
    would only appear after a restart, which is the worst place for it."""
    s = _sched()
    RC.book_series(_series(), s, Scheduler)
    before = [o["local_date"]
              for o in RC.load_series(s, "ser-1").occurrences(Scheduler)]
    RC.cancel_occurrence(s, "ser-1", before[1])
    after = [o["local_date"]
             for o in RC.load_series(s, "ser-1").occurrences(Scheduler)]
    assert after[-1] == before[-1]


def test_cancelling_an_unknown_series_is_refused():
    s = _sched()
    with pytest.raises(RC.RecurrenceError):
        RC.cancel_occurrence(s, "no-such-series", date.today())


def test_an_all_or_nothing_rollback_leaves_no_series_behind():
    """A stored rule describing appointments that were rolled back is a claim
    the data cannot support."""
    s = _sched()
    series = _series()
    occ = series.occurrences(Scheduler)
    s.book("pr-1", "pt-2", "vt-1", occ[2]["utc"])          # block the third
    out = RC.book_series(series, s, Scheduler, policy="all-or-nothing")
    assert out["n_booked"] == 0
    assert RC.list_series(s) == []


def test_persistence_can_be_switched_off():
    s = _sched()
    RC.book_series(_series(), s, Scheduler, persist=False)
    assert RC.list_series(s) == []
