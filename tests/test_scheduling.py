"""Tests for the constraints that make scheduling hard.

Booking a free slot is easy. What these test is that the system cannot book an
unlicensed visit, cannot double-book under load, cannot lose an hour to DST,
and cannot send the same reminder twice after a crash.
"""

import os
import sys
import tempfile
import threading
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import workflow as W
from scheduling import (InvalidTransition, LicenceViolation, Scheduler,
                        SlotTaken)

UTC = ZoneInfo("UTC")
ET = ZoneInfo("America/New_York")
AZ = ZoneInfo("America/Phoenix")


def build(path=":memory:"):
    s = Scheduler(path)
    s.add_provider("dr-ellis", "Dr Ellis", "America/New_York")
    s.add_licence("dr-ellis", "NY", "2023-01-01", "2026-12-31")
    s.add_licence("dr-ellis", "AZ", "2023-01-01", "2025-04-30")
    for weekday in range(5):
        s.add_working_hours("dr-ellis", weekday, "09:00", "17:00")
    s.add_patient("pat-ny", "Nina", "NY", "America/New_York")
    s.add_patient("pat-az", "Alex", "AZ", "America/Phoenix")
    s.add_visit_type("followup", 30, "video", lead_time_hours=2,
                     buffer_minutes=10)
    return s


@pytest.fixture
def s():
    return build()


# ---------------------------------------------------------------------------
# Licensure
# ---------------------------------------------------------------------------
def test_licence_is_checked_on_the_date_of_service_not_today(s):
    """The planted trap. Booked today, a visit after the licence expires
    passes any check that asks 'is this provider licensed now?'."""
    assert s.is_licensed("dr-ellis", "AZ", date(2025, 4, 30))
    assert not s.is_licensed("dr-ellis", "AZ", date(2025, 5, 1))
    with pytest.raises(LicenceViolation):
        s.book("dr-ellis", "pat-az", "followup",
               datetime(2025, 5, 1, 15, 0, tzinfo=UTC))


def test_licence_boundaries_are_inclusive(s):
    assert s.is_licensed("dr-ellis", "AZ", date(2023, 1, 1))
    assert not s.is_licensed("dr-ellis", "AZ", date(2022, 12, 31))
    assert s.is_licensed("dr-ellis", "AZ", date(2025, 4, 30))


def test_provider_unlicensed_in_the_patients_state_is_blocked(s):
    s.add_patient("pat-tx", "Tom", "TX", "America/Chicago")
    with pytest.raises(LicenceViolation):
        s.book("dr-ellis", "pat-tx", "followup",
               datetime(2025, 3, 4, 15, 0, tzinfo=UTC))


def test_licence_governs_the_patients_state_not_the_providers(s):
    """Telehealth's defining constraint: the licence must cover where the
    PATIENT is, not where the provider sits."""
    assert s.book("dr-ellis", "pat-az", "followup",
                  datetime(2025, 3, 4, 15, 0, tzinfo=UTC))


def test_availability_is_empty_when_unlicensed(s):
    slots = s.availability("dr-ellis", "pat-az", "followup", date(2025, 5, 1),
                           now=datetime(2025, 1, 1, tzinfo=UTC))
    assert slots == []


def test_licence_gap_sweep_finds_newly_uncovered_days(s):
    s.con.execute("UPDATE licence SET expires_on='2025-03-31' "
                  "WHERE provider_id='dr-ellis' AND state='AZ'")
    gaps = s.licence_gaps("dr-ellis", "AZ", date(2025, 4, 1), date(2025, 4, 30))
    assert len(gaps) == 30


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
def test_one_hundred_concurrent_requests_book_exactly_once():
    tmp = os.path.join(tempfile.mkdtemp(), "c.db")
    s = build(tmp)
    slot = datetime(2025, 3, 4, 14, 0, tzinfo=UTC)
    wins, barrier = [], threading.Barrier(100)

    def attempt():
        con = s.connect()
        barrier.wait()
        try:
            wins.append(s.book("dr-ellis", "pat-ny", "followup", slot, con=con))
        except Exception:
            pass
        finally:
            con.close()

    threads = [threading.Thread(target=attempt) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1
    assert s.active_count("dr-ellis") == 1


def test_overlapping_bookings_for_one_provider_are_refused(s):
    s.book("dr-ellis", "pat-ny", "followup",
           datetime(2025, 3, 4, 14, 0, tzinfo=UTC))
    with pytest.raises(SlotTaken):
        s.book("dr-ellis", "pat-az", "followup",
               datetime(2025, 3, 4, 14, 15, tzinfo=UTC))


def test_buffer_between_visits_is_enforced(s):
    """A 30-minute visit with a 10-minute buffer blocks a start 30 minutes
    later, because the buffer is part of the occupied range."""
    s.book("dr-ellis", "pat-ny", "followup",
           datetime(2025, 3, 4, 14, 0, tzinfo=UTC))
    with pytest.raises(SlotTaken):
        s.book("dr-ellis", "pat-az", "followup",
               datetime(2025, 3, 4, 14, 30, tzinfo=UTC))
    assert s.book("dr-ellis", "pat-az", "followup",
                  datetime(2025, 3, 4, 14, 45, tzinfo=UTC))


def test_one_patient_cannot_be_in_two_places_at_once(s):
    s.add_provider("dr-two", "Dr Two", "America/New_York")
    s.add_licence("dr-two", "NY", "2023-01-01", "2026-12-31")
    s.book("dr-ellis", "pat-ny", "followup",
           datetime(2025, 3, 4, 14, 0, tzinfo=UTC))
    with pytest.raises(SlotTaken):
        s.book("dr-two", "pat-ny", "followup",
               datetime(2025, 3, 4, 14, 10, tzinfo=UTC))


def test_cancelled_appointments_free_the_slot(s):
    a = s.book("dr-ellis", "pat-ny", "followup",
               datetime(2025, 3, 4, 14, 0, tzinfo=UTC))
    s.transition(a, "cancelled")
    assert s.book("dr-ellis", "pat-az", "followup",
                  datetime(2025, 3, 4, 14, 0, tzinfo=UTC))


# ---------------------------------------------------------------------------
# Time zones and DST
# ---------------------------------------------------------------------------
def test_nonexistent_local_time_returns_none():
    """02:30 on the spring-forward day never occurs. Python constructs it
    happily and returns a plausible instant; None makes it explicit."""
    assert Scheduler._local_to_utc(date(2025, 3, 9), 2, 30, ET) is None
    assert Scheduler._local_to_utc(date(2025, 3, 9), 1, 30, ET) is not None
    assert Scheduler._local_to_utc(date(2025, 3, 9), 3, 30, ET) is not None


def test_recurring_availability_stays_anchored_to_provider_local_time():
    """The bug everyone ships: a recurring 09:00 rule stored as a UTC instant
    silently moves to 08:00 when DST changes."""
    s = build()
    before = s.availability("dr-ellis", "pat-ny", "followup", date(2025, 3, 4),
                            now=datetime(2025, 1, 1, tzinfo=UTC))
    after = s.availability("dr-ellis", "pat-ny", "followup", date(2025, 4, 8),
                           now=datetime(2025, 1, 1, tzinfo=UTC))
    assert before[0].astimezone(ET).hour == 9
    assert after[0].astimezone(ET).hour == 9


def test_patient_in_arizona_sees_the_time_shift_across_dst():
    """Arizona does not observe DST, so a provider's fixed 09:00 ET moves for
    the patient. The provider's calendar is stable; the patient's is not."""
    s = build()
    march = s.availability("dr-ellis", "pat-az", "followup", date(2025, 3, 4),
                           now=datetime(2025, 1, 1, tzinfo=UTC))[0]
    april = s.availability("dr-ellis", "pat-az", "followup", date(2025, 4, 8),
                           now=datetime(2025, 1, 1, tzinfo=UTC))[0]
    assert march.astimezone(AZ).hour == 7
    assert april.astimezone(AZ).hour == 6


def test_all_stored_times_are_utc(s):
    a = s.book("dr-ellis", "pat-ny", "followup",
               datetime(2025, 3, 4, 14, 0, tzinfo=UTC))
    row = s.appointment(a)
    assert row[4].endswith("+00:00")
    assert datetime.fromisoformat(row[4]).utcoffset() == timedelta(0)


def test_rendering_in_the_patients_zone(s):
    utc_dt = datetime(2025, 3, 4, 14, 0, tzinfo=UTC)
    assert Scheduler.to_patient_time(utc_dt, "America/Phoenix").hour == 7


def test_lead_time_excludes_slots_that_are_too_soon(s):
    day = date(2025, 3, 4)
    late = s.availability("dr-ellis", "pat-ny", "followup", day,
                          now=datetime(2025, 3, 4, 20, 0, tzinfo=UTC))
    early = s.availability("dr-ellis", "pat-ny", "followup", day,
                           now=datetime(2025, 3, 1, tzinfo=UTC))
    assert len(late) < len(early)


def test_exception_days_remove_all_availability(s):
    s.add_exception_day("dr-ellis", "2025-03-04", "conference")
    assert s.availability("dr-ellis", "pat-ny", "followup", date(2025, 3, 4),
                          now=datetime(2025, 1, 1, tzinfo=UTC)) == []


def test_weekends_have_no_availability(s):
    assert s.availability("dr-ellis", "pat-ny", "followup", date(2025, 3, 8),
                          now=datetime(2025, 1, 1, tzinfo=UTC)) == []


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------
def test_allowed_transitions(s):
    a = s.book("dr-ellis", "pat-ny", "followup",
               datetime(2025, 3, 4, 14, 0, tzinfo=UTC))
    s.transition(a, "checked_in")
    s.transition(a, "completed")
    assert s.appointment(a)[6] == "completed"


def test_terminal_states_refuse_every_transition(s):
    a = s.book("dr-ellis", "pat-ny", "followup",
               datetime(2025, 3, 4, 14, 0, tzinfo=UTC))
    s.transition(a, "cancelled")
    for to in ("confirmed", "checked_in", "completed", "no_show"):
        with pytest.raises(InvalidTransition):
            s.transition(a, to)


def test_cannot_skip_states(s):
    a = s.book("dr-ellis", "pat-ny", "followup",
               datetime(2025, 3, 4, 14, 0, tzinfo=UTC))
    with pytest.raises(InvalidTransition):
        s.transition(a, "completed")       # confirmed -> completed is not allowed


def test_every_transition_is_audited(s):
    a = s.book("dr-ellis", "pat-ny", "followup",
               datetime(2025, 3, 4, 14, 0, tzinfo=UTC))
    s.transition(a, "checked_in", actor="front-desk")
    trail = s.audit_trail(a)
    assert len(trail) == 2
    assert trail[-1][1] == "front-desk"
    assert trail[-1][2:4] == ("confirmed", "checked_in")


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------
def test_reminders_are_not_duplicated_after_a_crash(s):
    """Crash injected between recording and sending. On recovery the job must
    not send again."""
    now = datetime(2025, 3, 3, 14, 0, tzinfo=UTC)
    a = s.book("dr-ellis", "pat-ny", "followup",
               datetime(2025, 3, 4, 14, 0, tzinfo=UTC))
    with pytest.raises(W.CrashInjected):
        W.send_reminders(s, now, crash_after_record="T-24h")
    assert W.reminder_count(s, a) == 1
    sent, _skipped = W.send_reminders(s, now)
    assert sent == []
    assert W.reminder_count(s, a) == 1


def test_reminders_are_sent_once_in_the_happy_path(s):
    now = datetime(2025, 3, 3, 14, 0, tzinfo=UTC)
    a = s.book("dr-ellis", "pat-ny", "followup",
               datetime(2025, 3, 4, 14, 0, tzinfo=UTC))
    log = []
    sent, _ = W.send_reminders(s, now, sender=log.append)
    assert len(sent) == 1 and len(log) == 1
    sent2, _ = W.send_reminders(s, now, sender=log.append)
    assert sent2 == [] and len(log) == 1


def test_cancelled_appointments_get_no_reminder(s):
    now = datetime(2025, 3, 3, 14, 0, tzinfo=UTC)
    a = s.book("dr-ellis", "pat-ny", "followup",
               datetime(2025, 3, 4, 14, 0, tzinfo=UTC))
    s.transition(a, "cancelled")
    sent, _ = W.send_reminders(s, now)
    assert sent == []
    assert W.reminder_count(s, a) == 0


def test_the_cancellation_race_is_closed(s):
    """Cancelled AFTER selection but BEFORE send. The status re-check inside
    the send transaction is what catches it."""
    now = datetime(2025, 3, 3, 14, 0, tzinfo=UTC)
    a = s.book("dr-ellis", "pat-ny", "followup",
               datetime(2025, 3, 4, 14, 0, tzinfo=UTC))
    due = W.due_reminders(s, now)
    assert len(due) == 1
    s.transition(a, "cancelled")
    sent, skipped = W.send_reminders(s, now)
    assert sent == []


def test_t_minus_1h_and_t_minus_24h_are_separate_reminders(s):
    a = s.book("dr-ellis", "pat-ny", "followup",
               datetime(2025, 3, 4, 14, 0, tzinfo=UTC))
    W.send_reminders(s, datetime(2025, 3, 3, 14, 0, tzinfo=UTC))
    W.send_reminders(s, datetime(2025, 3, 4, 13, 0, tzinfo=UTC))
    assert W.reminder_count(s, a) == 2


# ---------------------------------------------------------------------------
# No-shows and waitlist
# ---------------------------------------------------------------------------
def test_no_show_is_flagged_after_the_grace_period(s):
    start = datetime(2025, 3, 4, 14, 0, tzinfo=UTC)
    a = s.book("dr-ellis", "pat-ny", "followup", start)
    assert W.flag_no_shows(s, start + timedelta(minutes=5)) == []
    assert a in W.flag_no_shows(s, start + timedelta(minutes=20))
    assert s.appointment(a)[6] == "no_show"


def test_waitlist_hold_expires_and_the_claim_is_refused(s):
    slot = datetime(2025, 3, 5, 14, 0, tzinfo=UTC)
    a = s.book("dr-ellis", "pat-ny", "followup", slot)
    W.join_waitlist(s, "dr-ellis", "pat-az", "followup")
    s.transition(a, "cancelled")
    now = datetime(2025, 3, 4, 12, 0, tzinfo=UTC)
    offer = W.offer_to_waitlist(s, "dr-ellis", slot, now, hold_minutes=10)
    result = W.claim_hold(s, offer["seq"], slot, now + timedelta(minutes=11))
    assert result["claimed"] is False
    assert "expired" in result["reason"]


def test_waitlist_hold_claimed_in_time_books(s):
    slot = datetime(2025, 3, 5, 14, 0, tzinfo=UTC)
    a = s.book("dr-ellis", "pat-ny", "followup", slot)
    W.join_waitlist(s, "dr-ellis", "pat-az", "followup")
    s.transition(a, "cancelled")
    now = datetime(2025, 3, 4, 12, 0, tzinfo=UTC)
    offer = W.offer_to_waitlist(s, "dr-ellis", slot, now, hold_minutes=10)
    result = W.claim_hold(s, offer["seq"], slot, now + timedelta(minutes=5))
    assert result["claimed"] is True
    assert s.appointment(result["appointment_id"])[6] == "confirmed"


def test_a_held_slot_cannot_be_claimed_twice(s):
    slot = datetime(2025, 3, 5, 14, 0, tzinfo=UTC)
    a = s.book("dr-ellis", "pat-ny", "followup", slot)
    W.join_waitlist(s, "dr-ellis", "pat-az", "followup")
    s.transition(a, "cancelled")
    now = datetime(2025, 3, 4, 12, 0, tzinfo=UTC)
    offer = W.offer_to_waitlist(s, "dr-ellis", slot, now)
    W.claim_hold(s, offer["seq"], slot, now + timedelta(minutes=1))
    second = W.claim_hold(s, offer["seq"], slot, now + timedelta(minutes=2))
    assert second["claimed"] is False


# ---------------------------------------------------------------------------
# Video tokens
# ---------------------------------------------------------------------------
@pytest.fixture
def token_setup(s):
    a = s.book("dr-ellis", "pat-ny", "followup",
               datetime(2025, 3, 4, 14, 0, tzinfo=UTC))
    starts = datetime(2025, 3, 4, 14, 0, tzinfo=UTC)
    ends = starts + timedelta(minutes=30)
    return a, starts, ends, W.issue_token(a, "pat-ny", "patient", starts, ends)


def test_valid_token_in_window(token_setup):
    a, starts, _e, tok = token_setup
    ok, _ = W.verify_token(tok, a, "patient", starts + timedelta(minutes=5))
    assert ok


def test_patient_token_cannot_join_as_provider(token_setup):
    a, starts, _e, tok = token_setup
    ok, reason = W.verify_token(tok, a, "provider", starts + timedelta(minutes=5))
    assert not ok and "role" in reason


def test_token_is_bound_to_one_appointment(token_setup):
    _a, starts, _e, tok = token_setup
    ok, reason = W.verify_token(tok, "appt-other", "patient", starts)
    assert not ok and "different appointment" in reason


def test_token_is_time_boxed(token_setup):
    a, starts, _e, tok = token_setup
    early, r1 = W.verify_token(tok, a, "patient", starts - timedelta(hours=2))
    late, r2 = W.verify_token(tok, a, "patient", starts + timedelta(days=1))
    assert not early and "too early" in r1
    assert not late and "expired" in r2


def test_tampered_token_is_rejected(token_setup):
    a, starts, _e, tok = token_setup
    tampered = {"payload": {**tok["payload"], "role": "provider"},
                "signature": tok["signature"]}
    ok, reason = W.verify_token(tampered, a, "provider", starts)
    assert not ok and "signature" in reason
