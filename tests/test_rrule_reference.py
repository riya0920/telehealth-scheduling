"""Differences the hand-written RRULE expansion against `dateutil.rrule`.

THESE TESTS SKIP when `python-dateutil` is absent, and that is the point:
`src/` has no third-party recurrence dependency. The reference is used to AUDIT
the expansion, never to provide it.

The README used to excuse the small supported subset by saying `dateutil.rrule`
"does this properly and is not installed". That was wrong -- it is installed,
and arrives with pandas on most machines. The subset is still small, but that
is a scoping decision now, not a missing dependency.
"""

import os
import sys
from datetime import date, datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

import recurrence as RC
from scheduling import Scheduler

pytest.importorskip("dateutil", reason="reference audit only")

from dateutil.rrule import DAILY, WEEKLY, rrule, rruleset       # noqa: E402

import validate_rrule as V                                      # noqa: E402


def _mine(text, start, tz="America/New_York"):
    s = RC.Series("s", "pr-1", "pt-1", "vt-1", start, 9, 0, tz, text)
    return [o["local_date"] for o in s.occurrences(Scheduler)]


def test_count_is_consumed_before_exdate_agrees_with_dateutil():
    """THE CLAIM THIS AUDIT EXISTS FOR.

    `exclude()` asserts a reading of RFC 5545 3.8.5.1: COUNT is consumed BEFORE
    EXDATE, so excluding one occurrence SHORTENS the series rather than sliding
    a replacement onto the end.

    A unit test cannot settle that on its own -- a test written by whoever read
    the spec encodes the same reading of it. `dateutil` is an independent
    reading. The alternative is not academic: topping the series back up to
    COUNT silently books a patient who cancelled one week an extra one.
    """
    start = datetime(2024, 10, 1, 9, 0)
    rs = rruleset()
    rs.rrule(rrule(WEEKLY, byweekday=1, count=6, dtstart=start))
    rs.exdate(datetime(2024, 10, 15, 9, 0))
    ref = [d.date().isoformat() for d in rs]

    got = _mine("FREQ=WEEKLY;BYDAY=TU;COUNT=6;EXDATE=20241015", "2024-10-01")
    base = _mine("FREQ=WEEKLY;BYDAY=TU;COUNT=6", "2024-10-01")

    assert got == ref
    assert len(got) == 5                     # the series SHORTENS
    assert got[-1] == base[-1]               # and ends on the same date


@pytest.mark.parametrize("text,start", [
    ("FREQ=DAILY;COUNT=10", "2024-03-05"),
    ("FREQ=DAILY;INTERVAL=3;COUNT=7", "2024-06-01"),
    ("FREQ=WEEKLY;BYDAY=MO,WE,FR;COUNT=9", "2024-01-02"),
    ("FREQ=WEEKLY;INTERVAL=2;BYDAY=TU;COUNT=5", "2024-11-05"),
    ("FREQ=WEEKLY;BYDAY=SA,SU;UNTIL=20240401", "2024-03-01"),
    ("FREQ=DAILY;UNTIL=20240315", "2024-03-01"),
])
def test_supported_grammar_matches_dateutil(text, start):
    rule = RC.parse_rrule(text)
    assert _mine(text, start) == V._reference(rule, start)


def test_a_randomised_sweep_agrees_everywhere():
    """400 generated rules across the whole supported grammar.

    Hand-picked cases test what the author thought of. The sweep tests
    combinations nobody chose, which is where an INTERVAL/BYDAY interaction
    would hide.
    """
    n, n_ex, n_occ, bad = V.compare(trials=400)
    assert n > 300, "the generator produced too few valid rules to mean much"
    assert n_ex > 100, "EXDATE was barely exercised"
    assert n_occ > 3000
    assert bad == []


def test_the_sweep_can_actually_fail():
    """A comparison reporting zero mismatches is worthless until you know it
    can report a non-zero one.

    This plants the plausible WRONG reading of COUNT/EXDATE and requires the
    sweep to catch it. Without this, `test_a_randomised_sweep_agrees_everywhere`
    would pass just as happily against a generator that emitted nothing.
    """
    _n, _n_ex, _n_occ, bad = V.compare(trials=400, sabotage=True)
    assert len(bad) > 50, (
        "the sabotaged reading was not caught, so the clean sweep proves "
        "nothing about the implementation")


def test_unsupported_parts_are_refused_not_ignored():
    """dateutil SUPPORTS BYMONTHDAY; this deliberately does not, and refuses.

    That asymmetry is the right one. Silently dropping a part a caller wrote
    makes the rule mean something else that still runs -- a series that recurs
    on the wrong days is worse than one that fails loudly at booking time.
    """
    with pytest.raises(RC.RecurrenceError) as exc:
        RC.parse_rrule("FREQ=MONTHLY;BYMONTHDAY=15")
    assert "FREQ" in str(exc.value)

    with pytest.raises(RC.RecurrenceError) as exc:
        RC.parse_rrule("FREQ=WEEKLY;BYDAY=TU;BYSETPOS=1")
    assert "BYSETPOS" in str(exc.value)
    assert "ignored" in str(exc.value)
