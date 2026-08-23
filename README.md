# SE-3 — Telehealth scheduling correctness (~80% build)

Scheduling looks like a CRUD tutorial and is a constraint-satisfaction
minefield. This builds the mines: licensure by state **on the date of service**,
no double-booking proven under 100-way concurrency, a DST suite that is tested
rather than asserted, and idempotent reminders under crash injection.

```bash
python run_demo.py         # concurrency, licensure, DST, crash injection, tokens
python serve.py --load     # booking p99, contended and spread
python serve.py            # booking API on :8090
python -m pytest tests -q  # 67 tests
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

**Autumn fall-back, the case UTC storage does *not* solve.** On 2025-11-02 the
clocks go back at 02:00, so **01:30 happens twice** — two different instants an
hour apart that render as the same wall clock:

```
01:30 ET on 2025-11-02 is AMBIGUOUS
  2025-11-02T05:30:00+00:00 = 01:30 EDT
  2025-11-02T06:30:00+00:00 = 01:30 EST
```

`classify_local_time()` returns one of three things — `ok`, `nonexistent`,
`ambiguous` — because a scheduler that assumes the first is wrong twice a year.

The consequences are concrete and both are tested:

| day (provider works 01:00–04:00 local) | slots | real hours |
|---|---|---|
| fall-back Sunday | **8** | **4.0** |
| ordinary Sunday | 6 | 3.0 |
| spring-forward Sunday | **4** | **2.0** |

The fall-back day genuinely has an **extra bookable hour** and the
spring-forward day genuinely has one fewer. Collapsing the repeated hour loses a
slot; treating it as one slot double-books it.
`test_both_halves_of_the_repeated_hour_are_separately_bookable` books both.

And the half that UTC discipline cannot fix — **the confirmation email**:

```
Sun 02 Nov 2025, 01:30 AM EDT (this local time occurs twice tonight ...)
Sun 02 Nov 2025, 01:30 AM EST (this local time occurs twice tonight ...)
```

Storing UTC makes the *booking* unambiguous and does nothing for the *rendering*.
"Sunday 2 November, 1:30 AM" is two different appointments and the patient has
no way to tell which one they have, so `describe_for_patient()` always carries
the zone and flags the ambiguity.

*Why "we store everything in UTC" is necessary but not sufficient:* availability
**rules** live in local wall-clock time, recurrences are local, and rendering
needs a zone. UTC alone loses the wall-clock semantics that scheduling runs on —
and it is silent about a wall clock that happens twice.

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

## Recurring series — the DST bug that only series have

The gap list said it twice: *"there is no RRULE model, so 'move the whole
series' — where DST bugs really live — is untested because series do not
exist."* `src/recurrence.py` is that model.

### A weekly series stored as UTC drifts by an hour, silently

Store "Tuesdays at 09:00" as a UTC instant plus a 7-day interval and every
occurrence after the transition lands an hour out. Nothing errors. The patient,
told 9am, misses a session — which for a weekly therapy series is a gap in a
course of care, not a scheduling inconvenience.

The series is therefore stored as a **local wall clock plus a rule**, and every
occurrence is resolved against the timezone independently:

| local date | tz | UTC |
|---|---|---|
| 2024-10-29 | EDT | 13:00Z |
| **2024-11-05** | **EST** | **14:00Z** |

The UTC instants are **deliberately not evenly spaced**. A series whose UTC
instants are evenly spaced across a transition is a series whose local times
are wrong.

`naive_utc_occurrences()` implements the bug on purpose so it can be measured,
and `storage_drift()` reports it:

```
2024-11-05  intended 09:00 (EST)  naive 08:00 (EST)  drift -60 min
2024-11-12  intended 09:00 (EST)  naive 08:00 (EST)  drift -60 min
2024-11-19  intended 09:00 (EST)  naive 08:00 (EST)  drift -60 min
```

### "Move the whole series" — and a claim the measurement corrected

I first wrote that the wall-clock and absolute readings of a move diverge *"once
a transition is inside the range"*. **That is wrong**, and measuring it said so:
once each occurrence is resolved independently, adding a constant to a
correctly-resolved instant preserves its local time, so the 8-week series above
sees **no disagreement at all**.

They diverge somewhere narrower and much harder to spot — when the *occurrence
itself* sits in an ambiguous window. A series at 01:30 on US fall-back Sunday,
moved 60 minutes later:

| date | wall clock | absolute |
|---|---|---|
| 2024-10-27 | 02:30 EDT | 02:30 EDT |
| **2024-11-03** | **02:30 EST** | **01:30 EST** ← the *second* 01:30 |
| 2024-11-10 | 02:30 EST | 02:30 EST |

One occurrence out of three. `move()` refuses to pick a default and
`compare_moves()` answers "does this move disagree with itself, and where".

## Booking p99 under load — and two bugs it found

`python serve.py --load`. The spec asked for booking p99 under load; the
existing concurrency test proved correctness, not performance.

**The load runs contended, not spread.** Clients booking distinct slots never
touch the same row and produce a flattering p99 that measures the HTTP stack.
24 clients racing for **one** slot measure the thing that prevents a
double-booking:

```
mode             n      p50      p95      p99      max    req/s
spread          16    149.6    843.2    843.2    843.2       17
contended       24     31.4    192.5    509.5    509.5       45

CONTENDED: 24 clients raced for ONE slot -> 1 booked, 23 got 409.
Exactly one winner, and zero server errors.
```

**And the p99 comparison came out backwards from what I expected.** Contended
p99 is *lower*. Not because the lock is fast — because a conflicting booking
**short-circuits**: it takes the write lock, finds the clash, rolls back and
returns 409 without ever writing. 23 of 24 requests do less work than a
successful booking. Reporting that as a latency win would be reading a
rejection rate as performance. The number that would actually hurt — p99 for a
request that *wins* under contention — has a sample size of one per run, and
measuring it needs many contended slots in parallel, which is not done here.

### Two real bugs, found by running it rather than reading it

- **`is_licensed()` always read the shared connection.** `book(con=...)` used
  the caller's connection for its own queries and `self.con` for the licensure
  check. Under 24 concurrent bookings that produced sqlite3 `InterfaceError` and
  `DatabaseError` — and, worse, **intermittent spurious `LicenceViolation`s**: a
  lawful booking refused, with the one error message that sends someone to
  investigate licensure rather than the database. The existing concurrency test
  never caught it because it called `book()` directly with its own connection
  and never mixed the two.
- **An unhandled exception dropped the connection instead of answering.**
  Passing an ISO string where `book()` wanted a datetime raised deep inside, and
  `BaseHTTPRequestHandler` closed the socket. The client saw
  `RemoteDisconnected` — indistinguishable from a network fault, and invisible
  to a load harness that only catches `HTTPError`. The first load run therefore
  reported `n=1` and a flawless p99 of 0.0 ms. Both the handler and the harness
  are fixed; the harness now records failures as outcomes rather than dropping
  them.

Also worth naming, because it looks like a bug and is not: **"spread" mode still
produces 409s.** The visit type carries a 10-minute buffer, so adjacent slots
overlap once the buffer is applied. A generated slot list is a list of *start
times*, not a list of independently bookable ones.

## What is still missing

- **No Postgres**, therefore no exclusion constraint — the substitution and its
  weaknesses are documented above and in the code, but the elegant answer is
  described rather than run.
- **The HTTP API has no auth and no UI.** No sessions, no patient identity,
  no CSRF, no rate limiting — anyone who can reach the port can book on anyone's
  behalf. The API exists to make the concurrency and DST behaviour reachable
  over a socket, not to be a booking service.
- **No real notification delivery.** `sender` is a callback; there is no SMS or
  email provider, no delivery receipts, and therefore no way to test the
  ambiguous-timeout case that at-most-once actually loses.
- **No real video service.** Tokens are HMAC blobs verified locally — no WebRTC,
  no TURN, no room lifecycle, and no check that the token was actually redeemed.
- **The RRULE subset is small.** FREQ=DAILY|WEEKLY, INTERVAL, BYDAY, COUNT,
  UNTIL. No MONTHLY or YEARLY, no BYSETPOS, no BYMONTHDAY, no EXDATE/RDATE, no
  VTIMEZONE serialisation, and **no per-occurrence exceptions** — "cancel just
  the 12 November one" is the most common real request and is not supported.
- **Series are not persisted or booked.** They expand to instants and the
  drift is measured, but a series is held in process memory and its occurrences
  are not written as appointments, so there is no partial-failure story for
  "book 8 occurrences and the 5th clashes".
- **No overbooking or capacity policy**, no provider panels, no group visits, no
  interpreter or resource scheduling.
- **Licensure is a flat state list.** No compact-state handling (the Interstate
  Medical Licensure Compact and PSYPACT change the answer materially), no
  modality-specific rules, no verification against a licensing board.
- **The load measurement is a floor, not a service level.** One process,
  loopback, file-backed SQLite on local disk, 24 clients, no other tenants. And
  the most useful number — p99 for the request that *wins* under contention —
  has a sample size of one per run and is not measured.

## Files

| path | what |
|---|---|
| `src/scheduling.py` | domain model, licensure, availability, booking under concurrency |
| `src/workflow.py` | idempotent reminders, no-shows, waitlist holds, video tokens |
| `run_demo.py` | concurrency proof, licensure traps, DST suite, crash injection |
| `src/recurrence.py` | RRULE subset, local-wall-clock series, the UTC drift measured |
| `serve.py` | booking API, series routes, contended p99 measurement |
| `tests/test_recurrence.py` | 20 tests: expansion, drift, and both readings of a move |
| `tests/test_scheduling.py` | 47 tests |
