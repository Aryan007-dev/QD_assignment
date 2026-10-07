"""Policy arithmetic. Every figure here was confirmed against data/*.csv."""

from datetime import date

import pytest

from app.policy import (
    daily_incentive,
    expected_penalty,
    round_half_up,
    trip_fare,
    within_dispute_window,
)
from decimal import Decimal

D = date.fromisoformat


# (trip, km, surge, owed) -- all cross-checked against the real exports
FARES = [
    ("T926334", 6.0, 1.5, 74),
    ("T604546", 10.0, 1.2, 88),
    ("T252921", 5.5, 1.0, 46),
    ("T840677", 9.0, 1.5, 101),
    ("T312538", 5.0, 1.5, 65),   # 5000 metres, normalised upstream
    ("T672899", 7.4, 1.0, 57),
    ("T795007", 12.0, 1.0, 85),
    ("T206956", 13.0, 1.0, 91),
    ("T980582", 6.0, 1.5, 74),
    ("T990831", 6.5, 1.0, 52),
    ("T482410", 3.8, 1.0, 36),
    ("T502951", 5.0, 1.5, 65),
]


@pytest.mark.parametrize("trip,km,surge,owed", FARES)
def test_trip_fare(trip, km, surge, owed):
    assert trip_fare(km, surge) == owed, trip


def test_surge_applies_to_the_whole_fare_not_just_distance():
    """Policy: 'it applies to the whole trip fare (base + distance)'.

    6.0 km at 1.5x is (25 + 24) * 1.5 = 73.5 -> 74.
    The wrong reading, surging only the distance, gives 25 + 36 = 61.
    """
    assert trip_fare(6.0, 1.5) == 74
    assert trip_fare(6.0, 1.5) != 61


def test_first_two_km_are_free_and_never_negative():
    assert trip_fare(2.0, 1.0) == 25
    assert trip_fare(0.8, 1.0) == 25
    assert trip_fare(0.0, 1.0) == 25


def test_rounding_is_half_up_not_bankers():
    """Python's round() would send 74.5 to 74. The policy says 0.5 rounds up."""
    assert round_half_up(Decimal("73.5")) == 74
    assert round_half_up(Decimal("74.5")) == 75
    assert round_half_up(Decimal("74.4")) == 74
    assert round(74.5) == 74  # the trap we are avoiding


def test_daily_incentive_threshold():
    assert daily_incentive(11) == 0
    assert daily_incentive(12) == 150
    assert daily_incentive(13) == 150


def test_cancellation_penalty():
    assert expected_penalty(0) == 0
    assert expected_penalty(1) == 10
    assert expected_penalty(3) == 30


def test_dispute_window_is_seven_days_inclusive():
    anchor = D("2026-09-22")
    assert within_dispute_window(D("2026-09-22"), anchor) is True
    assert within_dispute_window(D("2026-09-15"), anchor) is True   # exactly 7
    assert within_dispute_window(D("2026-09-14"), anchor) is False  # 8
    assert within_dispute_window(D("2026-09-23"), anchor) is False  # future
