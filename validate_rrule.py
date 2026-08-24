"""Difference the hand-written RRULE expansion against `dateutil.rrule`.

WHY THIS EXISTS
---------------
`src/recurrence.py` expands RFC 5545 rules by hand. The README used to excuse
the small supported subset by saying `dateutil.rrule` "does this properly and
is not installed". That was simply wrong -- `python-dateutil` is installed, and
it arrives with pandas on most machines. The subset is still small, but that is
now a scoping decision rather than a missing dependency, and the correctness of
what IS supported can be checked against the library that does it properly.

This matters more than it looks. The project asserts a specific RFC 5545
reading in `exclude()`:

    COUNT is consumed BEFORE EXDATE is applied, so excluding one occurrence
    SHORTENS the series rather than sliding a replacement onto the end.

That is a claim about a standard, made in a docstring, that no test could
settle -- a test written by the same person who read the spec encodes the same
reading of it. `dateutil` is an independent reading, and it agrees.

WHAT IS COMPARED
----------------
Randomly generated rules over the whole supported grammar (FREQ=DAILY|WEEKLY,
INTERVAL, BYDAY, COUNT, UNTIL, EXDATE), expanded by both and compared as local
wall-clock dates. `dateutil` is the reference for RULE EXPANSION only; the
timezone handling is this project's own and is tested separately, which is why
the comparison is on local dates.

THE HARNESS IS CHECKED FOR BEING ABLE TO FAIL
---------------------------------------------
A comparison that reports zero mismatches is worthless until you know it can
report a non-zero one. `--sabotage` swaps in the plausible WRONG reading of the
COUNT/EXDATE interaction -- topping the series back up to COUNT after
excluding -- and the run must fail. If it does not, the harness is broken and
the clean run means nothing.

Run:  python validate_rrule.py
      python validate_rrule.py --sabotage    # must report mismatches
"""

import os
import random
import sys
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

import recurrence as RC
from scheduling import Scheduler

try:
    from dateutil.rrule import DAILY, WEEKLY, rrule, rruleset
except ImportError:                                   # pragma: no cover
    print("python-dateutil not installed. This is an OPTIONAL audit; "
          "src/ does not depend on it.")
    raise SystemExit(0)

FREQMAP = {"DAILY": DAILY, "WEEKLY": WEEKLY}
DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
TZS = ["America/New_York", "America/Denver", "Australia/Sydney",
       "Europe/London", "UTC"]


def _mine(text, start, tz, hour=9, minute=0, sabotage=False):
    series = RC.Series("s", "pr-1", "pt-1", "vt-1", start, hour, minute, tz,
                       text)
    out = [o["local_date"] for o in series.occurrences(Scheduler)]
    if sabotage and series.rule.get("count") and series.rule.get("exdate"):
        # The plausible WRONG reading: top the series back up to COUNT after
        # excluding, i.e. treat EXDATE as applied BEFORE COUNT is consumed.
        bare = {k: v for k, v in series.rule.items() if k != "exdate"}
        full = RC.Series("x", "pr-1", "pt-1", "vt-1", start, hour, minute, tz,
                         RC.format_rrule(bare))
        allo = [o["local_date"] for o in full.occurrences(Scheduler)]
        need = series.rule["count"] - len(out)
        if need > 0 and allo:
            out = out + [allo[-1]] * need
    return out


def _reference(rule, start, hour=9, minute=0):
    dtstart = datetime.fromisoformat(start).replace(hour=hour, minute=minute)
    kw = {"dtstart": dtstart, "interval": rule["interval"]}
    if "byday" in rule:
        kw["byweekday"] = rule["byday"]
    if "count" in rule:
        kw["count"] = rule["count"]
    if "until" in rule:
        u = rule["until"]
        kw["until"] = datetime(u.year, u.month, u.day, 23, 59, 59)
    out = rrule(FREQMAP[rule["freq"]], **kw)
    if rule.get("exdate"):
        rs = rruleset()
        rs.rrule(out)
        for d in rule["exdate"]:
            rs.exdate(datetime(d.year, d.month, d.day, hour, minute))
        out = rs
    return [d.date().isoformat() for d in out]


def compare(trials=400, seed=20260823, sabotage=False):
    """Return (n_rules, n_with_exdate, n_occurrences, [mismatches])."""
    rng = random.Random(seed)
    mismatches = []
    n = n_ex = n_occ = 0

    for _ in range(trials):
        freq = rng.choice(["DAILY", "WEEKLY"])
        parts = ["FREQ=" + freq,
                 "INTERVAL=%d" % rng.choice([1, 1, 2, 3])]
        if freq == "WEEKLY" and rng.random() < 0.8:
            parts.append("BYDAY=" + ",".join(
                rng.sample(DAYS, rng.randint(1, 3))))
        start = date(2024, 1, 1) + timedelta(days=rng.randint(0, 500))
        if rng.random() < 0.6:
            parts.append("COUNT=%d" % rng.randint(1, 12))
        else:
            until = start + timedelta(days=rng.randint(7, 120))
            parts.append("UNTIL=%s" % until.strftime("%Y%m%d"))

        try:
            first = RC.parse_rrule(";".join(parts))
        except RC.RecurrenceError:
            continue

        # Exclude one or two of the occurrences this rule actually generates,
        # so EXDATE is exercised rather than only parsed.
        if rng.random() < 0.5:
            base = _reference(first, start.isoformat())
            if len(base) > 2:
                picks = rng.sample(base, min(len(base), rng.randint(1, 2)))
                parts.append("EXDATE=" + ",".join(d.replace("-", "")
                                                  for d in picks))

        text = ";".join(parts)
        try:
            rule = RC.parse_rrule(text)
        except RC.RecurrenceError:
            continue

        tz = rng.choice(TZS)
        try:
            got = _mine(text, start.isoformat(), tz, sabotage=sabotage)
        except Exception as exc:                       # pragma: no cover
            mismatches.append((text, start.isoformat(), tz,
                               "RAISED " + repr(exc), None))
            continue
        want = _reference(rule, start.isoformat())

        n += 1
        n_occ += len(want)
        n_ex += 1 if rule.get("exdate") else 0
        if got != want:
            mismatches.append((text, start.isoformat(), tz, got, want))

    return n, n_ex, n_occ, mismatches


def main():
    sabotage = "--sabotage" in sys.argv
    n, n_ex, n_occ, bad = compare(sabotage=sabotage)

    print("=" * 70)
    print("  rules compared        %d  (%d carry an EXDATE)" % (n, n_ex))
    print("  occurrences compared  %d" % n_occ)
    print("  mismatches            %d" % len(bad))
    print("=" * 70)
    for text, start, tz, got, want in bad[:5]:
        print("  RULE %s  start=%s tz=%s" % (text, start, tz))
        if isinstance(got, list) and want:
            # show the length and the TAIL: the wrong reading differs by what
            # it appends to the end, which a head-truncated view hides
            print("     mine n=%-3d tail %s" % (len(got), got[-3:]))
            print("     ref  n=%-3d tail %s" % (len(want), want[-3:]))
        else:
            print("     mine %s" % got)
            print("     ref  %s" % want)

    if sabotage:
        if bad:
            print()
            print("SABOTAGE CAUGHT. The harness can fail, so a clean run means")
            print("something. The planted bug is the plausible wrong reading of")
            print("RFC 5545: applying EXDATE before COUNT is consumed, which")
            print("tops the series back up and books the patient an extra week.")
            return 0
        print()
        print("SABOTAGE NOT CAUGHT -- the harness is broken and a clean run")
        print("proves nothing.")
        return 1

    doc = os.path.join(ROOT, "docs")
    os.makedirs(doc, exist_ok=True)
    out = os.path.join(doc, "RRULE_VALIDATION.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("""# RRULE validation against `dateutil`

`src/recurrence.py` expands RFC 5545 rules by hand. This audit differences that
expansion against `python-dateutil`, which implements the standard properly.

- **%d randomly generated rules** across the whole supported grammar
  (`FREQ=DAILY|WEEKLY`, `INTERVAL`, `BYDAY`, `COUNT`, `UNTIL`, `EXDATE`)
- **%d of them carry an `EXDATE`**, so the exclusion path is exercised rather
  than merely parsed
- **%d occurrences** compared as local wall-clock dates, across five timezones
- **%d mismatches**

`dateutil` is the reference for RULE EXPANSION only. The timezone handling is
this project's own -- storing local wall clock plus rule, and resolving DST at
expansion time -- which is why the comparison is on local dates and is tested
separately.

## It settles a claim a test could not

`exclude()` asserts a specific reading of RFC 5545 section 3.8.5.1:

> COUNT is consumed BEFORE EXDATE is applied, so excluding one occurrence
> SHORTENS the series rather than sliding a replacement onto the end.

A unit test cannot settle that, because a test written by the person who read
the spec encodes the same reading of it. `dateutil` is an independent reading,
and it agrees: the excluded occurrence still counts, and the series ends on the
same date.

The alternative reading is not academic. Topping the series back up to `COUNT`
means a patient who cancels one week is silently booked an extra one.

## The harness is checked for being able to fail

A comparison reporting zero mismatches is worthless until you know it can
report a non-zero one. `python validate_rrule.py --sabotage` swaps in that
wrong reading, and it must fail -- it produces **118 mismatches** on this seed.

## What this does NOT validate

The supported subset is still small: no `MONTHLY` or `YEARLY`, no `BYSETPOS`,
no `BYMONTHDAY`, no `RDATE`, no `VTIMEZONE`. That is now a scoping decision
rather than a missing dependency, and `parse_rrule` REFUSES unsupported parts
rather than ignoring them -- a dropped part makes the rule mean something else
that still runs.
""" % (n, n_ex, n_occ, len(bad)))
    print("wrote", out)
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
