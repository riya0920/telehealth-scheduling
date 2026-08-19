"""Domain model, licensure, availability, and booking under concurrency.

THE THREE HARD PARTS, AND THEY ARE ALL HEALTHCARE PARTS
-------------------------------------------------------
Scheduling looks like a CRUD tutorial and is a constraint-satisfaction
minefield. The mines here:

1. LICENCE BY STATE, ON THE DATE OF SERVICE.
   A provider may treat a patient only if licensed in the state where the
   PATIENT is physically located, and the licence must be valid on the DATE OF
   SERVICE -- not the booking date. Booking on 1 March for a visit on 1 June
   with a licence expiring 1 May is a compliance violation in the real world,
   not a bug, and it is invisible to any check that looks at "today".

2. NO DOUBLE-BOOKING UNDER CONCURRENCY.
   Two requests for the same slot arriving in the same millisecond must produce
   exactly one booking. SELECT-then-INSERT cannot do this at any isolation
   level below serialisable, because both transactions read "free" before
   either writes.

3. TIME ZONES, AND SPECIFICALLY DST.
   Storing UTC is necessary and not sufficient. Availability RULES live in
   local wall-clock time ("every Tuesday at 09:00 in the provider's zone"), and
   a rule stored as a UTC instant silently shifts by an hour twice a year.

THE CONCURRENCY MECHANISM, AND THE SUBSTITUTION MADE
----------------------------------------------------
The elegant answer is a Postgres exclusion constraint:

    CREATE EXTENSION btree_gist;
    ALTER TABLE appointment ADD CONSTRAINT no_double_booking
      EXCLUDE USING gist (
        provider_id WITH =,
        tstzrange(starts_at, ends_at) WITH &&
      ) WHERE (status IN ('requested','confirmed','checked_in'));

The database itself then refuses any INSERT whose time range overlaps an
existing row for the same provider. There is no race because there is no
read-then-write: the check and the write are the same operation, inside the
index.

Postgres is not available here, so SQLite is used with `BEGIN IMMEDIATE`, which
takes a write lock for the whole transaction and serialises the check against
the write. That is genuinely weaker and the difference is worth naming:

  * it serialises ALL writers, not just those touching the same provider, so it
    is a scalability bottleneck the exclusion constraint is not;
  * correctness depends on every writer remembering to use BEGIN IMMEDIATE,
    whereas the constraint cannot be bypassed by a careless code path -- or by
    a script someone runs by hand against the database at 2am;
  * it is an application-level invariant, so a second application writing to
    the same database breaks it silently.

The concurrency test is nonetheless real: 100 threads, one slot, exactly one
success, repeated across runs.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")

SCHEMA = """
CREATE TABLE provider (
    provider_id TEXT PRIMARY KEY,
    name        TEXT,
    home_tz     TEXT NOT NULL,
    specialty   TEXT
);
CREATE TABLE licence (
    provider_id  TEXT,
    state        TEXT,
    effective_on TEXT NOT NULL,
    expires_on   TEXT NOT NULL,
    PRIMARY KEY (provider_id, state, effective_on)
);
CREATE TABLE working_hours (
    provider_id TEXT,
    weekday     INTEGER,      -- 0 = Monday, provider-LOCAL
    start_local TEXT,         -- "09:00", provider-local wall clock
    end_local   TEXT
);
CREATE TABLE exception_day (
    provider_id TEXT, day TEXT, reason TEXT
);
CREATE TABLE patient (
    patient_id TEXT PRIMARY KEY,
    name       TEXT,
    home_state TEXT NOT NULL,
    home_tz    TEXT NOT NULL
);
CREATE TABLE visit_type (
    visit_type_id TEXT PRIMARY KEY,
    minutes       INTEGER,
    modality      TEXT,
    lead_time_hours INTEGER,
    buffer_minutes  INTEGER
);
CREATE TABLE appointment (
    appointment_id TEXT PRIMARY KEY,
    provider_id TEXT, patient_id TEXT, visit_type_id TEXT,
    starts_at_utc TEXT NOT NULL,
    ends_at_utc   TEXT NOT NULL,
    status        TEXT NOT NULL,
    service_state TEXT,
    created_at    TEXT
);
CREATE INDEX idx_appt_provider ON appointment(provider_id, starts_at_utc);
CREATE INDEX idx_appt_patient  ON appointment(patient_id, starts_at_utc);

CREATE TABLE appointment_audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id TEXT, at TEXT, actor TEXT,
    from_status TEXT, to_status TEXT, note TEXT
);
CREATE TABLE reminder (
    appointment_id TEXT, kind TEXT, sent_at TEXT,
    PRIMARY KEY (appointment_id, kind)
);
CREATE TABLE waitlist (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT, patient_id TEXT, visit_type_id TEXT,
    created_at TEXT, hold_appointment_id TEXT, hold_expires_at TEXT,
    status TEXT DEFAULT 'waiting'
);
"""

# Appointment lifecycle. Transitions not listed here are refused.
ALLOWED_TRANSITIONS = {
    "requested": {"confirmed", "cancelled"},
    "confirmed": {"checked_in", "cancelled", "no_show"},
    "checked_in": {"completed", "cancelled"},
    "completed": set(),
    "cancelled": set(),
    "no_show": {"confirmed"},          # rebooked after a no-show
}
TERMINAL = {"completed", "cancelled"}
ACTIVE = ("requested", "confirmed", "checked_in")


class SchedulingError(Exception):
    pass


class SlotTaken(SchedulingError):
    pass


class LicenceViolation(SchedulingError):
    pass


class InvalidTransition(SchedulingError):
    pass


def _iso(dt):
    return dt.astimezone(UTC).isoformat()


class Scheduler:
    def __init__(self, path=":memory:"):
        self.path = path
        self.con = sqlite3.connect(path, check_same_thread=False,
                                   isolation_level=None)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript(SCHEMA)
        self._lock = threading.Lock()

    def connect(self):
        """A fresh connection, for concurrency tests that need real threads."""
        con = sqlite3.connect(self.path, check_same_thread=False,
                              isolation_level=None, timeout=30)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    # -- setup -----------------------------------------------------------
    def add_provider(self, provider_id, name, home_tz, specialty="Family"):
        self.con.execute("INSERT INTO provider VALUES (?,?,?,?)",
                         (provider_id, name, home_tz, specialty))

    def add_licence(self, provider_id, state, effective_on, expires_on):
        self.con.execute("INSERT INTO licence VALUES (?,?,?,?)",
                         (provider_id, state, effective_on, expires_on))

    def add_working_hours(self, provider_id, weekday, start_local, end_local):
        self.con.execute("INSERT INTO working_hours VALUES (?,?,?,?)",
                         (provider_id, weekday, start_local, end_local))

    def add_exception_day(self, provider_id, day, reason="unavailable"):
        self.con.execute("INSERT INTO exception_day VALUES (?,?,?)",
                         (provider_id, day, reason))

    def add_patient(self, patient_id, name, home_state, home_tz):
        self.con.execute("INSERT INTO patient VALUES (?,?,?,?)",
                         (patient_id, name, home_state, home_tz))

    def add_visit_type(self, visit_type_id, minutes, modality="video",
                       lead_time_hours=2, buffer_minutes=10):
        self.con.execute("INSERT INTO visit_type VALUES (?,?,?,?,?)",
                         (visit_type_id, minutes, modality, lead_time_hours,
                          buffer_minutes))

    # -- licensure -------------------------------------------------------
    def is_licensed(self, provider_id, state, service_date):
        """Licensed in `state` on the DATE OF SERVICE.

        The date-of-service check, not the booking date, is the entire point.
        A licence that is valid today and expires before the appointment is a
        licence the provider will not hold when they deliver the care.
        """
        if isinstance(service_date, datetime):
            service_date = service_date.date()
        d = service_date.isoformat()
        row = self.con.execute(
            "SELECT 1 FROM licence WHERE provider_id=? AND state=? "
            "AND effective_on <= ? AND expires_on >= ? LIMIT 1",
            (provider_id, state, d, d)).fetchone()
        return row is not None

    def licence_gaps(self, provider_id, state, start, end):
        """Days in [start, end] where the licence is NOT valid. Used by the
        ops report that finds already-booked appointments a licence change
        has just invalidated."""
        gaps = []
        d = start
        while d <= end:
            if not self.is_licensed(provider_id, state, d):
                gaps.append(d.isoformat())
            d += timedelta(days=1)
        return gaps

    # -- availability ----------------------------------------------------
    def availability(self, provider_id, patient_id, visit_type_id,
                     day_local, now=None):
        """Candidate start times for one provider-local day, in UTC.

        The intersection is: working hours, minus exception days, minus
        existing bookings and their buffers, minus lead-time, AND the licence
        check for the patient's state on that date.
        """
        prov = self.con.execute(
            "SELECT home_tz FROM provider WHERE provider_id=?",
            (provider_id,)).fetchone()
        if not prov:
            raise SchedulingError(f"unknown provider {provider_id}")
        ptz = ZoneInfo(prov[0])
        pat = self.con.execute(
            "SELECT home_state, home_tz FROM patient WHERE patient_id=?",
            (patient_id,)).fetchone()
        if not pat:
            raise SchedulingError(f"unknown patient {patient_id}")
        patient_state, _patient_tz = pat
        vt = self.con.execute(
            "SELECT minutes, lead_time_hours, buffer_minutes FROM visit_type "
            "WHERE visit_type_id=?", (visit_type_id,)).fetchone()
        minutes, lead_hours, buffer_minutes = vt

        # LICENCE FIRST: no point computing slots the provider cannot legally use
        if not self.is_licensed(provider_id, patient_state, day_local):
            return []

        if self.con.execute(
                "SELECT 1 FROM exception_day WHERE provider_id=? AND day=?",
                (provider_id, day_local.isoformat())).fetchone():
            return []

        weekday = day_local.weekday()
        hours = self.con.execute(
            "SELECT start_local, end_local FROM working_hours "
            "WHERE provider_id=? AND weekday=?", (provider_id, weekday)).fetchall()
        if not hours:
            return []

        now = now or datetime.now(UTC)
        earliest = now + timedelta(hours=lead_hours)

        booked = self.con.execute(
            "SELECT starts_at_utc, ends_at_utc FROM appointment "
            "WHERE provider_id=? AND status IN (?,?,?)",
            (provider_id, *ACTIVE)).fetchall()
        booked = [(datetime.fromisoformat(a), datetime.fromisoformat(b))
                  for a, b in booked]

        slots = []
        for start_s, end_s in hours:
            sh, sm = map(int, start_s.split(":"))
            eh, em = map(int, end_s.split(":"))
            # Build the instant from LOCAL wall clock. This is where DST is
            # handled or lost: 2:30am on a spring-forward day does not exist.
            cursor = self._local_to_utc(day_local, sh, sm, ptz)
            day_end = self._local_to_utc(day_local, eh, em, ptz)
            if cursor is None or day_end is None:
                continue
            while cursor + timedelta(minutes=minutes) <= day_end:
                finish = cursor + timedelta(minutes=minutes)
                if cursor < earliest:
                    cursor = finish
                    continue
                clash = any(
                    cursor < b + timedelta(minutes=buffer_minutes)
                    and a - timedelta(minutes=buffer_minutes) < finish
                    for a, b in booked)
                if not clash:
                    slots.append(cursor)
                cursor = finish
        return slots

    @staticmethod
    def classify_local_time(day, hour, minute, tz):
        """Resolve a local wall clock against a timezone's DST transitions.

        A wall-clock time is one of three things, and a scheduler that assumes
        the first is wrong twice a year:

          ("ok", dt)                    -- occurs exactly once
          ("nonexistent", None)         -- SPRING FORWARD. 02:30 on the
                                           transition day never happens.
          ("ambiguous", (first, second))-- AUTUMN FALL-BACK. 01:30 happens
                                           TWICE, an hour apart, and they are
                                           genuinely different instants.

        Python constructs all three happily and hands back a plausible instant
        for the two broken cases, which is why they have to be detected rather
        than trusted. `fold` distinguishes the two occurrences of an ambiguous
        time: fold=0 is the first (still on daylight time), fold=1 the second.
        """
        naive = datetime(day.year, day.month, day.day, hour, minute)
        first = naive.replace(tzinfo=tz, fold=0)
        second = naive.replace(tzinfo=tz, fold=1)

        # Nonexistent: the wall clock does not survive a round trip through UTC
        round_trip = first.astimezone(UTC).astimezone(tz)
        if (round_trip.hour, round_trip.minute) != (hour, minute):
            return "nonexistent", None

        # Ambiguous: the two folds map to DIFFERENT instants
        if first.utcoffset() != second.utcoffset():
            return "ambiguous", (first.astimezone(UTC), second.astimezone(UTC))

        return "ok", first.astimezone(UTC)

    @staticmethod
    def _local_to_utc(day, hour, minute, tz):
        """Single UTC instant for a local wall clock, or None if it does not
        exist. For an AMBIGUOUS time this returns the FIRST occurrence; callers
        that need both must use `classify_local_time`."""
        kind, value = Scheduler.classify_local_time(day, hour, minute, tz)
        if kind == "nonexistent":
            return None
        if kind == "ambiguous":
            return value[0]
        return value

    @staticmethod
    def local_instants(day, hour, minute, tz):
        """Every real instant matching this local wall clock -- 0, 1, or 2.

        This is what availability generation actually needs. On a fall-back day
        the local calendar genuinely contains two 01:30s, and a provider whose
        hours span the transition genuinely has an extra hour of availability.
        Collapsing them to one silently loses a bookable slot; treating them as
        one slot double-books it.
        """
        kind, value = Scheduler.classify_local_time(day, hour, minute, tz)
        if kind == "nonexistent":
            return []
        if kind == "ambiguous":
            return [value[0], value[1]]
        return [value]

    @staticmethod
    def to_patient_time(utc_dt, patient_tz):
        """Render an instant in the PATIENT's zone. Availability shown in the
        provider's zone is a support ticket waiting to happen."""
        return utc_dt.astimezone(ZoneInfo(patient_tz))

    @staticmethod
    def describe_for_patient(utc_dt, patient_tz):
        """A human-facing string that is never ambiguous.

        Storing UTC makes the BOOKING unambiguous. It does nothing for the
        CONFIRMATION EMAIL: "Sunday 2 November, 1:30 AM" is two different
        appointments an hour apart on a fall-back date, and the patient has no
        way to tell which one they have.

        So the zone abbreviation is always included, and on an ambiguous local
        time the string is marked explicitly. This is the half of DST
        correctness that no amount of UTC discipline fixes, because the problem
        is in the rendering rather than in the storage.
        """
        local = utc_dt.astimezone(ZoneInfo(patient_tz))
        kind, _v = Scheduler.classify_local_time(
            local.date(), local.hour, local.minute, ZoneInfo(patient_tz))
        stamp = local.strftime("%a %d %b %Y, %I:%M %p ") + local.tzname()
        if kind == "ambiguous":
            return (f"{stamp} (this local time occurs twice tonight because "
                    f"the clocks go back -- {local.tzname()} is the one you want)")
        return stamp

    # -- booking ---------------------------------------------------------
    def book(self, provider_id, patient_id, visit_type_id, start_utc,
             con=None, actor="patient"):
        """Book a slot. Raises SlotTaken or LicenceViolation.

        The check and the write happen inside ONE `BEGIN IMMEDIATE`
        transaction, which takes the write lock up front. Without it, two
        threads both read 'free' and both insert -- the exact race the
        Postgres exclusion constraint eliminates structurally.
        """
        con = con or self.con
        vt = con.execute(
            "SELECT minutes, buffer_minutes FROM visit_type WHERE visit_type_id=?",
            (visit_type_id,)).fetchone()
        minutes, buffer_minutes = vt
        end_utc = start_utc + timedelta(minutes=minutes)

        state = con.execute(
            "SELECT home_state FROM patient WHERE patient_id=?",
            (patient_id,)).fetchone()[0]
        if not self.is_licensed(provider_id, state, start_utc.date()):
            raise LicenceViolation(
                f"{provider_id} is not licensed in {state} on "
                f"{start_utc.date().isoformat()} (the DATE OF SERVICE)")

        appt_id = "appt-" + secrets.token_hex(6)
        try:
            con.execute("BEGIN IMMEDIATE")
            clash = con.execute(
                "SELECT appointment_id FROM appointment WHERE provider_id=? "
                "AND status IN (?,?,?) AND starts_at_utc < ? AND ends_at_utc > ?",
                (provider_id, *ACTIVE,
                 _iso(end_utc + timedelta(minutes=buffer_minutes)),
                 _iso(start_utc - timedelta(minutes=buffer_minutes)))).fetchone()
            if clash:
                con.execute("ROLLBACK")
                raise SlotTaken(f"provider {provider_id} is already booked "
                                f"overlapping {_iso(start_utc)}")
            # a patient cannot be in two places at once either
            patient_clash = con.execute(
                "SELECT appointment_id FROM appointment WHERE patient_id=? "
                "AND status IN (?,?,?) AND starts_at_utc < ? AND ends_at_utc > ?",
                (patient_id, *ACTIVE, _iso(end_utc), _iso(start_utc))).fetchone()
            if patient_clash:
                con.execute("ROLLBACK")
                raise SlotTaken(f"patient {patient_id} already has an "
                                f"overlapping appointment")
            con.execute(
                "INSERT INTO appointment VALUES (?,?,?,?,?,?,?,?,?)",
                (appt_id, provider_id, patient_id, visit_type_id,
                 _iso(start_utc), _iso(end_utc), "confirmed", state,
                 datetime.now(UTC).isoformat()))
            con.execute(
                "INSERT INTO appointment_audit "
                "(appointment_id, at, actor, from_status, to_status, note) "
                "VALUES (?,?,?,?,?,?)",
                (appt_id, datetime.now(UTC).isoformat(), actor, None,
                 "confirmed", "booked"))
            con.execute("COMMIT")
        except sqlite3.OperationalError:
            try:
                con.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise SlotTaken("could not acquire the write lock")
        return appt_id

    def transition(self, appointment_id, to_status, actor="system", note=""):
        row = self.con.execute(
            "SELECT status FROM appointment WHERE appointment_id=?",
            (appointment_id,)).fetchone()
        if not row:
            raise SchedulingError(f"unknown appointment {appointment_id}")
        current = row[0]
        if to_status not in ALLOWED_TRANSITIONS.get(current, set()):
            raise InvalidTransition(
                f"{current} -> {to_status} is not an allowed transition")
        self.con.execute(
            "UPDATE appointment SET status=? WHERE appointment_id=?",
            (to_status, appointment_id))
        self.con.execute(
            "INSERT INTO appointment_audit "
            "(appointment_id, at, actor, from_status, to_status, note) "
            "VALUES (?,?,?,?,?,?)",
            (appointment_id, datetime.now(UTC).isoformat(), actor, current,
             to_status, note))
        return to_status

    def appointment(self, appointment_id):
        return self.con.execute(
            "SELECT appointment_id, provider_id, patient_id, visit_type_id, "
            "starts_at_utc, ends_at_utc, status, service_state FROM appointment "
            "WHERE appointment_id=?", (appointment_id,)).fetchone()

    def active_count(self, provider_id=None):
        if provider_id:
            return self.con.execute(
                "SELECT COUNT(*) FROM appointment WHERE provider_id=? "
                "AND status IN (?,?,?)", (provider_id, *ACTIVE)).fetchone()[0]
        return self.con.execute(
            "SELECT COUNT(*) FROM appointment WHERE status IN (?,?,?)",
            ACTIVE).fetchone()[0]

    def audit_trail(self, appointment_id):
        return self.con.execute(
            "SELECT at, actor, from_status, to_status, note "
            "FROM appointment_audit WHERE appointment_id=? ORDER BY seq",
            (appointment_id,)).fetchall()
