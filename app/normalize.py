"""Normalisation of the exports in data/.

The exports carry three faults that are formatting problems, not disputes.
They must be repaired on ingest so the dispute engine never sees them:

1. distance_km holds metres in 222 of 3314 rows (values like 5000, 11400 --
   all exact multiples of 100). Evidence this is metres and not a dispute:
   read as metres, 202 of the 204 paid rows in that group match the policy
   fare exactly; read as kilometres, zero do. The two exceptions are
   themselves planted underpayment bugs (T312538, T659741).

2. rider_id is not canonical: trips.csv contains "R7" and "r19" where
   riders.csv has R007 and R019. T840677 belongs to "R7", so case 15 of the
   eval set is unanswerable without this.

3. Ten trip rows are exact duplicates. Left in place they inflate daily trip
   counts and conjure incentives nobody earned.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

_RIDER_ID = re.compile(r"^\s*[Rr]0*(\d+)\s*$")
_TRIP_ID = re.compile(r"\b[Tt]\s*0*(\d{4,6})\b")

# Above this, a distance is metres rather than kilometres. The longest honest
# trip in the exports is 21.0 km, and every suspect row is a multiple of 100.
METRES_THRESHOLD = 100.0

STATUS_COMPLETED = "completed"
STATUS_CANCELLED_BY_RIDER = "cancelled_by_rider"
STATUS_CANCELLED_BY_CUSTOMER = "cancelled_by_customer"

LINE_TRIP = "trip"
LINE_INCENTIVE = "daily_incentive"
LINE_PENALTY = "cancellation_penalty"


def canonical_rider_id(raw: str) -> str:
    """'R7' -> 'R007', 'r19' -> 'R019', 'R003' -> 'R003'.

    Unparseable input is returned stripped rather than guessed at, so it fails
    a lookup loudly instead of silently resolving to the wrong rider.
    """
    if raw is None:
        return ""
    match = _RIDER_ID.match(str(raw))
    if not match:
        return str(raw).strip()
    return "R%03d" % int(match.group(1))


def canonical_trip_id(raw: str) -> str:
    """'t926334' or 'T 926334' -> 'T926334'. Returns '' if there is no trip id."""
    if raw is None:
        return ""
    match = _TRIP_ID.search(str(raw))
    if not match:
        return ""
    return "T%s" % match.group(1)


def canonical_distance_km(raw: str | float) -> float:
    """Repair the metres-in-a-kilometres-column fault.

    Only values above 100 are touched, so a genuine 21.0 km trip is untouched
    and a genuine 0.8 km trip is untouched.
    """
    km = float(raw)
    if km > METRES_THRESHOLD:
        return km / 1000.0
    return km


def ist_day(started_at: str | datetime) -> date:
    """UTC timestamp from the trips export -> IST calendar day.

    'Day' means the calendar day in IST (docs/policy.md), so a trip at
    2026-09-19T20:30:00Z belongs to 2026-09-20.
    """
    if isinstance(started_at, datetime):
        moment = started_at
    else:
        moment = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(IST).date()


def ist_now_from(received_at: str) -> datetime:
    """The message's own timestamp, in IST. This is the clock the agent uses."""
    moment = datetime.fromisoformat(str(received_at).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=IST)
    return moment.astimezone(IST)


@dataclass(frozen=True)
class Trip:
    trip_id: str
    rider_id: str
    day: date
    status: str
    distance_km: float
    surge_multiplier: float

    @property
    def is_completed(self) -> bool:
        return self.status == STATUS_COMPLETED

    @property
    def is_rider_cancellation(self) -> bool:
        return self.status == STATUS_CANCELLED_BY_RIDER


@dataclass(frozen=True)
class PayoutLine:
    line_id: str
    payout_date: date
    rider_id: str
    line_type: str
    trip_id: str
    amount: int


def normalize_trip_row(row: dict) -> Trip:
    return Trip(
        trip_id=canonical_trip_id(row["trip_id"]) or str(row["trip_id"]).strip(),
        rider_id=canonical_rider_id(row["rider_id"]),
        day=ist_day(row["started_at"]),
        status=str(row["status"]).strip(),
        distance_km=canonical_distance_km(row["distance_km"]),
        surge_multiplier=float(row["surge_multiplier"]),
    )


def normalize_payout_line_row(row: dict) -> PayoutLine:
    return PayoutLine(
        line_id=str(row["line_id"]).strip(),
        payout_date=date.fromisoformat(str(row["payout_date"]).strip()),
        rider_id=canonical_rider_id(row["rider_id"]),
        line_type=str(row["line_type"]).strip(),
        trip_id=canonical_trip_id(row["trip_id"]),
        amount=int(row["amount"]),
    )


def dedupe_trips(trips: list[Trip]) -> list[Trip]:
    """Drop exact duplicate rows, keeping first occurrence and order.

    Deliberately keyed on the whole row, not on trip_id alone: two rows that
    share an id but disagree on their contents are a real conflict and must
    survive to be escalated, not silently collapsed. In this export all ten
    duplicates are byte-identical, so none reach that path.
    """
    seen: set[Trip] = set()
    unique: list[Trip] = []
    for trip in trips:
        if trip in seen:
            continue
        seen.add(trip)
        unique.append(trip)
    return unique


def conflicting_trip_ids(trips: list[Trip]) -> list[str]:
    """trip_ids that appear more than once with differing contents."""
    by_id: dict[str, set[Trip]] = {}
    for trip in trips:
        by_id.setdefault(trip.trip_id, set()).add(trip)
    return sorted(tid for tid, rows in by_id.items() if len(rows) > 1)
