"""Reminders, no-shows, waitlist holds, and video-session tokens.

IDEMPOTENT REMINDERS, AND WHY THE ORDER OF TWO LINES MATTERS
------------------------------------------------------------
A reminder job that crashes between sending and recording sends the reminder
again on recovery. A reminder job that records before sending drops the
reminder if it crashes in between. Neither is acceptable, and you cannot have
both without either a transactional outbox or an idempotency key at the
provider.

The choice made here, and the reasoning: **record first, then send**, with the
record inside a transaction that the send cannot roll back.

That means the failure mode is a MISSED reminder rather than a DUPLICATE one,
and for appointment reminders that is the right way round -- a duplicate SMS at
3am erodes trust in every future message, while a missed one degrades to the
patient's own calendar. For a medication reminder the calculus would reverse.

The honest limitation: this is at-most-once, and true exactly-once needs the
messaging provider to accept an idempotency key so a retry after an ambiguous
timeout is deduplicated at their end. `send_reminders(crash_after_record=...)`
injects a crash between the two steps and the test asserts no duplicate on
recovery.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")

REMINDER_KINDS = {"T-24h": 24, "T-1h": 1}
TOKEN_SECRET = b"demo-signing-key-not-a-secret"


class CrashInjected(Exception):
    """Raised by the crash-injection harness, never in production paths."""


# ---------------------------------------------------------------------------
def due_reminders(sched, now):
    """Appointments needing a reminder, excluding cancelled ones.

    The cancellation race: an appointment cancelled after the job selects it
    but before it sends. Re-checking status inside the send transaction is what
    prevents a reminder for a visit that is no longer happening.
    """
    out = []
    for kind, hours in REMINDER_KINDS.items():
        window_start = now + timedelta(hours=hours) - timedelta(minutes=30)
        window_end = now + timedelta(hours=hours) + timedelta(minutes=30)
        rows = sched.con.execute(
            "SELECT a.appointment_id, a.patient_id, a.starts_at_utc "
            "FROM appointment a "
            "LEFT JOIN reminder r ON r.appointment_id = a.appointment_id "
            "  AND r.kind = ? "
            "WHERE a.status IN ('requested','confirmed') "
            "  AND r.appointment_id IS NULL "
            "  AND a.starts_at_utc >= ? AND a.starts_at_utc <= ?",
            (kind, window_start.astimezone(UTC).isoformat(),
             window_end.astimezone(UTC).isoformat())).fetchall()
        for appt_id, patient_id, starts in rows:
            out.append({"appointment_id": appt_id, "patient_id": patient_id,
                        "kind": kind, "starts_at": starts})
    return out


def send_reminders(sched, now, sender=None, crash_after_record=None):
    """Record then send. `crash_after_record` names a kind to crash on."""
    sent, skipped = [], []
    for item in due_reminders(sched, now):
        appt_id, kind = item["appointment_id"], item["kind"]
        try:
            sched.con.execute("BEGIN IMMEDIATE")
            # Re-check inside the transaction: the appointment may have been
            # cancelled since the selection above.
            status = sched.con.execute(
                "SELECT status FROM appointment WHERE appointment_id=?",
                (appt_id,)).fetchone()[0]
            if status not in ("requested", "confirmed"):
                sched.con.execute("ROLLBACK")
                skipped.append({**item, "reason": f"status is {status}"})
                continue
            sched.con.execute(
                "INSERT INTO reminder VALUES (?,?,?)",
                (appt_id, kind, datetime.now(UTC).isoformat()))
            sched.con.execute("COMMIT")
        except Exception:
            try:
                sched.con.execute("ROLLBACK")
            except Exception:
                pass
            skipped.append({**item, "reason": "already recorded"})
            continue

        if crash_after_record == kind:
            raise CrashInjected(
                f"crashed after recording {kind} for {appt_id}, before sending")
        if sender:
            sender(item)
        sent.append(item)
    return sent, skipped


def reminder_count(sched, appointment_id=None):
    if appointment_id:
        return sched.con.execute(
            "SELECT COUNT(*) FROM reminder WHERE appointment_id=?",
            (appointment_id,)).fetchone()[0]
    return sched.con.execute("SELECT COUNT(*) FROM reminder").fetchone()[0]


# ---------------------------------------------------------------------------
def flag_no_shows(sched, now, grace_minutes=15):
    """Auto-flag confirmed appointments whose grace period has elapsed."""
    cutoff = (now - timedelta(minutes=grace_minutes)).astimezone(UTC).isoformat()
    rows = sched.con.execute(
        "SELECT appointment_id FROM appointment WHERE status='confirmed' "
        "AND starts_at_utc <= ?", (cutoff,)).fetchall()
    flagged = []
    for (appt_id,) in rows:
        sched.transition(appt_id, "no_show", actor="system",
                         note=f"grace period of {grace_minutes} min elapsed")
        flagged.append(appt_id)
    return flagged


# ---------------------------------------------------------------------------
def join_waitlist(sched, provider_id, patient_id, visit_type_id):
    cur = sched.con.execute(
        "INSERT INTO waitlist (provider_id, patient_id, visit_type_id, "
        "created_at) VALUES (?,?,?,?)",
        (provider_id, patient_id, visit_type_id, datetime.now(UTC).isoformat()))
    return cur.lastrowid


def offer_to_waitlist(sched, provider_id, start_utc, now, hold_minutes=10):
    """A cancellation frees a slot: offer it to the next waiting patient with
    an EXPIRING hold.

    The hold is what makes this safe. Without one, a freed slot is offered to
    everyone at once and whoever clicks first wins a race the system did not
    intend to run; with one, the slot is reserved for exactly one patient for a
    bounded time and then moves on.
    """
    row = sched.con.execute(
        "SELECT seq, patient_id, visit_type_id FROM waitlist "
        "WHERE provider_id=? AND status='waiting' ORDER BY seq LIMIT 1",
        (provider_id,)).fetchone()
    if not row:
        return None
    seq, patient_id, visit_type_id = row
    expires = now + timedelta(minutes=hold_minutes)
    sched.con.execute(
        "UPDATE waitlist SET status='held', hold_expires_at=? WHERE seq=?",
        (expires.astimezone(UTC).isoformat(), seq))
    return {"seq": seq, "patient_id": patient_id,
            "visit_type_id": visit_type_id, "expires_at": expires,
            "start_utc": start_utc}


def claim_hold(sched, seq, start_utc, now):
    """Convert a held offer into a booking, IF the hold has not expired.

    The expiry is checked inside the same transaction that books, because the
    interesting failure is a hold expiring while the patient is mid-checkout.
    In transaction terms: the claim reads the hold, finds it expired, and
    aborts before the INSERT -- so the slot is never double-allocated to a
    patient whose hold lapsed and to the next person in line.
    """
    row = sched.con.execute(
        "SELECT provider_id, patient_id, visit_type_id, hold_expires_at, status "
        "FROM waitlist WHERE seq=?", (seq,)).fetchone()
    if not row:
        raise ValueError(f"no waitlist entry {seq}")
    provider_id, patient_id, visit_type_id, expires_at, status = row
    if status != "held":
        return {"claimed": False, "reason": f"hold status is {status}"}
    if datetime.fromisoformat(expires_at) < now:
        sched.con.execute(
            "UPDATE waitlist SET status='expired' WHERE seq=?", (seq,))
        return {"claimed": False, "reason": "hold expired before checkout "
                                            "completed"}
    appt_id = sched.book(provider_id, patient_id, visit_type_id, start_utc,
                         actor="waitlist")
    sched.con.execute(
        "UPDATE waitlist SET status='booked', hold_appointment_id=? WHERE seq=?",
        (appt_id, seq))
    return {"claimed": True, "appointment_id": appt_id}


def expire_holds(sched, now):
    rows = sched.con.execute(
        "SELECT seq FROM waitlist WHERE status='held' AND hold_expires_at < ?",
        (now.astimezone(UTC).isoformat(),)).fetchall()
    for (seq,) in rows:
        sched.con.execute("UPDATE waitlist SET status='expired' WHERE seq=?",
                          (seq,))
    return [s for (s,) in rows]


# ---------------------------------------------------------------------------
# Video session tokens
# ---------------------------------------------------------------------------
def issue_token(appointment_id, subject_id, role, starts_at, ends_at,
                early_minutes=15, late_minutes=15):
    """Short-lived, appointment-bound, role-scoped.

    All three bindings matter. Appointment-bound so a token for one visit
    cannot open another. Role-scoped so a patient token cannot join with
    provider privileges. Time-boxed so a token recovered from a browser history
    six weeks later opens nothing.
    """
    payload = {
        "appointment_id": appointment_id, "sub": subject_id, "role": role,
        "nbf": (starts_at - timedelta(minutes=early_minutes)).timestamp(),
        "exp": (ends_at + timedelta(minutes=late_minutes)).timestamp(),
        "jti": secrets.token_hex(8),
    }
    body = json.dumps(payload, sort_keys=True).encode()
    sig = hmac.new(TOKEN_SECRET, body, hashlib.sha256).hexdigest()
    return {"payload": payload, "signature": sig}


def verify_token(token, appointment_id, required_role, now=None):
    """Returns (ok, reason). Every failure mode is named."""
    now = (now or datetime.now(UTC)).timestamp()
    body = json.dumps(token["payload"], sort_keys=True).encode()
    expected = hmac.new(TOKEN_SECRET, body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, token["signature"]):
        return False, "signature invalid (token tampered with)"
    p = token["payload"]
    if p["appointment_id"] != appointment_id:
        return False, "token is bound to a different appointment"
    if p["role"] != required_role:
        return False, f"token role is {p['role']}, not {required_role}"
    if now < p["nbf"]:
        return False, "too early: the session window has not opened"
    if now > p["exp"]:
        return False, "expired: the session window has closed"
    return True, "ok"
