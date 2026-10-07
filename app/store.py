"""Reference data (trips, payout lines, riders) and mutable agent state.

Both live behind narrow accessors so the Postgres-backed implementation can
replace the bodies without the dispute engine noticing. Reference data is
normalised once on ingest (see app/normalize.py) -- nothing downstream ever
sees a raw export row.
"""

from __future__ import annotations

import csv
import threading
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from app.normalize import (
    LINE_INCENTIVE,
    LINE_PENALTY,
    LINE_TRIP,
    PayoutLine,
    Trip,
    canonical_rider_id,
    dedupe_trips,
    normalize_payout_line_row,
    normalize_trip_row,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@dataclass(frozen=True)
class Rider:
    rider_id: str
    name: str
    city: str


class ReferenceData:
    """Read-only view of the trips system and the payout ledger exports."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or DATA_DIR
        self.riders: dict[str, Rider] = {}
        self.trips_by_id: dict[str, Trip] = {}
        self.trips_by_rider_day: dict[tuple[str, date], list[Trip]] = defaultdict(list)
        self.trips_by_rider: dict[str, list[Trip]] = defaultdict(list)
        self.trip_payment: dict[str, int] = {}
        self.penalty_count: dict[str, int] = defaultdict(int)
        self.incentive_days: set[tuple[str, date]] = set()
        self.ingest_notes: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        with open(self.data_dir / "riders.csv", newline="") as handle:
            for row in csv.DictReader(handle):
                rider = Rider(
                    rider_id=canonical_rider_id(row["rider_id"]),
                    name=row["name"].strip(),
                    city=row["city"].strip(),
                )
                self.riders[rider.rider_id] = rider

        with open(self.data_dir / "trips.csv", newline="") as handle:
            raw_trips = [normalize_trip_row(row) for row in csv.DictReader(handle)]
        trips = dedupe_trips(raw_trips)
        self.ingest_notes["trip_rows_raw"] = len(raw_trips)
        self.ingest_notes["trip_rows_deduped"] = len(trips)
        self.ingest_notes["duplicate_rows_dropped"] = len(raw_trips) - len(trips)

        for trip in trips:
            self.trips_by_id[trip.trip_id] = trip
            self.trips_by_rider_day[(trip.rider_id, trip.day)].append(trip)
            self.trips_by_rider[trip.rider_id].append(trip)

        with open(self.data_dir / "payout_lines.csv", newline="") as handle:
            lines = [normalize_payout_line_row(row) for row in csv.DictReader(handle)]
        self.ingest_notes["payout_lines"] = len(lines)

        for line in lines:
            if line.line_type == LINE_TRIP:
                self.trip_payment[line.trip_id] = line.amount
            elif line.line_type == LINE_PENALTY:
                self.penalty_count[line.trip_id] += 1
            elif line.line_type == LINE_INCENTIVE:
                self.incentive_days.add((line.rider_id, line.payout_date))

    # -- accessors -------------------------------------------------------

    def rider_exists(self, rider_id: str) -> bool:
        return rider_id in self.riders

    def trip(self, trip_id: str) -> Trip | None:
        return self.trips_by_id.get(trip_id)

    def trips_on(self, rider_id: str, day: date) -> list[Trip]:
        return list(self.trips_by_rider_day.get((rider_id, day), []))

    def trips_of(self, rider_id: str) -> list[Trip]:
        return list(self.trips_by_rider.get(rider_id, []))

    def amount_paid_for_trip(self, trip_id: str) -> int | None:
        return self.trip_payment.get(trip_id)

    def penalties_charged_for_trip(self, trip_id: str) -> int:
        return self.penalty_count.get(trip_id, 0)

    def incentive_was_paid(self, rider_id: str, day: date) -> bool:
        return (rider_id, day) in self.incentive_days


# -- mutable agent state -------------------------------------------------


@dataclass
class OpsItem:
    id: str
    rider_id: str
    type: str  # "approval" | "escalation"
    amount: int | None
    reason: str
    created_at: str
    status: str = "pending"  # pending | approved | rejected
    detail: dict = field(default_factory=dict)

    def as_api(self) -> dict:
        return {
            "id": self.id,
            "rider_id": self.rider_id,
            "type": self.type,
            "amount": self.amount,
            "reason": self.reason,
            "created_at": self.created_at,
            "status": self.status,
            "detail": self.detail,
        }


class AgentState:
    """Everything the agent remembers. Guarded by a lock: the messaging vendor
    retries, so the same message can arrive concurrently with the original."""

    def __init__(self, persistence=None) -> None:
        self.lock = threading.RLock()
        # Set by app.main at start-up. None means in-memory only, which is how
        # CI and the eval suite run.
        self.persistence = persistence
        self.ops_seq = 0
        self.traces: dict[str, list[dict]] = defaultdict(list)
        self.handled_messages: dict[str, str] = {}  # message_id -> reply sent
        self.ops_items: dict[str, OpsItem] = {}
        self.conversations: dict[str, dict] = {}
        self.autopay_log: dict[tuple[str, date], list[dict]] = defaultdict(list)
        # rider_id -> component keys already paid or queued for approval. The
        # payout exports are a static snapshot, so this is the only record that a
        # shortfall has been settled.
        self.compensated: dict[str, set[str]] = defaultdict(set)
        # ops item id -> the component keys it reserved, so a rejection releases them.
        self.reserved_by_item: dict[str, tuple[str, set[str]]] = {}
        self.dispute_seq = 0
        # Identifies this run of the service. It changes on every reset, and it
        # goes into every dispute id, so a payout reference from a previous run
        # can never be mistaken for this run's -- otherwise a fresh system would
        # look up an old payout, conclude the dispute was already settled, and
        # quietly pay the rider nothing.
        self.run_id = uuid.uuid4().hex[:8]
        # Payout ids that already existed in PaySwift when this epoch began.
        # PaySwift's ledger is in memory and is only cleared when it restarts, so
        # without this a reset would inherit another run's auto-payments and
        # wrongly conclude a rider has spent today's auto-pay budget.
        self.ledger_epoch: set[str] = set()

    def reset(self, ledger_epoch: set[str] | None = None) -> None:
        with self.lock:
            self.traces.clear()
            self.handled_messages.clear()
            self.ops_items.clear()
            self.conversations.clear()
            self.autopay_log.clear()
            self.compensated.clear()
            self.reserved_by_item.clear()
            self.dispute_seq = 0
            self.ops_seq = 0
            self.run_id = uuid.uuid4().hex[:8]
            self.ledger_epoch = set(ledger_epoch or ())
        if self.persistence is not None:
            self.persistence.clear_all()

    def next_dispute_id(self, rider_id: str) -> str:
        with self.lock:
            self.dispute_seq += 1
            return f"D{self.dispute_seq:05d}-{rider_id}-{self.run_id}"
