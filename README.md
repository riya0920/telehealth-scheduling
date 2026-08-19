# SE-3 — Telehealth scheduling correctness (first 20%)

Scheduling looks like a CRUD tutorial and is a constraint-satisfaction
minefield. This builds the mines: licensure by state **on the date of service**,
no double-booking proven under 100-way concurrency, a DST suite that is tested
rather than asserted, and idempotent reminders under crash injection.

```bash
python run_demo.py         # concurrency, licensure, DST, crash injection, tokens
python -m pytest tests -q  # 37 tests
```

Offline, ~3 seconds, standard library only.

---

## The four things worth reading

### 1. Licensure by state, on the DATE OF SERVICE

**The telehealth-specific constraint almost everyone misses.** A provider may
treat a patient only if licensed where the **patient** is physically located,
and the licence must be valid on the **date of service** — not the booking date.

Dr Ellis: NY licence to 2026-12-31, AZ licence to **2025-04-30**.

| booking | result |
|---|---|
| AZ patient, visit 2025-04-30 (last valid day) | **booked** |
| AZ patient, visit 2025-05-01 (day after expiry) | **BLOCKED** |
| AZ patient, visit 2025-08-12 | **BLOCKED** |

Rows two and three are the trap: booked *today*, both pass any check that asks
*"is this provider licensed now?"*. Getting it wrong is a compliance violation
in the real world, not a bug — and `availability()` runs the licence check
**first**, because there is no point computing slots the provider cannot legally
use.

**And what happens when a licence expires between booking and visit?** The demo
shortens the AZ licence after a booking exists:

> the appointment is still `confirmed` — nothing retroactively cancels it, and
> 30 days in April are now uncovered

**Decision: block at booking, plus a licence-change sweep** (`licence_gaps()`)
that flags already-booked appointments for rebooking. Book-then-flag is the only
workable design, because licences change after the fact — but it carries an ops
burden, and **if nobody works that queue the control is theatre.**

### 2. No double-booking, proven

```
run 1: 100 concurrent requests -> 1 success, 99 rejected, 1 row in the table  PASS
run 2..5: same
5/5 runs booked exactly once
```

100 real threads through a `threading.Barrier` to maximise the collision, five
independent runs, one row every time.

**Why `SELECT`-then-`INSERT` cannot do this:** both transactions read "free"
before either writes, at any isolation level below serialisable. The read and
the write are separate, and the gap between them is the race.

**The elegant answer is a Postgres exclusion constraint:**

```sql
ALTER TABLE appointment ADD CONSTRAINT no_double_booking
  EXCLUDE USING gist (
    provider_id WITH =,
    tstzrange(starts_at, ends_at) WITH &&
  ) WHERE (status IN ('requested','confirmed','checked_in'));
```

There is no race because there is no read-then-write: **the check *is* the
write**, inside the index.

Postgres is not available here, so SQLite `BEGIN IMMEDIATE` takes the write lock
before the overlap check. The substitution is genuinely weaker and the
difference is named in `scheduling.py`: it serialises *all* writers rather than
just those touching one provider; correctness depends on every code path
remembering to use it; and a second application writing the same database breaks
the invariant silently. The constraint cannot be bypassed by a careless path —
or by someone running SQL by hand at 2am.

Also enforced: **buffers** (a 30-minute visit with a 10-minute buffer blocks a
start 30 minutes later) and **one patient cannot be in two places at once**,
even with two different providers.

### 3. The DST suite

Provider in ET (observes DST), patient in AZ (does not). US spring forward 2025:
02:00 ET → 03:00 ET on 9 March.

```
March (ET on standard time):  provider 09:00 EST = patient 07:00 MST
April (ET on daylight time):  provider 09:00 EDT = patient 06:00 MST

02:30 ET on 2025-03-09 -> None   (that time never occurs)
01:30 ET on 2025-03-09 -> 01:30 EST
03:30 ET on 2025-03-09 -> 03:30 EDT
```

The provider's recurring 09:00 **stays at 09:00 local** across the transition;
the patient sees it move, because Arizona does not change. A rule stored as a
UTC instant would instead drag the provider's morning to 08:00, and nobody would
notice until a patient arrived an hour early.

**02:30 on the spring-forward day returns `None`.** Python constructs it
happily and hands back a plausible instant; returning `None` makes the
non-existence explicit instead of silently booking someone at 01:30 or 03:30.

*Why "we store everything in UTC" is necessary but not sufficient:* availability
**rules** live in local wall-clock time, recurrences are local, and rendering
needs a zone. UTC alone loses the wall-clock semantics that scheduling runs on.

### 4. Idempotent reminders under crash injection

```
crash injected: crashed after recording T-24h, before sending
reminder rows recorded: 1
after recovery the job runs again: newly sent 0
total reminder rows: 1     NO DUPLICATE: True
```

**Record first, then send** — inside a transaction the send cannot roll back.

That makes the failure mode a **missed** reminder, never a **duplicate** one,
and for appointment reminders that is the right way round: a duplicate 3am SMS
erodes trust in every future message, while a missed one degrades to the
patient's own calendar. *For a medication reminder the calculus reverses*, and
the code says so.

The honest limitation, stated in `workflow.py`: this is **at-most-once**. True
exactly-once needs the messaging provider to accept an idempotency key so a
retry after an ambiguous timeout is deduplicated at their end.

The **cancellation race** is closed too — an appointment cancelled between
selection and send is caught by a status re-check *inside* the send
transaction, not by the query that selected it.

### Plus: state machine, waitlist holds, video tokens

- **Transitions are enforced**, terminal states refuse everything, states cannot
  be skipped, and every transition is audited with actor and timestamp.
- **Waitlist hold expiring mid-checkout:** the claim reads the hold, finds it
  expired, and aborts **before** the insert — so the slot is never allocated
  both to the lapsed patient and to the next in line. A hold claimed twice fails
  the second time.
- **Video tokens** are appointment-bound, role-scoped and time-boxed, and all
  four misuse paths are tested: patient token used as provider, token on a
  different appointment, used two hours early, used the next day, plus a
  tampered payload caught by the HMAC.

---

## What is missing (the other 80%)

- **No Postgres**, therefore no exclusion constraint — the substitution and its
  weaknesses are documented above and in the code, but the elegant answer is
  described rather than run.
- **No HTTP API and no UI.** Everything is a Python method call; there is no
  booking service, no auth, no session handling.
- **No real notification delivery.** `sender` is a callback; there is no SMS or
  email provider, no delivery receipts, and therefore no way to test the
  ambiguous-timeout case that at-most-once actually loses.
- **No real video service.** Tokens are HMAC blobs verified locally — no WebRTC,
  no TURN, no room lifecycle, and no check that the token was actually redeemed.
- **No recurring-availability model.** Working hours are per weekday; there is
  no RRULE, no series booking, no "move the whole series" operation — which is
  where DST bugs really live.
- **No fall-back DST handling.** Spring-forward (nonexistent times) is handled
  and tested; **autumn fall-back (ambiguous times, where 01:30 happens twice) is
  not** — `fold` is never set, so an appointment at 01:30 on the fall-back date
  resolves arbitrarily. That is a real gap, not a rounding error.
- **No overbooking or capacity policy**, no provider panels, no group visits, no
  interpreter or resource scheduling.
- **Licensure is a flat state list.** No compact-state handling (the Interstate
  Medical Licensure Compact and PSYPACT change the answer materially), no
  modality-specific rules, no verification against a licensing board.
- **No load or latency measurement.** The spec asks for booking p99 under load;
  the concurrency test proves correctness, not performance.

## Files

| path | what |
|---|---|
| `src/scheduling.py` | domain model, licensure, availability, booking under concurrency |
| `src/workflow.py` | idempotent reminders, no-shows, waitlist holds, video tokens |
| `run_demo.py` | concurrency proof, licensure traps, DST suite, crash injection |
| `tests/test_scheduling.py` | 37 tests |
