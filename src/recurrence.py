"""Recurring appointment series, and the DST bug that only series have.

WHY THIS FILE IS THE ONE THAT MATTERS
--------------------------------------
The README said it twice: *"there is no RRULE model, so 'move the whole series'
-- where DST bugs really live -- is untested because series do not exist."*
Single appointments across a DST transition were already handled. Series were
not, and series are where the interesting failure is:

    A WEEKLY SERIES STORED AS UTC INSTANTS SILENTLY MOVES BY AN HOUR WHEN THE
    CLOCKS CHANGE.

Store "Tuesdays at 09:00" as a UTC instant plus a 7-day interval and every
occurrence after the transition lands at 08:00 or 10:00 local. Nothing errors.
The patient, who was told 9am, misses the appointment -- or arrives an hour
early and the provider is with someone else. For a weekly therapy series that
is not a scheduling inconvenience; it is a missed session in a course of care.

The fix is to store the RULE IN LOCAL WALL TIME and resolve each occurrence
against the timezone independently:

    anchor        2024-10-01, 09:00, America/New_York
    rule          FREQ=WEEKLY;BYDAY=TU;COUNT=8
    occurrence 4  2024-10-22 09:00 EDT -> 13:00 UTC
    occurrence 6  2024-11-05 09:00 EST -> 14:00 UTC   <- different UTC offset,
                                                          same wall clock

The UTC instants are NOT evenly spaced, and that is the correct behaviour. A
series whose UTC instants are evenly spaced across a transition is a series
whose local times are wrong.

TWO WAYS TO MOVE A SERIES, AND WHERE THEY ACTUALLY DISAGREE
-----------------------------------------------------------
    WALL-CLOCK MOVE   the RULE changes: 09:00 becomes 09:30 local. What a
                      patient means by "move my appointment half an hour later".

    ABSOLUTE MOVE     every stored INSTANT shifts by that much elapsed time.
                      What you want when the constraint is a resource booked in
                      real time.

I first wrote that these diverge "once a transition is inside the range". That
is wrong, and measuring it said so. Once each occurrence is resolved
independently against the timezone, adding a constant to a correctly-resolved
instant preserves its local time, so for the 8-week Tuesday-09:00 series above
the two modes agree on every occurrence -- transition included.

They diverge in a narrower and more interesting place: WHEN THE OCCURRENCE
ITSELF SITS IN AN AMBIGUOUS OR NONEXISTENT LOCAL WINDOW. A series at 01:30 on
US fall-back Sunday, moved 60 minutes later:

    wall clock   01:30 -> 02:30 EST
    absolute     01:30 EDT + 60 min of elapsed time -> 01:30 EST
                 ...the SECOND 01:30, an hour later, same wall clock

    2024-10-27   no disagreement
    2024-11-03   wall_clock 02:30 EST   absolute 01:30 EST   differ by 60 min
    2024-11-10   no disagreement

One occurrence out of three, and only the one on the transition day. That is a
much harder bug to notice than "the whole series shifted", which is exactly why
`move()` refuses to pick a default and `compare_moves()` exists to answer "does
this move disagree with itself, and where".

WHAT THIS IS NOT
----------------
A very small subset of RFC 5545: FREQ=DAILY|WEEKLY, INTERVAL, BYDAY, COUNT and
UNTIL. No MONTHLY or YEARLY, no BYSETPOS, no BYMONTHDAY, no EXDATE/RDATE, no
nested rules, no VTIMEZONE serialisation. `dateutil.rrule` and `icalendar` do
this properly. What is here is the part that touches the timezone question,
which is the part the schedulers get wrong.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
WEEKDAY_NAMES = {v: k for k, v in WEEKDAYS.items()}
UTC = ZoneInfo("UTC")


class RecurrenceError(ValueError):
    pass


class AmbiguousMove(RecurrenceError):
    """Raised when a move spans a DST transition and the caller did not say
    which of the two readings they meant."""


def parse_rrule(text):
    """A small RFC 5545 RRULE subset. Unknown parts are REFUSED, not ignored.

    Ignoring an unrecognised part is how a series silently recurs on the wrong
    days: a caller writes `BYMONTHDAY=15`, the parser drops it, and the rule
    quietly means something else that still runs.
    """
    parts = {}
    for chunk in text.replace("RRULE:", "").split(";"):
        if not chunk:
            continue
        if "=" not in chunk:
            raise RecurrenceError(f"malformed RRULE part {chunk!r}")
        k, v = chunk.split("=", 1)
        parts[k.upper()] = v.upper()

    known = {"FREQ", "INTERVAL", "BYDAY", "COUNT", "UNTIL", "EXDATE"}
    unknown = set(parts) - known
    if unknown:
        raise RecurrenceError(
            f"unsupported RRULE part(s) {sorted(unknown)}. Refused rather than "
            f"ignored: a dropped part makes the rule mean something else that "
            f"still runs. Supported: {sorted(known)}")

    freq = parts.get("FREQ")
    if freq not in ("DAILY", "WEEKLY"):
        raise RecurrenceError(f"FREQ must be DAILY or WEEKLY, got {freq!r}")
    if "COUNT" in parts and "UNTIL" in parts:
        raise RecurrenceError("COUNT and UNTIL are mutually exclusive (RFC 5545)")

    rule = {"freq": freq, "interval": int(parts.get("INTERVAL", 1))}
    if rule["interval"] < 1:
        raise RecurrenceError("INTERVAL must be >= 1")
    if "BYDAY" in parts:
        days = [d.strip() for d in parts["BYDAY"].split(",") if d.strip()]
        bad = [d for d in days if d not in WEEKDAYS]
        if bad:
            raise RecurrenceError(f"unknown BYDAY value(s) {bad}")
        rule["byday"] = [WEEKDAYS[d] for d in days]
    if "COUNT" in parts:
        rule["count"] = int(parts["COUNT"])
    if "EXDATE" in parts:
        # EXDATE removes specific occurrences WITHOUT changing the rule. That
        # distinction is the whole point: "cancel just the 12 November one" is
        # the most common real request, and re-writing the rule to route around
        # one date changes what every OTHER occurrence means.
        rule["exdate"] = sorted({
            date.fromisoformat(d[:4] + "-" + d[4:6] + "-" + d[6:8]
                               if "-" not in d else d[:10])
            for d in parts["EXDATE"].split(",") if d.strip()})
    if "UNTIL" in parts:
        rule["until"] = date.fromisoformat(
            parts["UNTIL"][:4] + "-" + parts["UNTIL"][4:6] + "-"
            + parts["UNTIL"][6:8] if "-" not in parts["UNTIL"]
            else parts["UNTIL"][:10])
    return rule


def format_rrule(rule):
    out = [f"FREQ={rule['freq']}"]
    if rule.get("interval", 1) != 1:
        out.append(f"INTERVAL={rule['interval']}")
    if rule.get("byday"):
        out.append("BYDAY=" + ",".join(WEEKDAY_NAMES[d]
                                       for d in sorted(rule["byday"])))
    if rule.get("count"):
        out.append(f"COUNT={rule['count']}")
    if rule.get("until"):
        out.append("UNTIL=" + rule["until"].isoformat().replace("-", ""))
    if rule.get("exdate"):
        out.append("EXDATE=" + ",".join(d.isoformat().replace("-", "")
                                        for d in rule["exdate"]))
    return "RRULE:" + ";".join(out)


class Series:
    """A recurring appointment, stored as a LOCAL wall clock plus a rule.

    The storage decision is the whole design. `hour`/`minute`/`tz` and a rule,
    never a UTC instant and an interval -- because the second representation
    cannot express "Tuesdays at 9am" across a DST transition, and will silently
    express something else instead.
    """

    def __init__(self, series_id, provider_id, patient_id, visit_type_id,
                 anchor_date, hour, minute, tz, rrule, duration_minutes=30):
        self.series_id = series_id
        self.provider_id = provider_id
        self.patient_id = patient_id
        self.visit_type_id = visit_type_id
        self.anchor_date = (date.fromisoformat(anchor_date)
                            if isinstance(anchor_date, str) else anchor_date)
        self.hour, self.minute = hour, minute
        self.tz = tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)
        self.rule = parse_rrule(rrule) if isinstance(rrule, str) else dict(rrule)
        self.duration_minutes = duration_minutes

    # -- expansion --------------------------------------------------------
    def local_dates(self, horizon_days=730):
        """The calendar dates the rule lands on, in local terms."""
        r = self.rule
        out, seen = [], 0
        d = self.anchor_date
        limit = self.anchor_date + timedelta(days=horizon_days)
        step = timedelta(days=1) if r["freq"] == "DAILY" else timedelta(days=1)
        byday = r.get("byday")

        if r["freq"] == "WEEKLY" and not byday:
            byday = [self.anchor_date.weekday()]

        week0 = self.anchor_date - timedelta(days=self.anchor_date.weekday())
        while d <= limit:
            take = False
            if r["freq"] == "DAILY":
                take = ((d - self.anchor_date).days % r["interval"] == 0)
            else:
                weeks = ((d - timedelta(days=d.weekday())) - week0).days // 7
                take = (d.weekday() in byday and weeks % r["interval"] == 0
                        and d >= self.anchor_date)
            if take:
                if r.get("until") and d > r["until"]:
                    break
                # COUNT IS CONSUMED BEFORE EXDATE IS APPLIED (RFC 5545 3.8.5.1).
                # An excluded occurrence still counts toward COUNT, so
                # cancelling one appointment SHORTENS the series rather than
                # sliding a replacement onto the end. Getting this backwards
                # silently adds a session nobody scheduled -- and the patient
                # who cancelled one week would be booked an extra one.
                seen += 1
                if d not in (r.get("exdate") or ()):
                    out.append(d)
                if r.get("count") and seen >= r["count"]:
                    break
            d += step
        return out

    def occurrences(self, scheduler_cls, horizon_days=730):
        """Resolve each local date to real UTC instants, DST included.

        Each occurrence is resolved INDEPENDENTLY against the timezone. That
        independence is the fix: the rule says 09:00 local, and 09:00 local is a
        different UTC instant either side of a transition.

        Uses the same `classify_local_time` / `local_instants` the single-
        appointment path uses, so a series and a one-off cannot disagree about
        what a wall clock means -- and a nonexistent occurrence (spring forward)
        is reported rather than silently skipped or silently shifted.
        """
        out = []
        for d in self.local_dates(horizon_days):
            kind, _value = scheduler_cls.classify_local_time(
                d, self.hour, self.minute, self.tz)
            instants = scheduler_cls.local_instants(d, self.hour, self.minute,
                                                    self.tz)
            utc = instants[0].astimezone(UTC) if instants else None
            # THE OFFSET MUST BE READ IN THE SERIES' OWN ZONE. local_instants
            # returns UTC datetimes, so calling .utcoffset() on one gives 0.0
            # every time -- which reads as "the offset never changes" and is
            # exactly the reassuring wrong answer this file exists to avoid.
            local = utc.astimezone(self.tz) if utc else None
            out.append({
                "local_date": d.isoformat(),
                "local_time": f"{self.hour:02d}:{self.minute:02d}",
                "kind": kind,
                "instants": instants,
                "utc": utc,
                "tzname": local.tzname() if local else None,
                "utc_offset_hours": (local.utcoffset().total_seconds() / 3600
                                     if local else None),
            })
        return out

    def naive_utc_occurrences(self, scheduler_cls, horizon_days=730):
        """THE BUG, implemented on purpose so it can be measured.

        This is the representation a scheduler reaches for first: resolve the
        anchor to a UTC instant once, then add a fixed interval. It is simpler,
        it needs no timezone library at expansion time, and it is wrong.

        Compare against `occurrences()`. Before the transition the two agree
        exactly; after it they differ by an hour, and the naive one is the one
        whose LOCAL time has moved. Nothing errors, no test fails, and the
        patient told "Tuesdays at 9" misses a session.
        """
        dates = self.local_dates(horizon_days)
        if not dates:
            return []
        first = scheduler_cls.local_instants(dates[0], self.hour, self.minute,
                                             self.tz)
        if not first:
            return []
        anchor = first[0].astimezone(UTC)
        out = []
        for d in dates:
            utc = anchor + timedelta(days=(d - dates[0]).days)
            local = utc.astimezone(self.tz)
            out.append({"local_date": d.isoformat(), "utc": utc,
                        "naive_local_time": local.strftime("%H:%M"),
                        "tzname": local.tzname()})
        return out

    # -- moving ---------------------------------------------------------
    # -- moving -----------------------------------------------------------
    def move(self, minutes, *, mode, scheduler_cls, horizon_days=730):
        """Move the series. `mode` is 'wall_clock' or 'absolute'.

        REFUSES TO GUESS, because the two readings differ on exactly the
        occurrences hardest to notice -- see `compare_moves` and the module
        docstring: not the whole series, but the one occurrence sitting in an
        ambiguous local window.

          wall_clock  the RULE moves. 09:00 becomes 09:30 local for every
                      occurrence. This is what a patient means by "move my
                      Tuesday appointment half an hour later", and it keeps the
                      series consistent in the only frame the patient uses.

          absolute    every stored INSTANT moves by that much elapsed time, so
                      occurrences after a transition land at a different local
                      time from those before it. Right when the constraint is a
                      resource booked in real time; wrong for a patient-facing
                      appointment.
        """
        if mode not in ("wall_clock", "absolute"):
            raise AmbiguousMove(
                "mode must be 'wall_clock' or 'absolute'. These give different "
                "answers once a DST transition falls inside the series, and "
                "guessing is how a therapy series ends up an hour out for half "
                "its occurrences.")

        if mode == "wall_clock":
            total = self.hour * 60 + self.minute + minutes
            if not (0 <= total < 24 * 60):
                raise RecurrenceError(
                    "a wall-clock move that crosses midnight would change "
                    "which DAY each occurrence falls on, and therefore which "
                    "BYDAY the rule means; refused rather than silently "
                    "re-dating the series")
            moved = Series(self.series_id, self.provider_id, self.patient_id,
                           self.visit_type_id, self.anchor_date,
                           total // 60, total % 60, self.tz, dict(self.rule),
                           self.duration_minutes)
            return moved, [o["utc"] for o in moved.occurrences(scheduler_cls,
                                                               horizon_days)]

        shifted = []
        for o in self.occurrences(scheduler_cls, horizon_days):
            shifted.append(o["utc"] + timedelta(minutes=minutes)
                           if o["utc"] else None)
        return self, shifted







def compare_moves(series, minutes, scheduler_cls, horizon_days=730):
    """Where do the two readings of 'move the series' disagree?

    Returns the occurrences whose LOCAL time differs between the two modes.

    Usually empty, and that is the finding. Correct storage makes the two modes
    agree for ordinary shifts -- adding a constant to a correctly-resolved
    instant preserves its local time. The disagreement survives only where the
    occurrence itself falls in an ambiguous or nonexistent local window, which
    is one occurrence in a series rather than half of one, and correspondingly
    harder to spot.
    """
    wall, wall_utc = series.move(minutes, mode="wall_clock",
                                 scheduler_cls=scheduler_cls,
                                 horizon_days=horizon_days)
    _same, abs_utc = series.move(minutes, mode="absolute",
                                 scheduler_cls=scheduler_cls,
                                 horizon_days=horizon_days)
    rows = []
    for w, a in zip(wall_utc, abs_utc):
        if w is None or a is None:
            continue
        wl = w.astimezone(series.tz)
        al = a.astimezone(series.tz)
        if wl.strftime("%H:%M") != al.strftime("%H:%M"):
            rows.append({
                "date": wl.date().isoformat(),
                "wall_clock_local": wl.strftime("%H:%M %Z"),
                "absolute_local": al.strftime("%H:%M %Z"),
                "differ_by_minutes": int((a - w).total_seconds() // 60),
            })
    return rows


def storage_drift(series, scheduler_cls, horizon_days=730):
    """Where does the naive UTC+interval representation drift?

    Returns one row per occurrence whose local time under the naive scheme is
    not the local time the rule asks for. An empty result means no transition
    fell inside the range -- which is why this is measured rather than asserted.
    """
    correct = series.occurrences(scheduler_cls, horizon_days)
    naive = series.naive_utc_occurrences(scheduler_cls, horizon_days)
    want = f"{series.hour:02d}:{series.minute:02d}"
    rows = []
    for c, n in zip(correct, naive):
        if n["naive_local_time"] != want:
            rows.append({
                "date": c["local_date"],
                "intended_local": f"{want} ({c['tzname']})",
                "naive_local": f"{n['naive_local_time']} ({n['tzname']})",
                "drift_minutes": int((n["utc"] - c["utc"]).total_seconds() // 60)
                if c["utc"] else None,
            })
    return rows


def exclude(series, when):
    """Cancel ONE occurrence without touching the rule.

    Returns a new Series. The rule is unchanged and the excluded date is
    recorded as an EXDATE, so the series still reads "Tuesdays at 9" and the
    cancellation is visible as a cancellation rather than as a different
    schedule.

    The alternative -- rewriting the rule to route around one date -- changes
    what every other occurrence means, and is unrecoverable: nothing in the new
    rule records that a cancellation ever happened.
    """
    when = date.fromisoformat(when) if isinstance(when, str) else when
    rule = dict(series.rule)
    rule["exdate"] = sorted(set(rule.get("exdate", ())) | {when})
    return Series(series.series_id, series.provider_id, series.patient_id,
                  series.visit_type_id, series.anchor_date, series.hour,
                  series.minute, series.tz, rule, series.duration_minutes)


# ---------------------------------------------------------------------------
# booking a series
# ---------------------------------------------------------------------------

def save_series(scheduler, series, con=None, status="active"):
    """Persist the RULE, so a restart does not orphan the appointments.

    THE GAP THIS CLOSES. `book_series` wrote appointments and kept the Series
    in process memory. The appointments survive a restart; the rule that
    explains them does not. A cancellation then has nothing to attach an
    EXDATE to, and the only remaining move is to DELETE an appointment -- which
    loses the fact that the series ever included that date, and leaves the
    remaining rows looking like a series that always skipped it.

    Stored as RRULE TEXT, not as expanded occurrences, for the same reason
    `Series` stores wall clock plus a rule: expansions cannot survive a DST
    change and cannot be edited without rewriting every row.
    """
    # scheduler.con, NOT scheduler.connect(). On the default ":memory:" path
    # every connect() opens a SEPARATE, EMPTY database -- so a fresh connection
    # here would write the rule into a database nothing else can see, and
    # reading it back would report the series does not exist.
    own = False
    con = con or scheduler.con
    try:
        con.execute(
            "INSERT OR REPLACE INTO series (series_id, provider_id, "
            "patient_id, visit_type_id, anchor_date, hour, minute, tz, rrule, "
            "duration_minutes, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (series.series_id, series.provider_id, series.patient_id,
             series.visit_type_id, series.anchor_date.isoformat(),
             series.hour, series.minute, str(series.tz),
             format_rrule(series.rule), series.duration_minutes, status,
             datetime.now(UTC).isoformat()))
        con.commit()
    finally:
        if own:
            con.close()
    return series.series_id


def load_series(scheduler, series_id, con=None):
    """Rebuild a Series from storage, or None. The rule comes back intact."""
    # scheduler.con, NOT scheduler.connect(). On the default ":memory:" path
    # every connect() opens a SEPARATE, EMPTY database -- so a fresh connection
    # here would write the rule into a database nothing else can see, and
    # reading it back would report the series does not exist.
    own = False
    con = con or scheduler.con
    try:
        row = con.execute(
            "SELECT provider_id, patient_id, visit_type_id, anchor_date, "
            "hour, minute, tz, rrule, duration_minutes, status "
            "FROM series WHERE series_id = ?", (series_id,)).fetchone()
    finally:
        if own:
            con.close()
    if row is None:
        return None
    (provider, patient, visit_type, anchor, hour, minute, tz, rrule,
     duration, _status) = row
    return Series(series_id, provider, patient, visit_type, anchor, hour,
                  minute, tz, rrule, duration_minutes=duration)


def list_series(scheduler, patient_id=None, con=None):
    # scheduler.con, NOT scheduler.connect(). On the default ":memory:" path
    # every connect() opens a SEPARATE, EMPTY database -- so a fresh connection
    # here would write the rule into a database nothing else can see, and
    # reading it back would report the series does not exist.
    own = False
    con = con or scheduler.con
    try:
        if patient_id:
            rows = con.execute(
                "SELECT series_id FROM series WHERE patient_id = ? "
                "ORDER BY series_id", (patient_id,)).fetchall()
        else:
            rows = con.execute(
                "SELECT series_id FROM series ORDER BY series_id").fetchall()
    finally:
        if own:
            con.close()
    return [r[0] for r in rows]


def cancel_occurrence(scheduler, series_id, when, con=None, actor="patient"):
    """Cancel ONE occurrence of a PERSISTED series: EXDATE plus appointment.

    This is the operation the gap list said was impossible after a restart, and
    it has to do BOTH halves or it is worse than neither:

      * add the EXDATE to the STORED rule, so the series itself records that
        this date was cancelled rather than silently never existing
      * cancel the APPOINTMENT, so the slot is actually free

    Doing only the first leaves a booked appointment for a date the rule now
    excludes. Doing only the second is the old behaviour -- the appointment
    disappears and the rule still claims the date, so any re-expansion books it
    straight back.
    """
    # scheduler.con, NOT scheduler.connect(). On the default ":memory:" path
    # every connect() opens a SEPARATE, EMPTY database -- so a fresh connection
    # here would write the rule into a database nothing else can see, and
    # reading it back would report the series does not exist.
    own = False
    con = con or scheduler.con
    try:
        series = load_series(scheduler, series_id, con=con)
        if series is None:
            raise RecurrenceError("no persisted series %r" % series_id)

        updated = exclude(series, when)
        con.execute("UPDATE series SET rrule = ? WHERE series_id = ?",
                    (format_rrule(updated.rule), series_id))

        target = when if isinstance(when, date) else date.fromisoformat(
            str(when)[:10])
        cancelled = []
        rows = con.execute(
            "SELECT appointment_id, starts_at_utc FROM appointment "
            "WHERE series_id = ? AND status = 'confirmed'",
            (series_id,)).fetchall()
        for appt_id, starts in rows:
            local = datetime.fromisoformat(starts).astimezone(series.tz)
            if local.date() == target:
                con.execute(
                    "UPDATE appointment SET status = 'cancelled' "
                    "WHERE appointment_id = ?", (appt_id,))
                con.execute(
                    "INSERT INTO appointment_audit (appointment_id, at, "
                    "actor, from_status, to_status, note) VALUES (?,?,?,?,?,?)",
                    (appt_id, datetime.now(UTC).isoformat(), actor,
                     "confirmed", "cancelled",
                     "series %s EXDATE %s" % (series_id, target.isoformat())))
                cancelled.append(appt_id)
        con.commit()
    finally:
        if own:
            con.close()
    return {"series_id": series_id, "excluded": target.isoformat(),
            "appointments_cancelled": cancelled,
            "rrule": format_rrule(updated.rule)}


def book_series(series, scheduler, scheduler_cls, *, policy="best-effort",
                actor="patient", con_factory=None, persist=True):
    """Book every occurrence. Returns what happened to each.

    THE PARTIAL-FAILURE STORY, which is the named gap: "book 8 occurrences and
    the 5th clashes". Three policies, because the right answer depends on what
    the series IS and no default is right for both:

      BEST-EFFORT   book what is free, report what is not. Correct for a
                    therapy series -- seven sessions plus one to rearrange is
                    better than none, and the patient would rather have the
                    seven.

      ALL-OR-NOTHING  roll back everything if any occurrence clashes. Correct
                    when the series only makes sense complete: a titration
                    schedule with a gap in the middle is not a shorter
                    titration, it is a different and possibly unsafe one.

      STOP-AT-FIRST-CLASH  book up to the clash and stop. Correct when later
                    occurrences depend on earlier ones and booking past a gap
                    would schedule sessions that assume something that did not
                    happen.

    A NONEXISTENT OCCURRENCE IS NOT A CLASH. Spring-forward can delete an
    occurrence's wall-clock time entirely, and that is a scheduling fact rather
    than a conflict -- it is reported separately so it is not confused with a
    double-booking, and so nobody "fixes" it by moving the whole series.
    """
    if policy not in ("best-effort", "all-or-nothing", "stop-at-first-clash"):
        raise RecurrenceError(
            f"unknown policy {policy!r}. The right answer depends on what the "
            f"series IS -- a therapy course tolerates a gap, a titration "
            f"schedule does not -- so there is no safe default.")

    from scheduling import LicenceViolation, SchedulingError, SlotTaken

    results, booked = [], []
    for occ in series.occurrences(scheduler_cls):
        if occ["kind"] == "nonexistent":
            results.append({"date": occ["local_date"], "status": "nonexistent",
                            "detail": ("this wall-clock time does not exist on "
                                       "this date -- the clocks went forward. "
                                       "Not a clash, and not fixable by moving "
                                       "the series.")})
            continue
        con = con_factory() if con_factory else None
        try:
            appt = scheduler.book(series.provider_id, series.patient_id,
                                  series.visit_type_id, occ["utc"],
                                  con=con, actor=actor,
                                  series_id=series.series_id)
            booked.append(appt)
            results.append({"date": occ["local_date"], "status": "booked",
                            "appointment_id": appt,
                            "ambiguous": occ["kind"] == "ambiguous"})
        except SlotTaken as exc:
            results.append({"date": occ["local_date"], "status": "clash",
                            "detail": str(exc)})
            if policy == "stop-at-first-clash":
                results.append({"date": occ["local_date"], "status": "stopped",
                                "detail": ("later occurrences not attempted: "
                                           "this policy assumes they depend on "
                                           "this one")})
                break
            if policy == "all-or-nothing":
                break
        except (LicenceViolation, SchedulingError) as exc:
            results.append({"date": occ["local_date"], "status": "refused",
                            "detail": str(exc)})
            if policy in ("all-or-nothing", "stop-at-first-clash"):
                break
        finally:
            if con is not None:
                con.close()

    failed = [r for r in results if r["status"] in ("clash", "refused")]
    rolled_back = []
    if policy == "all-or-nothing" and failed:
        for appt in booked:
            try:
                scheduler.transition(appt, "cancelled", actor="system",
                                     note="series rollback: all-or-nothing")
                rolled_back.append(appt)
            except Exception:                          # noqa: BLE001
                pass

    if persist and booked and not rolled_back:
        # AFTER booking, and only if something WAS booked. A stored rule with
        # no appointments is a claim the data cannot support -- and an
        # all-or-nothing rollback must not leave a series behind describing
        # appointments that were undone.
        #
        # No try/except here. An earlier draft swallowed the exception on the
        # grounds that "the appointments are already correct", which is exactly
        # how a series silently fails to persist and the gap this closes
        # reopens without anyone noticing. If the rule cannot be stored, the
        # caller needs to know.
        save_series(scheduler, series)

    return {
        "policy": policy,
        "results": results,
        "n_booked": 0 if rolled_back else len(booked),
        "n_failed": len(failed),
        "n_nonexistent": sum(1 for r in results
                             if r["status"] == "nonexistent"),
        "rolled_back": rolled_back,
        "outcome": ("rolled back -- the series only makes sense complete"
                    if rolled_back else
                    "nothing booked" if not booked else
                    f"{len(booked)} of {len(results)} occurrence(s) booked"),
    }
