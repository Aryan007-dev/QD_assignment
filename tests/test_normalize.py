"""The three export faults, and the evidence for the metres one."""

import csv
from datetime import date
from pathlib import Path

import pytest

from app.normalize import (
    canonical_distance_km,
    canonical_rider_id,
    canonical_trip_id,
    conflicting_trip_ids,
    dedupe_trips,
    ist_day,
    normalize_trip_row,
)
from app.policy import trip_fare
from app.store import DATA_DIR


def test_rider_ids_are_canonicalised():
    assert canonical_rider_id("R7") == "R007"
    assert canonical_rider_id("r19") == "R019"
    assert canonical_rider_id("R003") == "R003"
    assert canonical_rider_id("R031") == "R031"


def test_unparseable_rider_id_is_not_guessed_at():
    """Better a loud lookup miss than a silent payment to the wrong rider."""
    assert canonical_rider_id("unknown") == "unknown"


def test_trip_ids_are_canonicalised():
    assert canonical_trip_id("t926334") == "T926334"
    assert canonical_trip_id("T 926334") == "T926334"
    assert canonical_trip_id("order T926334 ka") == "T926334"
    assert canonical_trip_id("no id here") == ""


def test_metres_are_converted_and_kilometres_are_left_alone():
    assert canonical_distance_km("5000") == 5.0
    assert canonical_distance_km("11400") == 11.4
    assert canonical_distance_km("800") == 0.8
    assert canonical_distance_km("6.0") == 6.0
    assert canonical_distance_km("21.0") == 21.0   # longest honest trip
    assert canonical_distance_km("0.8") == 0.8


def test_ist_day_bucketing():
    assert ist_day("2026-09-19T20:30:00Z") == date(2026, 9, 20)  # crosses midnight IST
    assert ist_day("2026-09-20T02:30:00Z") == date(2026, 9, 20)


def test_export_has_exactly_ten_duplicate_rows():
    with open(DATA_DIR / "trips.csv", newline="") as handle:
        rows = [normalize_trip_row(r) for r in csv.DictReader(handle)]
    unique = dedupe_trips(rows)
    assert len(rows) == 3314
    assert len(unique) == 3304


def test_no_duplicate_trip_id_carries_conflicting_contents():
    """Dedupe is keyed on the whole row. That is only safe because every
    duplicate in this export is byte-identical -- assert it, don't assume it."""
    with open(DATA_DIR / "trips.csv", newline="") as handle:
        rows = [normalize_trip_row(r) for r in csv.DictReader(handle)]
    assert conflicting_trip_ids(dedupe_trips(rows)) == []


def test_metres_reading_is_the_one_that_matches_the_ledger():
    """The evidence for treating >100 as metres, rather than asserting it.

    Of the 222 suspect rows, read as metres 202 of the 204 that were paid match
    the policy fare exactly. Read as kilometres, none do. The two exceptions
    are themselves planted underpayment bugs (T312538, T659741).
    """
    with open(DATA_DIR / "trips.csv", newline="") as handle:
        raw = list(csv.DictReader(handle))
    with open(DATA_DIR / "payout_lines.csv", newline="") as handle:
        paid = {
            r["trip_id"]: int(r["amount"])
            for r in csv.DictReader(handle)
            if r["line_type"] == "trip"
        }

    suspect = [r for r in raw if float(r["distance_km"]) > 100]
    assert len(suspect) == 222
    assert all(float(r["distance_km"]) % 100 == 0 for r in suspect)

    as_metres = as_km = considered = 0
    for row in suspect:
        amount = paid.get(row["trip_id"])
        if amount is None:
            continue
        considered += 1
        surge = float(row["surge_multiplier"])
        raw_km = float(row["distance_km"])
        as_metres += trip_fare(raw_km / 1000, surge) == amount
        as_km += trip_fare(raw_km, surge) == amount

    assert considered == 204
    assert as_metres == 202
    assert as_km == 0
