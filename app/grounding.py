"""Resolving what the rider referred to into concrete records.

Deliberately deterministic. The model classifies *what kind* of complaint this
is; resolving "20 wala" to 2026-09-20 and "T926334" to a row is arithmetic and
lookup, and doing it in code makes it testable and identical every run.

The hard part is not parsing dates, it is telling them apart from the other
numbers riders use in the same sentence:

    "18 ko 12 order complete kiye the"   -> day 18, trip count 12
    "21 ko 300 rupay kam aaye"           -> day 21, amount 300
    "penalty 2 baar kata"                -> no day at all
    "kal ka payout, 12 se zyada order"   -> day = anchor - 1, trip count 12

A number is only a day if what follows it says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.normalize import canonical_trip_id

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

# Words that mark the number before them as a calendar day.
DAY_MARKERS = r"(?:wala|wale|wali|tarikh|tareekh|ko|ka|ke|sept|sep|september|th|st|nd|rd)"

# Words that mark the number before them as something other than a day.
NOT_A_DAY_AFTER = r"(?:order|orders|trip|trips|baar|baar|rupay|rupaye|rupee|rupees|rs|km|kms|%)"

_TRIP_ID = re.compile(r"\b[Tt]\s*0*(\d{4,6})\b")
_ISO_DATE = re.compile(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b")
_DAY_MONTH = re.compile(
    r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s*(" + "|".join(MONTHS) + r")\b", re.I
)
_MONTH_DAY = re.compile(
    r"\b(" + "|".join(MONTHS) + r")\s*(\d{1,2})(?:st|nd|rd|th)?\b", re.I
)
_DAY_MARKED = re.compile(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s+" + DAY_MARKERS + r"\b", re.I)
# A bare ordinal is a day on its own: "Trip T672899 on 19th was 7.4 km".
_DAY_ORDINAL = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\b", re.I)
# "2nd order" is an ordinal position, not the 2nd of the month, so the optional
# ordinal suffix has to sit inside the blocking pattern too.
_NUMBER_THEN_NOUN = re.compile(
    r"\b(\d{1,4})\s*(?:st|nd|rd|th)?\s*" + NOT_A_DAY_AFTER + r"\b", re.I
)
_TRIP_COUNT = re.compile(r"\b(\d{1,2})\s*(?:se\s*zyada\s*)?(?:order|orders|trip|trips)\b", re.I)
_AMOUNT = re.compile(
    r"(?:rs\.?|₹|rupay|rupaye|rupee|rupees)\s*(\d{1,6})|\b(\d{1,6})\s*(?:rs\.?|₹|rupay|rupaye|rupee|rupees)\b",
    re.I,
)

RELATIVE_DAYS = {
    "aaj": 0, "today": 0,
    "kal": 1, "yesterday": 1,          # "kal" can mean tomorrow, but a payout
                                       # complaint is always about the past
    "parso": 2, "parson": 2,
}


@dataclass
class Grounding:
    """Everything resolvable from one message, with the raw spans that produced
    it so the trace can show its working."""

    trip_ids: list[str] = field(default_factory=list)
    days: list[date] = field(default_factory=list)
    trip_count_claimed: int | None = None
    amount_claimed: int | None = None
    evidence: list[str] = field(default_factory=list)

    @property
    def has_scope(self) -> bool:
        return bool(self.trip_ids or self.days)

    def as_trace(self) -> dict:
        return {
            "trip_ids": self.trip_ids,
            "days": [str(d) for d in self.days],
            "trip_count_claimed": self.trip_count_claimed,
            "amount_claimed": self.amount_claimed,
            "evidence": self.evidence,
        }


def _resolve_day(day: int, month: int | None, anchor: date) -> date | None:
    """A bare day number belongs to the anchor's month, unless that would put it
    in the future -- then it is last month."""
    if not 1 <= day <= 31:
        return None
    year, use_month = anchor.year, month or anchor.month
    try:
        candidate = date(year, use_month, day)
    except ValueError:
        return None
    if candidate > anchor and month is None:
        previous = use_month - 1 or 12
        year -= 1 if previous == 12 else 0
        try:
            candidate = date(year, previous, day)
        except ValueError:
            return None
    return candidate if candidate <= anchor else None


def extract_trip_ids(text: str) -> list[str]:
    """Trip ids exactly as written. Validation against the ledger happens in
    app/entitlement.py, which knows who is allowed to ask about what."""
    found: list[str] = []
    for match in _TRIP_ID.finditer(text or ""):
        trip_id = canonical_trip_id(match.group(0))
        if trip_id and trip_id not in found:
            found.append(trip_id)
    return found


def extract_days(text: str, anchor: date) -> tuple[list[date], list[str]]:
    text = text or ""
    days: list[date] = []
    evidence: list[str] = []

    def add(day: date | None, span: str) -> None:
        if day and day not in days:
            days.append(day)
            evidence.append(span)

    # Numbers that belong to a noun ("12 orders", "300 rupay") are not days.
    blocked: set[int] = {int(m.group(1)) for m in _NUMBER_THEN_NOUN.finditer(text)}

    for match in _ISO_DATE.finditer(text):
        year, month, day = (int(g) for g in match.groups())
        try:
            add(date(year, month, day), match.group(0))
        except ValueError:
            pass

    for match in _DAY_MONTH.finditer(text):
        day, month = int(match.group(1)), MONTHS[match.group(2).lower()]
        add(_resolve_day(day, month, anchor), match.group(0))

    for match in _MONTH_DAY.finditer(text):
        month, day = MONTHS[match.group(1).lower()], int(match.group(2))
        add(_resolve_day(day, month, anchor), match.group(0))

    for word, offset in RELATIVE_DAYS.items():
        if re.search(rf"\b{word}\b", text, re.I):
            add(anchor - timedelta(days=offset), word)

    for pattern in (_DAY_MARKED, _DAY_ORDINAL):
        for match in pattern.finditer(text):
            day = int(match.group(1))
            if day in blocked:
                continue
            add(_resolve_day(day, None, anchor), match.group(0))

    return days, evidence


def extract_trip_count(text: str) -> int | None:
    """'12 se zyada order kiye' -- the rider's own count, used to answer an
    incentive complaint, never used as money."""
    matches = _TRIP_COUNT.findall(text or "")
    return int(matches[0]) if matches else None


def extract_amount(text: str) -> int | None:
    """Recorded for the trace only. Never an input to a payment: case 21 claims
    Rs 300 and is owed Rs 25."""
    for match in _AMOUNT.finditer(text or ""):
        value = match.group(1) or match.group(2)
        if value:
            return int(value)
    return None


def ground(text: str, anchor: date) -> Grounding:
    trip_ids = extract_trip_ids(text)
    days, evidence = extract_days(text, anchor)
    return Grounding(
        trip_ids=trip_ids,
        days=days,
        trip_count_claimed=extract_trip_count(text),
        amount_claimed=extract_amount(text),
        evidence=evidence + [f"trip_id:{t}" for t in trip_ids],
    )


def resolve_trip_for_rider(ref, rider_id: str, written_id: str) -> tuple[str | None, str]:
    """Match a written trip id to one of this rider's trips.

    Rider scope is applied *before* similarity, not after. A typo must never
    become a payment on somebody else's trip -- that is the confused-deputy
    failure, and returning "I couldn't find that order" is strictly better.
    """
    if ref.trip(written_id) is not None:
        return written_id, "exact"

    candidates = [t.trip_id for t in ref.trips_of(rider_id)]
    near = [c for c in candidates if _edit_distance_at_most_one(c, written_id)]
    if len(near) == 1:
        return near[0], "fuzzy_rider_scoped"
    return None, "unresolved"


def _edit_distance_at_most_one(a: str, b: str) -> bool:
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    shorter, longer = (a, b) if len(a) < len(b) else (b, a)
    for i in range(len(longer)):
        if longer[:i] + longer[i + 1:] == shorter:
            return True
    return False
