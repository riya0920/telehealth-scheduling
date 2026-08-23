"""Tests for licensure compacts, EXDATE exceptions, and series booking.

The compact tests are about a distinction a flat state list cannot express:
IMLC is an expedited pathway to OBTAIN a licence, PSYPACT grants an authority to
PRACTISE. Treating them the same puts a provider in front of a patient in a
state where they hold nothing.
"""

import os
import sys
from datetime import date, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

import compact as C
import recurrence as RC
from scheduling import Scheduler


# --------------------------------------------------------------------------
# compacts
# --------------------------------------------------------------------------

def test_imlc_never_authorises_practice():
    """THE DISTINCTION. The IMLC is an expedited pathway to OBTAIN a licence,
    not a licence. Treating membership as authorisation would put a provider in
    front of a patient in a state where they hold nothing."""
    ok, reason = C.authorises("IMLC", home_state="CO", patient_state="WA",
                              profession="physician", modality="video",
                              service_date="2024-06-01")
    assert ok is False
    assert "does not grant practice authority" in reason
    assert "EXPEDITED PATHWAY" in reason


def test_psypact_does_authorise_telepsychology():
    """Different in kind, not degree: an APIT genuinely permits practice into
    other member states."""
    ok, reason = C.authorises("PSYPACT", home_state="CO", patient_state="TX",
                              profession="psychologist", modality="video",
                              service_date="2024-06-01")
    assert ok is True
    assert "APIT" in reason


def test_a_compact_is_profession_specific():
    ok, reason = C.authorises("PSYPACT", home_state="CO", patient_state="TX",
                              profession="physician", modality="video",
                              service_date="2024-06-01")
    assert ok is False and "psychologist" in reason


def test_a_compact_is_modality_specific():
    """PSYPACT covers TELEpsychology. An in-person visit is not covered by an
    interjurisdictional telepractice authority."""
    ok, reason = C.authorises("PSYPACT", home_state="CO", patient_state="TX",
                              profession="psychologist", modality="in-person",
                              service_date="2024-06-01")
    assert ok is False and "covers" in reason


def test_membership_is_checked_on_the_date_of_service():
    """A state that joins next month does not authorise an appointment booked
    for next week -- the same discipline is_licensed uses for the licence."""
    early = C.authorises("PSYPACT", home_state="AZ", patient_state="VA",
                         profession="psychologist", modality="video",
                         service_date="2021-01-01")
    later = C.authorises("PSYPACT", home_state="AZ", patient_state="VA",
                         profession="psychologist", modality="video",
                         service_date="2024-01-01")
    assert early[0] is False and "was not a" in early[1]
    assert later[0] is True


def test_both_states_must_be_members():
    ok, reason = C.authorises("PSYPACT", home_state="CO", patient_state="NY",
                              profession="psychologist", modality="video",
                              service_date="2024-06-01")
    assert ok is False and "NY" in reason


def test_an_unknown_compact_is_refused():
    with pytest.raises(C.CompactError):
        C.authorises("MADE-UP", home_state="CO", patient_state="TX",
                     profession="psychologist", modality="video",
                     service_date="2024-06-01")


def test_why_not_explains_every_compact():
    """'Not licensed' is an answer a front desk cannot act on. 'PSYPACT would
    cover this but Ohio joined after the date of service' is one they can."""
    rows = C.why_not("CO", "NY", "psychologist", "video", "2024-06-01")
    assert {r["compact"] for r in rows} == set(C.COMPACTS)
    assert all(r["reason"] for r in rows)


def test_a_held_licence_is_checked_before_any_compact():
    """A held licence is the strongest and simplest answer, so the common case
    never touches compact logic at all."""
    s = Scheduler()
    s.add_provider("pr-1", "Dr A", "America/Denver")
    s.add_licence("pr-1", "WA", "2020-01-01", "2030-01-01")
    out = C.check(s, "pr-1", home_state="CO", patient_state="WA",
                  profession="physician", modality="video",
                  service_date=date(2024, 6, 1))
    assert out["authorised"] is True and out["basis"] == "licence"


def test_a_psychologist_without_a_licence_falls_back_to_psypact():
    s = Scheduler()
    s.add_provider("pr-2", "Dr B", "America/Denver")
    s.add_licence("pr-2", "CO", "2020-01-01", "2030-01-01")
    out = C.check(s, "pr-2", home_state="CO", patient_state="TX",
                  profession="psychologist", modality="video",
                  service_date=date(2024, 6, 1))
    assert out["authorised"] is True and out["basis"] == "PSYPACT"


def test_a_physician_without_a_licence_is_refused_despite_imlc():
    """Both states are IMLC members and it still does not authorise the visit,
    because the IMLC is not a licence."""
    s = Scheduler()
    s.add_provider("pr-3", "Dr C", "America/Denver")
    s.add_licence("pr-3", "CO", "2020-01-01", "2030-01-01")
    out = C.check(s, "pr-3", home_state="CO", patient_state="WA",
                  profession="physician", modality="video",
                  service_date=date(2024, 6, 1))
    assert out["authorised"] is False
    assert any(r["compact"] == "IMLC" and not r["authorises"]
               for r in out["compacts"])


# --------------------------------------------------------------------------
# EXDATE
# --------------------------------------------------------------------------

def _series(rrule="FREQ=WEEKLY;BYDAY=TU;COUNT=6"):
    return RC.Series("s", "pr-1", "pt-1", "vt-1", "2024-10-01", 9, 0,
                     "America/New_York", rrule)


def test_excluding_one_date_removes_only_that_occurrence():
    """'Cancel just the 12 November one' -- the most common real request."""
    base = [o["local_date"] for o in _series().occurrences(Scheduler)]
    out = RC.exclude(_series(), "2024-10-15")
    dates = [o["local_date"] for o in out.occurrences(Scheduler)]
    assert "2024-10-15" in base and "2024-10-15" not in dates
    assert len(dates) == len(base) - 1


def test_the_rule_is_unchanged_by_an_exclusion():
    """Rewriting the rule to route around one date changes what every OTHER
    occurrence means, and nothing in the new rule records that a cancellation
    happened."""
    out = RC.exclude(_series(), "2024-10-15")
    assert out.rule["freq"] == "WEEKLY"
    assert out.rule["byday"] == _series().rule["byday"]
    assert "EXDATE=20241015" in RC.format_rrule(out.rule)


def test_count_is_consumed_before_exdate_is_applied():
    """RFC 5545: an excluded occurrence still counts toward COUNT, so a
    cancellation SHORTENS the series rather than sliding a replacement onto the
    end. Backwards, and the patient who cancelled one week is booked an extra
    one."""
    base = [o["local_date"] for o in _series().occurrences(Scheduler)]
    out = RC.exclude(_series(), "2024-10-15")
    dates = [o["local_date"] for o in out.occurrences(Scheduler)]
    assert dates[-1] == base[-1]              # the series does NOT extend


def test_several_exclusions_accumulate():
    s = RC.exclude(RC.exclude(_series(), "2024-10-15"), "2024-10-29")
    dates = [o["local_date"] for o in s.occurrences(Scheduler)]
    assert "2024-10-15" not in dates and "2024-10-29" not in dates
    assert len(dates) == 4


def test_excluding_the_same_date_twice_is_idempotent():
    s = RC.exclude(RC.exclude(_series(), "2024-10-15"), "2024-10-15")
    assert s.rule["exdate"].count(date(2024, 10, 15)) == 1


def test_an_exdate_rrule_round_trips():
    s = _series("FREQ=WEEKLY;BYDAY=TU;COUNT=4;EXDATE=20241008")
    assert date(2024, 10, 8) in s.rule["exdate"]
    assert RC.parse_rrule(RC.format_rrule(s.rule))["exdate"] == s.rule["exdate"]


# --------------------------------------------------------------------------
# series booking
# --------------------------------------------------------------------------

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


def _future_series(weeks=4, hour=9):
    start = date.today() + timedelta(days=14)
    while start.weekday() != 1:
        start += timedelta(days=1)
    return RC.Series("s", "pr-1", "pt-1", "vt-1", start.isoformat(), hour, 0,
                     "America/New_York",
                     f"FREQ=WEEKLY;BYDAY=TU;COUNT={weeks}")


def test_a_clean_series_books_every_occurrence():
    s = _sched()
    out = RC.book_series(_future_series(), s, Scheduler)
    assert out["n_booked"] == 4 and out["n_failed"] == 0


def test_best_effort_keeps_what_it_could_book():
    """Seven sessions plus one to rearrange is better than none, and the
    patient would rather have the seven."""
    s = _sched()
    series = _future_series()
    occ = series.occurrences(Scheduler)
    s.book("pr-1", "pt-2", "vt-1", occ[2]["utc"])        # block the third
    out = RC.book_series(series, s, Scheduler, policy="best-effort")
    assert out["n_booked"] == 3 and out["n_failed"] == 1
    assert not out["rolled_back"]


def test_all_or_nothing_rolls_back():
    """A titration schedule with a gap in the middle is not a shorter
    titration, it is a different and possibly unsafe one."""
    s = _sched()
    series = _future_series()
    occ = series.occurrences(Scheduler)
    s.book("pr-1", "pt-2", "vt-1", occ[2]["utc"])
    out = RC.book_series(series, s, Scheduler, policy="all-or-nothing")
    assert out["n_booked"] == 0
    assert len(out["rolled_back"]) == 2
    assert "only makes sense complete" in out["outcome"]


def test_stop_at_first_clash_does_not_book_past_the_gap():
    """Correct when later occurrences depend on earlier ones."""
    s = _sched()
    series = _future_series()
    occ = series.occurrences(Scheduler)
    s.book("pr-1", "pt-2", "vt-1", occ[1]["utc"])
    out = RC.book_series(series, s, Scheduler,
                         policy="stop-at-first-clash")
    assert out["n_booked"] == 1
    assert any(r["status"] == "stopped" for r in out["results"])


def test_an_unknown_policy_is_refused():
    """The right answer depends on what the series IS, so there is no safe
    default."""
    with pytest.raises(RC.RecurrenceError) as e:
        RC.book_series(_future_series(), _sched(), Scheduler, policy="whatever")
    assert "no safe default" in str(e.value)


def test_a_nonexistent_occurrence_is_not_reported_as_a_clash():
    """Spring-forward deletes a wall clock. That is a scheduling fact, not a
    conflict, and confusing the two invites someone to 'fix' it by moving the
    whole series."""
    s = _sched()
    # 02:30 on US spring-forward Sunday does not exist
    series = RC.Series("s", "pr-1", "pt-1", "vt-1", "2100-03-07", 2, 30,
                       "America/New_York", "FREQ=WEEKLY;BYDAY=SU;COUNT=3")
    occ = series.occurrences(Scheduler)
    assert any(o["kind"] == "nonexistent" for o in occ)
    out = RC.book_series(series, s, Scheduler)
    assert out["n_nonexistent"] >= 1
    assert all(r["status"] != "clash" for r in out["results"]
               if r["status"] == "nonexistent")
