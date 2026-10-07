"""Durable store for agent state.

Write-through, not write-behind: the in-memory maps stay the working set so the
hot path keeps its simple locking, and every mutation is also appended to
Postgres. On start-up the tables are read back, so a restart does not lose a
rider's history, the ops queue, or the record of which shortfalls have already
been compensated -- the last of those matters most, because the payout exports
are a static snapshot and losing it would mean paying a settled dispute again.

With no DATABASE_URL, every function here is a no-op and the service runs purely
in memory. That is deliberate: the eval suite and CI must not need a database,
and a database being down should not stop the agent answering riders.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date

log = logging.getLogger("db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trace_steps (
    id          BIGSERIAL PRIMARY KEY,
    rider_id    TEXT NOT NULL,
    at          TEXT NOT NULL,
    step_type   TEXT NOT NULL,
    name        TEXT NOT NULL,
    payload     JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS trace_steps_rider ON trace_steps (rider_id, id);

CREATE TABLE IF NOT EXISTS handled_messages (
    message_id  TEXT PRIMARY KEY,
    rider_id    TEXT NOT NULL,
    reply       TEXT NOT NULL,
    handled_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ops_items (
    id          TEXT PRIMARY KEY,
    rider_id    TEXT NOT NULL,
    item_type   TEXT NOT NULL,
    amount      INTEGER,
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL,
    detail      JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ops_items_status ON ops_items (status, created_at);

CREATE TABLE IF NOT EXISTS compensated_components (
    rider_id    TEXT NOT NULL,
    component   TEXT NOT NULL,
    at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (rider_id, component)
);

CREATE TABLE IF NOT EXISTS autopay_log (
    rider_id    TEXT NOT NULL,
    day         DATE NOT NULL,
    dispute_id  TEXT NOT NULL,
    amount      INTEGER NOT NULL,
    payout_id   TEXT,
    PRIMARY KEY (rider_id, day, dispute_id)
);

CREATE TABLE IF NOT EXISTS conversations (
    rider_id    TEXT PRIMARY KEY,
    thread      JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class Persistence:
    """Thin wrapper so the agent never imports psycopg directly, and so a
    database failure degrades to in-memory rather than to an outage."""

    def __init__(self, database_url: str | None = None) -> None:
        self.url = database_url if database_url is not None else os.getenv("DATABASE_URL")
        self.enabled = False
        self._conn = None
        self._lock = threading.Lock()
        self._failures = 0
        if self.url:
            self._connect()

    def _connect(self) -> None:
        """Connect and create the schema. Any failure degrades to in-memory.

        One connection guarded by a lock, not a pool: writes here are small,
        off the critical path for correctness, and must never be the reason a
        rider does not get an answer.
        """
        try:
            import psycopg
        except ImportError:
            log.info("psycopg not installed; running in memory only")
            return

        try:
            self._conn = psycopg.connect(self.url, autocommit=True, connect_timeout=5)
            with self._conn.cursor() as cursor:
                cursor.execute(SCHEMA)
            self.enabled = True
            log.info("persistence enabled")
        except Exception as exc:  # noqa: BLE001
            log.warning("could not reach the database, running in memory only: %s", exc)
            self.enabled = False

    def _execute(self, sql: str, params: tuple = ()) -> None:
        if not self.enabled:
            return
        try:
            with self._lock, self._conn.cursor() as cursor:
                cursor.execute(sql, params)
        except Exception as exc:  # noqa: BLE001
            self._failures += 1
            if self._failures <= 3:
                log.warning("write failed, continuing in memory: %s", exc)

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        if not self.enabled:
            return []
        try:
            with self._lock, self._conn.cursor() as cursor:
                cursor.execute(sql, params)
                return cursor.fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("read failed: %s", exc)
            return []

    # -- writes ----------------------------------------------------------

    def save_steps(self, rider_id: str, steps: list[dict]) -> None:
        for step in steps:
            self._execute(
                "INSERT INTO trace_steps (rider_id, at, step_type, name, payload) "
                "VALUES (%s, %s, %s, %s, %s)",
                (rider_id, step["at"], step["type"], step["name"], json.dumps(step)),
            )

    def save_handled_message(self, message_id: str, rider_id: str, reply: str) -> None:
        self._execute(
            "INSERT INTO handled_messages (message_id, rider_id, reply) VALUES (%s, %s, %s) "
            "ON CONFLICT (message_id) DO NOTHING",
            (message_id, rider_id, reply),
        )

    def save_ops_item(self, item) -> None:
        self._execute(
            "INSERT INTO ops_items (id, rider_id, item_type, amount, reason, created_at, status, detail) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, detail = EXCLUDED.detail",
            (
                item.id,
                item.rider_id,
                item.type,
                item.amount,
                item.reason,
                item.created_at,
                item.status,
                json.dumps(item.detail, default=str),
            ),
        )

    def save_compensated(self, rider_id: str, components: set[str]) -> None:
        for component in components:
            self._execute(
                "INSERT INTO compensated_components (rider_id, component) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (rider_id, component),
            )

    def release_compensated(self, rider_id: str, components: set[str]) -> None:
        for component in components:
            self._execute(
                "DELETE FROM compensated_components WHERE rider_id = %s AND component = %s",
                (rider_id, component),
            )

    def save_autopay(self, rider_id: str, day: date, entry: dict) -> None:
        self._execute(
            "INSERT INTO autopay_log (rider_id, day, dispute_id, amount, payout_id) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (rider_id, day, entry.get("dispute_id"), entry.get("amount"), entry.get("payout_id")),
        )

    def save_conversation(self, rider_id: str, thread: dict) -> None:
        self._execute(
            "INSERT INTO conversations (rider_id, thread) VALUES (%s, %s) "
            "ON CONFLICT (rider_id) DO UPDATE SET thread = EXCLUDED.thread, updated_at = now()",
            (rider_id, json.dumps(thread, default=str)),
        )

    def clear_all(self) -> None:
        """Used by POST /admin/reset so a fresh run really is fresh."""
        for table in (
            "trace_steps",
            "handled_messages",
            "ops_items",
            "compensated_components",
            "autopay_log",
            "conversations",
        ):
            self._execute(f"DELETE FROM {table}")

    # -- rehydrate --------------------------------------------------------

    def load_into(self, state) -> dict:
        """Restore the working set after a restart."""
        if not self.enabled:
            return {"restored": False}

        from app.store import OpsItem

        counts = {"steps": 0, "messages": 0, "ops_items": 0, "compensated": 0, "conversations": 0}

        with state.lock:
            for rider_id, payload in self._query(
                "SELECT rider_id, payload FROM trace_steps ORDER BY id"
            ):
                state.traces[rider_id].append(
                    payload if isinstance(payload, dict) else json.loads(payload)
                )
                counts["steps"] += 1

            for message_id, reply in self._query(
                "SELECT message_id, reply FROM handled_messages"
            ):
                state.handled_messages[message_id] = reply
                counts["messages"] += 1

            for row in self._query(
                "SELECT id, rider_id, item_type, amount, reason, created_at, status, detail "
                "FROM ops_items"
            ):
                detail = row[7] if isinstance(row[7], dict) else json.loads(row[7])
                state.ops_items[row[0]] = OpsItem(
                    id=row[0],
                    rider_id=row[1],
                    type=row[2],
                    amount=row[3],
                    reason=row[4],
                    created_at=row[5],
                    status=row[6],
                    detail=detail,
                )
                counts["ops_items"] += 1
                if row[6] == "pending" and detail.get("components"):
                    state.reserved_by_item[row[0]] = (row[1], set(detail["components"]))

            for rider_id, component in self._query(
                "SELECT rider_id, component FROM compensated_components"
            ):
                state.compensated[rider_id].add(component)
                counts["compensated"] += 1

            for rider_id, day, dispute_id, amount, payout_id in self._query(
                "SELECT rider_id, day, dispute_id, amount, payout_id FROM autopay_log"
            ):
                # Only confirmed payouts are ever written to this table, so a
                # rehydrated row is confirmed. Without this the reconciliation
                # view cannot tell "we never got an answer" from "the ledger no
                # longer has it", which are very different problems for ops.
                state.autopay_log[(rider_id, day)].append(
                    {
                        "dispute_id": dispute_id,
                        "amount": amount,
                        "payout_id": payout_id,
                        "confirmed": True,
                    }
                )

            for rider_id, thread in self._query("SELECT rider_id, thread FROM conversations"):
                state.conversations[rider_id] = (
                    thread if isinstance(thread, dict) else json.loads(thread)
                )
                counts["conversations"] += 1

            if state.ops_items:
                highest = max(int(i[3:]) for i in state.ops_items if i.startswith("OPS"))
                state.ops_seq = highest

        counts["restored"] = True
        return counts
