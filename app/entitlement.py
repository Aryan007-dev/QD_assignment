"""What the rider was owed, minus what the rider was paid, inside the scope of
the dispute they actually raised.

The scope rule is the heart of this module and it is not obvious. A rider who
complains about one order usually has *other* unclaimed shortfalls sitting in
the same seven days -- the exports are full of them. Auditing the whole week
and paying everything found computes more than the dispute is worth, produces
a payment nobody asked for and ops cannot reconcile, and fails four of the
twenty-four sample conversations (9, 11, 16, 21). So: settle the claim, do not
audit the rider.

Scope is therefore exactly one of:
  * the trips the rider named, or
  * the single day the rider named.

Vague complaints get a clarifying question instead of a blanket audit; that
decision lives in app/decide.py, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.normalize import (
    STATUS_CANCELLED_BY_CUSTOMER,
    STATUS_CANCELLED_BY_RIDER,
    Trip,
)
from app.policy import (
    CANCELLATION_PENALTY,
    DAILY_INCENTIVE,
    INCENTIVE_MIN_TRIPS,
    trip_fare,
    within_dispute_window,
)
from app.store import ReferenceData


@dataclass(frozen=True)
class Reason:
    """A component of the shortfall. Every rupee paid traces back to one."""

    kind: str  # surge_or_fare_short | trip_unpaid | incentive_missing | penalty_duplicated
    amount: int
    trip_id: str | None = None
    day: date | None = None
    detail: dict = field(default_factory=dict)

    @property
    def component_key(self) -> str:
        """Identifies the thing being compensated, independent of how the rider
        phrased it or which scope found it.

        The payout exports in data/ are a static snapshot, so settling a
        shortfall does not make it disappear from the ledger we read. Without
        this key, a rider who complains about the same order twice in different
        words gets compensated twice -- once auto-paid, once queued for ops.
        """
        if self.kind == "incentive_missing":
            return f"incentive:{self.day}"
        if self.kind == "penalty_duplicated":
            return f"penalty:{self.trip_id}"
        return f"trip:{self.trip_id}"

    def describe(self) -> str:
        if self.kind == "surge_or_fare_short":
            return (
                f"{self.trip_id}: owed Rs{self.detail.get('owed')}, "
                f"paid Rs{self.detail.get('paid')}, short Rs{self.amount}"
            )
        if self.kind == "trip_unpaid":
            return f"{self.trip_id}: owed Rs{self.amount}, no payout line at all"
        if self.kind == "incentive_missing":
            return (
                f"{self.day}: {self.detail.get('completed_trips')} completed trips, "
                f"daily incentive of Rs{self.amount} not paid"
            )
        if self.kind == "penalty_duplicated":
            return (
                f"{self.trip_id}: cancellation penalty charged "
                f"{self.detail.get('times_charged')} times, Rs{self.amount} over-deducted"
            )
        return f"{self.kind}: Rs{self.amount}"


@dataclass(frozen=True)
class Flag:
    """Something the engine noticed that is not money owed. Flags drive the
    reply and the escalation decision; they never add to the shortfall."""

    kind: str
    trip_id: str | None = None
    detail: dict = field(default_factory=dict)


@dataclass
class Settlement:
    rider_id: str
    shortfall: int = 0
    reasons: list[Reason] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)
    scope: dict = field(default_factory=dict)

    @property
    def component_keys(self) -> set[str]:
        """What this settlement would compensate, for the ledger of settled work."""
        return {reason.component_key for reason in self.reasons}

    @property
    def flag_kinds(self) -> set[str]:
        return {flag.kind for flag in self.flags}

    def has_flag(self, kind: str) -> bool:
        return any(flag.kind == kind for flag in self.flags)

    def as_trace(self) -> dict:
        return {
            "rider_id": self.rider_id,
            "scope": self.scope,
            "shortfall": self.shortfall,
            "reasons": [
                {
                    "kind": r.kind,
                    "trip_id": r.trip_id,
                    "day": str(r.day) if r.day else None,
                    "amount": r.amount,
                    "detail": r.detail,
                    "text": r.describe(),
                }
                for r in self.reasons
            ],
            "flags": [
                {"kind": f.kind, "trip_id": f.trip_id, "detail": f.detail}
                for f in self.flags
            ],
        }


def _settle_trip(
    ref: ReferenceData,
    settlement: Settlement,
    trip: Trip,
    compensated: set[str] | None = None,
) -> None:
    """Fare owed vs fare paid for one completed trip. Mutates settlement."""
    if compensated and f"trip:{trip.trip_id}" in compensated:
        settlement.flags.append(
            Flag(kind="already_compensated", trip_id=trip.trip_id, detail={"component": f"trip:{trip.trip_id}"})
        )
        return

    owed = trip_fare(trip.distance_km, trip.surge_multiplier)
    paid = ref.amount_paid_for_trip(trip.trip_id)

    if paid is None:
        settlement.shortfall += owed
        settlement.reasons.append(
            Reason(
                kind="trip_unpaid",
                amount=owed,
                trip_id=trip.trip_id,
                day=trip.day,
                detail={
                    "owed": owed,
                    "distance_km": trip.distance_km,
                    "surge_multiplier": trip.surge_multiplier,
                },
            )
        )
        return

    if paid < owed:
        delta = owed - paid
        settlement.shortfall += delta
        settlement.reasons.append(
            Reason(
                kind="surge_or_fare_short",
                amount=delta,
                trip_id=trip.trip_id,
                day=trip.day,
                detail={
                    "owed": owed,
                    "paid": paid,
                    "distance_km": trip.distance_km,
                    "surge_multiplier": trip.surge_multiplier,
                },
            )
        )
        return

    if paid > owed:
        # Recorded for ops, never clawed back and never netted against a
        # shortfall the rider is owed elsewhere.
        settlement.flags.append(
            Flag(
                kind="overpaid",
                trip_id=trip.trip_id,
                detail={"owed": owed, "paid": paid, "excess": paid - owed},
            )
        )
        return

    settlement.flags.append(
        Flag(
            kind="trip_paid_correctly",
            trip_id=trip.trip_id,
            detail={
                "owed": owed,
                "paid": paid,
                "distance_km": trip.distance_km,
                "surge_multiplier": trip.surge_multiplier,
            },
        )
    )


def _settle_duplicate_penalty(
    ref: ReferenceData,
    settlement: Settlement,
    trip: Trip,
    compensated: set[str] | None = None,
) -> None:
    """A rider cancellation earns exactly one Rs 10 penalty. More is an error."""
    if compensated and f"penalty:{trip.trip_id}" in compensated:
        settlement.flags.append(
            Flag(kind="already_compensated", trip_id=trip.trip_id, detail={"component": f"penalty:{trip.trip_id}"})
        )
        return

    times = ref.penalties_charged_for_trip(trip.trip_id)
    if times > 1:
        excess = (times - 1) * CANCELLATION_PENALTY
        settlement.shortfall += excess
        settlement.reasons.append(
            Reason(
                kind="penalty_duplicated",
                amount=excess,
                trip_id=trip.trip_id,
                day=trip.day,
                detail={"times_charged": times, "correct_times": 1},
            )
        )
    elif times == 1:
        settlement.flags.append(
            Flag(
                kind="penalty_legitimate",
                trip_id=trip.trip_id,
                detail={"status": trip.status, "penalty": CANCELLATION_PENALTY},
            )
        )


def settle_trips(
    ref: ReferenceData,
    rider_id: str,
    anchor_day: date,
    trip_ids: list[str],
    compensated: set[str] | None = None,
) -> Settlement:
    """Scope = the trips the rider named."""
    settlement = Settlement(
        rider_id=rider_id,
        scope={"kind": "trips", "trip_ids": list(trip_ids), "anchor_day": str(anchor_day)},
    )

    for trip_id in trip_ids:
        trip = ref.trip(trip_id)

        if trip is None:
            settlement.flags.append(Flag(kind="unknown_trip", trip_id=trip_id))
            continue

        if trip.rider_id != rider_id:
            # Never retarget to the other rider's trip, and never reveal whose
            # it is in a reply -- this goes to a human.
            settlement.flags.append(
                Flag(
                    kind="other_rider",
                    trip_id=trip_id,
                    detail={"belongs_to": trip.rider_id},
                )
            )
            continue

        if not within_dispute_window(trip.day, anchor_day):
            settlement.flags.append(
                Flag(
                    kind="outside_window",
                    trip_id=trip_id,
                    detail={
                        "trip_day": str(trip.day),
                        "anchor_day": str(anchor_day),
                        "days_ago": (anchor_day - trip.day).days,
                    },
                )
            )
            continue

        if trip.status == STATUS_CANCELLED_BY_RIDER:
            _settle_duplicate_penalty(ref, settlement, trip, compensated)
            continue

        if trip.status == STATUS_CANCELLED_BY_CUSTOMER:
            settlement.flags.append(
                Flag(
                    kind="customer_cancellation",
                    trip_id=trip_id,
                    detail={"status": trip.status},
                )
            )
            continue

        _settle_trip(ref, settlement, trip, compensated)

    return settlement


def settle_day(
    ref: ReferenceData,
    rider_id: str,
    anchor_day: date,
    day: date,
    compensated: set[str] | None = None,
) -> Settlement:
    """Scope = one IST calendar day. Checks all three policy components."""
    settlement = Settlement(
        rider_id=rider_id,
        scope={"kind": "day", "day": str(day), "anchor_day": str(anchor_day)},
    )

    if not within_dispute_window(day, anchor_day):
        settlement.flags.append(
            Flag(
                kind="outside_window",
                detail={
                    "day": str(day),
                    "anchor_day": str(anchor_day),
                    "days_ago": (anchor_day - day).days,
                },
            )
        )
        return settlement

    trips = ref.trips_on(rider_id, day)
    if not trips:
        settlement.flags.append(Flag(kind="no_trips_that_day", detail={"day": str(day)}))
        return settlement

    completed = [t for t in trips if t.is_completed]

    for trip in completed:
        _settle_trip(ref, settlement, trip, compensated)

    # Daily incentive.
    if len(completed) >= INCENTIVE_MIN_TRIPS:
        if compensated and f"incentive:{day}" in compensated:
            settlement.flags.append(
                Flag(kind="already_compensated", detail={"component": f"incentive:{day}"})
            )
        elif not ref.incentive_was_paid(rider_id, day):
            settlement.shortfall += DAILY_INCENTIVE
            settlement.reasons.append(
                Reason(
                    kind="incentive_missing",
                    amount=DAILY_INCENTIVE,
                    day=day,
                    detail={"completed_trips": len(completed), "threshold": INCENTIVE_MIN_TRIPS},
                )
            )
        else:
            settlement.flags.append(
                Flag(
                    kind="incentive_already_paid",
                    detail={"completed_trips": len(completed), "day": str(day)},
                )
            )
    else:
        # The rider needs this number even when nothing is owed: the sample
        # conversations expect the true trip count in the reply.
        settlement.flags.append(
            Flag(
                kind="incentive_not_earned",
                detail={
                    "completed_trips": len(completed),
                    "threshold": INCENTIVE_MIN_TRIPS,
                    "day": str(day),
                },
            )
        )

    # Duplicated cancellation penalties.
    for trip in trips:
        if trip.is_rider_cancellation:
            _settle_duplicate_penalty(ref, settlement, trip, compensated)

    return settlement


def audit_window(
    ref: ReferenceData,
    rider_id: str,
    anchor_day: date,
) -> Settlement:
    """Whole-window sweep. NOT a settlement path -- its total must never be
    paid, because it includes shortfalls the rider did not raise. Used only to
    tell a vague complainant whether there is anything to talk about, and to
    give ops the full picture on an escalation."""
    settlement = Settlement(
        rider_id=rider_id,
        scope={"kind": "window_audit", "anchor_day": str(anchor_day), "advisory": True},
    )
    for offset in range(0, 8):
        day = date.fromordinal(anchor_day.toordinal() - offset)
        daily = settle_day(ref, rider_id, anchor_day, day)
        settlement.shortfall += daily.shortfall
        settlement.reasons.extend(daily.reasons)
    return settlement
