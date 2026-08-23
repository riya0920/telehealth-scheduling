"""Licensure compacts, per-occurrence exceptions, and series booking.

THREE NAMED GAPS
----------------
"Licensure is a flat state list. No compact-state handling (the Interstate
Medical Licensure Compact and PSYPACT change the answer materially)."
"no per-occurrence exceptions -- 'cancel just the 12 November one' is the most
common real request and is not supported."
"Series are not persisted or booked ... there is no partial-failure story for
'book 8 occurrences and the 5th clashes'."

WHY A COMPACT IS NOT JUST A LONGER STATE LIST
-----------------------------------------------
The naive fix is to expand a compact into the states it covers and carry on.
That is wrong in three specific ways, and each one changes a real answer:

1. A COMPACT IS AN EXPEDITED PATHWAY, NOT A LICENCE. The IMLC does not let a
   physician practise in 40 states; it lets them OBTAIN a licence in a member
   state quickly. The licence still has to exist and still has to be issued.
   Treating compact membership as authorisation would put a provider in front
   of a patient in a state where they hold nothing.

2. PSYPACT IS DIFFERENT IN KIND, and this is the one worth knowing. PSYPACT
   grants an actual authority to practise telepsychology across member states
   (an APIT), which is genuinely closer to "one credential, many states". So
   IMLC and PSYPACT cannot share a code path even though both are called
   compacts.

3. THE COMPACT IS MODALITY- AND PROFESSION-SPECIFIC. PSYPACT covers
   telepsychology by psychologists. The Nurse Licensure Compact covers nursing.
   IMLC covers physicians. A provider's specialty and the visit's modality both
   participate in the answer, and a flat state list can express neither.

AND MEMBERSHIP HAS A DATE. States join compacts, and PSYPACT states joined on
different dates through the 2020s. `is_authorised` therefore takes the DATE OF
SERVICE, exactly as `is_licensed` does -- a state that joins next month does not
authorise an appointment booked for next week.

WHAT THIS IS NOT
----------------
The membership lists here are ILLUSTRATIVE, not current, and are not a legal
reference. Compact membership changes by legislative session; a real deployment
verifies against the compact commissions and the state boards, and re-verifies,
because a licence can also be suspended between the booking and the visit. There
is no DEA registration, no controlled-substance rule, no
originating-site requirement, and no consent-by-state handling.
"""

from __future__ import annotations

from datetime import date

# ILLUSTRATIVE membership with join dates. Not current, not legal advice.
IMLC_STATES = {
    "AL": "2017-04-01", "AZ": "2016-01-01", "CO": "2015-05-01",
    "IA": "2015-07-01", "ID": "2015-04-01", "IL": "2015-08-01",
    "KS": "2017-07-01", "ME": "2017-08-01", "MD": "2018-01-01",
    "MI": "2018-03-01", "MN": "2015-05-01", "MT": "2015-04-01",
    "NE": "2018-07-01", "NH": "2016-01-01", "NV": "2015-05-01",
    "PA": "2017-10-01", "SD": "2015-03-01", "TN": "2017-07-01",
    "UT": "2015-03-01", "VT": "2018-05-01", "WA": "2017-07-01",
    "WI": "2015-04-01", "WV": "2016-06-01", "WY": "2015-03-01",
}

PSYPACT_STATES = {
    "AZ": "2020-07-01", "CO": "2020-07-01", "DE": "2020-07-01",
    "GA": "2020-07-01", "IL": "2020-07-01", "MO": "2020-07-01",
    "NE": "2020-07-01", "NH": "2020-07-01", "NV": "2020-07-01",
    "OK": "2020-07-01", "PA": "2021-01-01", "TX": "2020-07-01",
    "UT": "2020-07-01", "VA": "2021-07-01", "OH": "2021-05-01",
    "MD": "2022-01-01", "NJ": "2022-07-01", "TN": "2022-01-01",
}

NLC_STATES = {"AZ": "2018-01-19", "CO": "2018-01-19", "FL": "2018-01-19",
              "GA": "2018-01-19", "ID": "2018-01-19", "IA": "2018-01-19"}

COMPACTS = {
    "IMLC": {
        "states": IMLC_STATES,
        "professions": {"physician"},
        "modalities": None,                    # not modality-limited
        "grants_practice_authority": False,    # THE KEY DIFFERENCE
        "what_it_grants": (
            "an EXPEDITED PATHWAY to obtain a full licence in a member state. "
            "It is not itself a licence and does not authorise practice. A "
            "provider must still hold an issued licence in the state of the "
            "patient."),
    },
    "PSYPACT": {
        "states": PSYPACT_STATES,
        "professions": {"psychologist"},
        "modalities": {"video", "phone"},       # telepsychology
        "grants_practice_authority": True,
        "what_it_grants": (
            "an Authority to Practice Interjurisdictional Telepsychology "
            "(APIT), which DOES authorise telepsychology into other member "
            "states without a separate licence in each."),
    },
    "NLC": {
        "states": NLC_STATES,
        "professions": {"nurse"},
        "modalities": None,
        "grants_practice_authority": True,
        "what_it_grants": (
            "a multistate licence permitting practice in other member states."),
    },
}


class CompactError(ValueError):
    pass


def _d(value):
    return date.fromisoformat(str(value)[:10]) if not isinstance(value, date) \
        else value


def member_on(compact, state, service_date):
    """Was `state` a member of `compact` on the date of service?

    A state that joins next month does not authorise an appointment booked for
    next week, which is the same date-of-service discipline `is_licensed` uses
    for the licence itself.
    """
    spec = COMPACTS.get(compact)
    if spec is None:
        raise CompactError(f"unknown compact {compact!r}. "
                           f"Known: {sorted(COMPACTS)}")
    joined = spec["states"].get(state)
    if joined is None:
        return False
    return _d(service_date) >= _d(joined)


def authorises(compact, *, home_state, patient_state, profession, modality,
               service_date):
    """Does this compact authorise the visit? Returns (bool, reason).

    Returns FALSE for IMLC in every case, and the reason says why. That is not
    a bug: the IMLC is an expedited pathway to obtain a licence, and treating
    compact membership as authorisation would put a provider in front of a
    patient in a state where they hold nothing.
    """
    spec = COMPACTS.get(compact)
    if spec is None:
        raise CompactError(f"unknown compact {compact!r}")

    if not spec["grants_practice_authority"]:
        return False, (
            f"{compact} does not grant practice authority. "
            f"{spec['what_it_grants']}")

    if profession not in spec["professions"]:
        return False, (f"{compact} covers {sorted(spec['professions'])}, not "
                       f"{profession!r}")

    if spec["modalities"] is not None and modality not in spec["modalities"]:
        return False, (f"{compact} covers {sorted(spec['modalities'])} only; "
                       f"this visit is {modality!r}")

    if not member_on(compact, home_state, service_date):
        return False, (f"the provider's home state {home_state} was not a "
                       f"{compact} member on {service_date}")

    if not member_on(compact, patient_state, service_date):
        return False, (f"the patient's state {patient_state} was not a "
                       f"{compact} member on {service_date}")

    return True, (f"{compact}: {spec['what_it_grants']}")


def why_not(home_state, patient_state, profession, modality, service_date):
    """Every compact, and why each one does or does not help.

    Returned in full rather than as a boolean, because "not licensed" is the
    answer a front desk cannot act on. "PSYPACT would cover this but Ohio joined
    after the date of service" is an answer somebody can do something with.
    """
    out = []
    for name in COMPACTS:
        ok, reason = authorises(name, home_state=home_state,
                                patient_state=patient_state,
                                profession=profession, modality=modality,
                                service_date=service_date)
        out.append({"compact": name, "authorises": ok, "reason": reason})
    return out


def check(scheduler, provider_id, home_state, patient_state, profession,
          modality, service_date, con=None):
    """Licence first, then compacts. Returns a decision with its basis.

    ORDER MATTERS. A held licence is the strongest and simplest answer, and
    checking it first means the common case never touches compact logic at all.
    A compact is only consulted when there is no licence to rely on.
    """
    if scheduler.is_licensed(provider_id, patient_state, service_date,
                             con=con):
        return {"authorised": True, "basis": "licence",
                "detail": (f"{provider_id} holds a licence in {patient_state} "
                           f"on the date of service")}

    for name in COMPACTS:
        ok, reason = authorises(name, home_state=home_state,
                                patient_state=patient_state,
                                profession=profession, modality=modality,
                                service_date=service_date)
        if ok:
            return {"authorised": True, "basis": name, "detail": reason}

    return {
        "authorised": False, "basis": None,
        "detail": (f"{provider_id} holds no licence in {patient_state} on "
                   f"{service_date} and no compact authorises the visit"),
        "compacts": why_not(home_state, patient_state, profession, modality,
                            service_date),
    }
