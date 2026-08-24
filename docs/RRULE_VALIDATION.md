# RRULE validation against `dateutil`

`src/recurrence.py` expands RFC 5545 rules by hand. This audit differences that
expansion against `python-dateutil`, which implements the standard properly.

- **400 randomly generated rules** across the whole supported grammar
  (`FREQ=DAILY|WEEKLY`, `INTERVAL`, `BYDAY`, `COUNT`, `UNTIL`, `EXDATE`)
- **212 of them carry an `EXDATE`**, so the exclusion path is exercised rather
  than merely parsed
- **5963 occurrences** compared as local wall-clock dates, across five timezones
- **0 mismatches**

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
