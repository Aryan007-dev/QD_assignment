"""Payout policy, transcribed from docs/policy.md.

Pure arithmetic. No I/O, no model calls, no state. This module is the only
place rupee amounts are produced.
"""

from decimal import Decimal, ROUND_HALF_UP

BASE_FARE = Decimal("25")
PER_KM_RATE = Decimal("6")
FREE_KM = Decimal("2")

DAILY_INCENTIVE = 150
INCENTIVE_MIN_TRIPS = 12
CANCELLATION_PENALTY = 10

# Finance: auto-pay up to Rs 200 per dispute, once per rider per day.
AUTO_PAY_MAX = 200
AUTO_PAY_PER_RIDER_PER_DAY = 1

# Ops wiki: we only look at disputes for the last 7 days.
DISPUTE_WINDOW_DAYS = 7

# PaySwift accepts whole rupees, 1 to 10000.
PAYSWIFT_MIN_AMOUNT = 1
PAYSWIFT_MAX_AMOUNT = 10000


def round_half_up(value: Decimal) -> int:
    """Round to the nearest rupee, 0.5 up.

    Not round(): Python uses banker's rounding, which sends 74.5 to 74.
    The policy says 0.5 rounds up, so 74.5 must become 75.
    """
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def trip_fare(distance_km: float | Decimal, surge_multiplier: float | Decimal) -> int:
    """Rs 25 base + Rs 6 per km after the first 2 km, surge applied to the whole fare.

    Surge multiplies base + distance together, not the distance component alone:
    6.0 km at 1.5x is (25 + 6*4) * 1.5 = 73.5 -> 74, not 25 + (6*4*1.5) = 61.
    """
    km = Decimal(str(distance_km))
    surge = Decimal(str(surge_multiplier))
    billable_km = max(Decimal("0"), km - FREE_KM)
    return round_half_up((BASE_FARE + PER_KM_RATE * billable_km) * surge)


def daily_incentive(completed_trips: int) -> int:
    """Rs 150 for 12 or more completed trips in an IST calendar day."""
    return DAILY_INCENTIVE if completed_trips >= INCENTIVE_MIN_TRIPS else 0


def expected_penalty(rider_cancellations: int) -> int:
    """Rs 10 for every trip the rider cancels. Customer cancellations earn nothing."""
    return CANCELLATION_PENALTY * rider_cancellations


def within_dispute_window(trip_day, anchor_day) -> bool:
    """Is trip_day inside the 7-day window ending at anchor_day?

    anchor_day comes from the message's received_at (event time), never from
    the wall clock: the exports are September 2026 and a service running later
    would otherwise consider every trip stale.
    """
    delta = (anchor_day - trip_day).days
    return 0 <= delta <= DISPUTE_WINDOW_DAYS
