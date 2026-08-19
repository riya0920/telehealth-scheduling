"""Concurrency proof, licensure traps, the DST suite, and crash injection.

Run:  python run_demo.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import workflow as W
from scheduling import (ACTIVE, LicenceViolation, Scheduler, SlotTaken,
                        InvalidTransition)

UTC = ZoneInfo("UTC")
ET = ZoneInfo("America/New_York")
AZ = ZoneInfo("America/Phoenix")
OUT = "out"


def build(path=":memory:"):
    s = Scheduler(path)
    s.add_provider("dr-ellis", "Dr Ellis", "America/New_York")
    # licensed in NY throughout, in AZ only until 30 April -- the planted trap
    s.add_licence("dr-ellis", "NY", "2023-01-01", "2026-12-31")
    s.add_licence("dr-ellis", "AZ", "2023-01-01", "2025-04-30")
    for weekday in range(5):
        s.add_working_hours("dr-ellis", weekday, "09:00", "17:00")
    s.add_patient("pat-ny", "Nina", "NY", "America/New_York")
    s.add_patient("pat-az", "Alex", "AZ", "America/Phoenix")
    s.add_visit_type("followup", 30, "video", lead_time_hours=2,
                     buffer_minutes=10)
    return s


def main():
    os.makedirs(OUT, exist_ok=True)
    payload = {}

    # =====================================================================
    print("=" * 78)
    print("1. NO DOUBLE-BOOKING UNDER CONCURRENCY")
    print("=" * 78)
    runs, all_ok = 5, True
    for run in range(runs):
        tmp = os.path.join(tempfile.mkdtemp(), f"sched{run}.db")
        s = build(tmp)
        slot = datetime(2025, 3, 4, 14, 0, tzinfo=UTC)
        results, errors = [], []
        barrier = threading.Barrier(100)

        def attempt(i):
            con = s.connect()
            barrier.wait()                     # maximise the collision
            try:
                results.append(s.book("dr-ellis", "pat-ny", "followup", slot,
                                      con=con))
            except SlotTaken:
                errors.append("taken")
            except Exception as exc:
                errors.append(type(exc).__name__)
            finally:
                con.close()

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ok = len(results) == 1 and s.active_count("dr-ellis") == 1
        all_ok &= ok
        print(f"  run {run+1}: 100 concurrent requests -> "
              f"{len(results)} success, {len(errors)} rejected, "
              f"{s.active_count('dr-ellis')} row(s) in the table  "
              f"{'PASS' if ok else 'FAIL'}")
    print(f"\n  {runs}/{runs} runs booked exactly once"
          if all_ok else "\n  FAILURE: double booking occurred")
    print("\n  Mechanism: BEGIN IMMEDIATE takes the write lock before the")
    print("  overlap check, so the check and the insert are one serialised")
    print("  unit. SELECT-then-INSERT cannot do this at any isolation level")
    print("  below serialisable -- both transactions read 'free' before either")
    print("  writes, and both then write.")
    print("\n  The right answer in Postgres is an exclusion constraint:")
    print("    EXCLUDE USING gist (provider_id WITH =,")
    print("                        tstzrange(starts_at, ends_at) WITH &&)")
    print("  There is no race because there is no read-then-write: the check")
    print("  IS the write, inside the index. It also cannot be bypassed by a")
    print("  careless code path, or by someone running SQL by hand at 2am.")
    payload["concurrency"] = {"runs": runs, "all_single_booking": all_ok}

    # =====================================================================
    print("\n" + "=" * 78)
    print("2. LICENSURE BY STATE, ON THE DATE OF SERVICE")
    print("=" * 78)
    s = build()
    print("  Dr Ellis: NY licence to 2026-12-31, AZ licence to 2025-04-30")
    cases = [
        ("NY patient, visit 2025-03-04", "pat-ny", datetime(2025, 3, 4, 14, 0, tzinfo=UTC), True),
        ("AZ patient, visit 2025-03-04 (licence valid)", "pat-az", datetime(2025, 3, 4, 15, 0, tzinfo=UTC), True),
        ("AZ patient, visit 2025-04-30 (last valid day)", "pat-az", datetime(2025, 4, 30, 15, 0, tzinfo=UTC), True),
        ("AZ patient, visit 2025-05-01 (day AFTER expiry)", "pat-az", datetime(2025, 5, 1, 15, 0, tzinfo=UTC), False),
        ("AZ patient, visit 2025-08-12 (well after expiry)", "pat-az", datetime(2025, 8, 12, 15, 0, tzinfo=UTC), False),
    ]
    licence_results = {}
    for label, patient, when, should_allow in cases:
        try:
            s.book("dr-ellis", patient, "followup", when)
            got = True
        except LicenceViolation:
            got = False
        licence_results[label] = got
        mark = "ok" if got == should_allow else "POLICY FAILURE"
        print(f"  [{'booked ' if got else 'BLOCKED'}] {label:<48} {mark}")
    print("\n  The fourth and fifth rows are the trap. Booked TODAY, they would")
    print("  pass any check that asks 'is this provider licensed now?'. The")
    print("  question is whether the licence is valid on the DATE OF SERVICE,")
    print("  and getting it wrong is a compliance violation in the real world,")
    print("  not a bug.")

    print("\n  What happens when a licence expires BETWEEN booking and visit:")
    s2 = build()
    future = datetime(2025, 4, 15, 15, 0, tzinfo=UTC)
    appt = s2.book("dr-ellis", "pat-az", "followup", future)
    print(f"    booked {appt} for 2025-04-15 while the AZ licence is valid")
    s2.con.execute("UPDATE licence SET expires_on='2025-03-31' "
                   "WHERE provider_id='dr-ellis' AND state='AZ'")
    gaps = s2.licence_gaps("dr-ellis", "AZ", date(2025, 4, 1), date(2025, 4, 30))
    still_booked = s2.appointment(appt)[6]
    print(f"    licence is then shortened to 2025-03-31")
    print(f"    the appointment is STILL '{still_booked}' -- nothing retroactively")
    print(f"    cancels it, and {len(gaps)} days in April are now uncovered")
    print("\n    Decision: BLOCK AT BOOKING, and run a licence-change sweep that")
    print("    flags already-booked appointments for rebooking. Book-then-flag")
    print("    is the only workable design because licences change after the")
    print("    fact, but it carries an ops burden -- somebody must work that")
    print("    queue, and if nobody does, the control is theatre.")
    payload["licensure"] = licence_results
    payload["licence_gap_days_after_change"] = len(gaps)

    # =====================================================================
    print("\n" + "=" * 78)
    print("3. THE DST SUITE")
    print("=" * 78)
    s3 = build()
    print("  Provider in ET (observes DST). Patient in AZ (does NOT).")
    print("  US spring forward 2025: 2025-03-09, 02:00 ET -> 03:00 ET\n")

    for label, day in [("March (ET on standard time)", date(2025, 3, 4)),
                       ("April (ET on daylight time)", date(2025, 4, 8))]:
        slots = s3.availability("dr-ellis", "pat-az", "followup", day,
                                now=datetime(2025, 1, 1, tzinfo=UTC))
        first = slots[0] if slots else None
        if first:
            et = first.astimezone(ET)
            az = first.astimezone(AZ)
            print(f"  {label}")
            print(f"    provider 09:00 {et.tzname()} = patient "
                  f"{az.strftime('%H:%M')} {az.tzname()}")
    print("\n  The provider's recurring 09:00 stays at 09:00 local across the")
    print("  transition; the PATIENT sees it move by an hour, because Arizona")
    print("  does not change. A rule stored as a UTC instant would instead")
    print("  drag the provider's morning to 08:00 and nobody would notice")
    print("  until a patient arrived an hour early.")

    nonexistent = Scheduler._local_to_utc(date(2025, 3, 9), 2, 30, ET)
    exists_before = Scheduler._local_to_utc(date(2025, 3, 9), 1, 30, ET)
    exists_after = Scheduler._local_to_utc(date(2025, 3, 9), 3, 30, ET)
    print(f"\n  02:30 ET on 2025-03-09 -> {nonexistent}  (that time never occurs)")
    print(f"  01:30 ET on 2025-03-09 -> {exists_before.astimezone(ET).strftime('%H:%M %Z')}")
    print(f"  03:30 ET on 2025-03-09 -> {exists_after.astimezone(ET).strftime('%H:%M %Z')}")
    print("\n  Python will happily construct 02:30 on that date and hand back a")
    print("  plausible instant. Returning None makes the non-existence explicit")
    print("  instead of silently booking someone at 01:30 or 03:30.")
    print("\n  'We store everything in UTC' is necessary and NOT sufficient:")
    print("  availability RULES live in local wall-clock time, recurrences are")
    print("  local, and rendering needs a zone. UTC alone loses the wall-clock")
    print("  semantics that scheduling actually runs on.")
    payload["dst"] = {"nonexistent_0230_returns_none": nonexistent is None}

    # =====================================================================
    print("\n" + "=" * 78)
    print("4. IDEMPOTENT REMINDERS UNDER CRASH INJECTION")
    print("=" * 78)
    s4 = build()
    now = datetime(2025, 3, 3, 14, 0, tzinfo=UTC)
    appt = s4.book("dr-ellis", "pat-ny", "followup",
                   datetime(2025, 3, 4, 14, 0, tzinfo=UTC),
                   actor="patient")
    sent_log = []
    try:
        W.send_reminders(s4, now, sender=sent_log.append,
                         crash_after_record="T-24h")
    except W.CrashInjected as exc:
        print(f"  crash injected: {exc}")
    print(f"  reminders actually delivered before the crash: {len(sent_log)}")
    print(f"  reminder rows recorded: {W.reminder_count(s4, appt)}")

    sent2, skipped2 = W.send_reminders(s4, now, sender=sent_log.append)
    print(f"  after recovery, the job runs again:")
    print(f"    newly sent {len(sent2)}, skipped {len(skipped2)}")
    print(f"    total reminder rows for {appt}: {W.reminder_count(s4, appt)}")
    print(f"    NO DUPLICATE: {W.reminder_count(s4, appt) == 1}")
    print("\n  Record-then-send means the failure mode is a MISSED reminder,")
    print("  never a duplicate. For appointment reminders that is the right way")
    print("  round: a duplicate 3am SMS erodes trust in every future message,")
    print("  a missed one degrades to the patient's own calendar. For a")
    print("  medication reminder the calculus would reverse.")

    s5 = build()
    appt5 = s5.book("dr-ellis", "pat-ny", "followup",
                    datetime(2025, 3, 4, 14, 0, tzinfo=UTC))
    s5.transition(appt5, "cancelled", actor="patient")
    sent5, skipped5 = W.send_reminders(s5, now)
    print(f"\n  cancelled inside the reminder window: sent {len(sent5)}, "
          f"skipped {len(skipped5)}")
    if skipped5:
        print(f"    reason: {skipped5[0]['reason']}")
    payload["reminders"] = {"no_duplicate_after_crash":
                            W.reminder_count(s4, appt) == 1,
                            "cancelled_not_reminded": len(sent5) == 0}

    # =====================================================================
    print("\n" + "=" * 78)
    print("5. STATE MACHINE, WAITLIST HOLDS, AND VIDEO TOKENS")
    print("=" * 78)
    s6 = build()
    a = s6.book("dr-ellis", "pat-ny", "followup",
                datetime(2025, 3, 4, 14, 0, tzinfo=UTC))
    print("  allowed transitions:")
    s6.transition(a, "checked_in")
    s6.transition(a, "completed")
    print(f"    confirmed -> checked_in -> completed  ok")
    for bad_to in ("confirmed", "cancelled"):
        try:
            s6.transition(a, bad_to)
            print(f"    completed -> {bad_to}: ALLOWED  <- should not happen")
        except InvalidTransition:
            print(f"    completed -> {bad_to}: refused (terminal state)")

    print("\n  waitlist hold expiring mid-checkout:")
    s7 = build()
    slot = datetime(2025, 3, 5, 14, 0, tzinfo=UTC)
    held = s7.book("dr-ellis", "pat-ny", "followup", slot)
    W.join_waitlist(s7, "dr-ellis", "pat-az", "followup")
    s7.transition(held, "cancelled", actor="patient")
    t_now = datetime(2025, 3, 4, 12, 0, tzinfo=UTC)
    offer = W.offer_to_waitlist(s7, "dr-ellis", slot, t_now, hold_minutes=10)
    print(f"    offered to {offer['patient_id']}, hold expires "
          f"{offer['expires_at'].strftime('%H:%M')}")
    late = t_now + timedelta(minutes=11)
    result = W.claim_hold(s7, offer["seq"], slot, late)
    print(f"    claim at +11 min -> claimed={result['claimed']}: "
          f"{result.get('reason')}")
    print("    In transaction terms: the claim reads the hold, finds it")
    print("    expired, and aborts BEFORE the insert -- so the slot is never")
    print("    allocated both to the lapsed patient and to the next in line.")

    print("\n  video session tokens:")
    starts = datetime(2025, 3, 4, 14, 0, tzinfo=UTC)
    ends = starts + timedelta(minutes=30)
    ptok = W.issue_token(a, "pat-ny", "patient", starts, ends)
    checks = [
        ("patient token, correct appointment, in window",
         W.verify_token(ptok, a, "patient", starts + timedelta(minutes=5))),
        ("patient token used as PROVIDER",
         W.verify_token(ptok, a, "provider", starts + timedelta(minutes=5))),
        ("patient token on a DIFFERENT appointment",
         W.verify_token(ptok, "appt-other", "patient", starts)),
        ("token used 2 hours early",
         W.verify_token(ptok, a, "patient", starts - timedelta(hours=2))),
        ("token used the next day",
         W.verify_token(ptok, a, "patient", starts + timedelta(days=1))),
    ]
    tampered = {"payload": {**ptok["payload"], "role": "provider"},
                "signature": ptok["signature"]}
    checks.append(("tampered payload (role escalated)",
                   W.verify_token(tampered, a, "provider", starts)))
    for label, (ok, reason) in checks:
        print(f"    [{'ALLOW' if ok else 'deny ':<5}] {label:<44} {reason}")
    payload["tokens"] = {label: ok for label, (ok, reason) in checks}

    with open(f"{OUT}/results.json", "w") as fh:
        json.dump(payload, fh, indent=2, default=str)
    print(f"\nwrote {OUT}/results.json")


if __name__ == "__main__":
    main()
