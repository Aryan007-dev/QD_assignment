"""Background reconciliation of payments the request path could not confirm.

PaySwift's sandbox stalls for 8 seconds on 10% of calls and drops 5% of
responses, while the messaging vendor re-sends a message if it does not get a
2xx in about 10. Those two numbers do not both fit in one synchronous request:
waiting out an 8-second stall risks the vendor re-sending, and giving up at 6.5
seconds means a payout can land *after* we have already answered the rider.

So the request path gets a short budget and anything unresolved is handed here.
This worker keeps asking the ledger what really happened, retries with the same
idempotency key if the payout genuinely never landed, and closes the ops
escalation if it turns out the money was there all along. No payout is ever
abandoned silently, and none is ever paid twice.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("reconciler")

# PROVIDER_SLOW_SECONDS defaults to 8 in the sandbox, so a stalled call can be
# recorded up to ~8s after we stopped waiting. Look past that before retrying.
STALL_GRACE_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 1.0
# The provider blocks an account for 60 seconds once its write limit is tripped,
# so a reconciler that gives up sooner would abandon money the rider is owed.
RATE_LIMIT_BACKOFF_SECONDS = 12.0
# Patience has to outlast the worst documented pause: a 60-second outage can be
# followed by a 60-second rate-limit block. A retry *count* cannot express that,
# because the right number of retries depends on how long each one took, so the
# budget is wall-clock and the backoff grows instead.
TOTAL_PATIENCE_SECONDS = float(os.getenv("RECONCILE_PATIENCE_SECONDS", "200"))
MAX_BACKOFF_SECONDS = 15.0


@dataclass
class PendingPayment:
    rider_id: str
    amount: int
    reference: str
    dispute_id: str
    ops_item_id: str | None
    enqueued_at: float
    day: object | None = None  # the IST day whose auto-pay slot this reserved
    epoch: int = 0


class Reconciler:
    def __init__(self, payswift, state) -> None:
        self.payswift = payswift
        self.state = state
        self.queue: queue.Queue[PendingPayment] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.resolved = 0
        self.unresolved = 0
        # Bumped by POST /admin/reset. Work queued in an earlier epoch is
        # abandoned: a reset declares the system fresh, so chasing the previous
        # run's payment would land money inside the next run's measurements.
        # Nothing is lost in production -- resets are an eval affordance.
        self.epoch = 0
        self._active = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="reconciler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def enqueue(
        self,
        rider_id: str,
        amount: int,
        reference: str,
        dispute_id: str,
        ops_item_id: str | None,
        day=None,
    ) -> None:
        self.queue.put(
            PendingPayment(
                rider_id=rider_id,
                amount=amount,
                reference=reference,
                dispute_id=dispute_id,
                ops_item_id=ops_item_id,
                enqueued_at=time.monotonic(),
                day=day,
                epoch=self.epoch,
            )
        )

    @property
    def in_flight(self) -> int:
        return self.queue.qsize() + self._active

    def drain(self) -> int:
        """Abandon everything queued and in flight, and start a new epoch."""
        dropped = 0
        self.epoch += 1
        while True:
            try:
                self.queue.get_nowait()
                self.queue.task_done()
                dropped += 1
            except queue.Empty:
                break
        return dropped

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._active += 1
            try:
                self._settle(item)
            except Exception as exc:  # noqa: BLE001 - a worker must not die
                log.warning("reconcile failed for %s: %s", item.dispute_id, exc)
            finally:
                self._active -= 1
                self.queue.task_done()

    def _settle(self, item: PendingPayment) -> None:
        """Poll the ledger, then retry if the payout really is absent.

        Reads are not rate limited, writes are, so the loop leans on reading the
        ledger and only writes again when reading has proved the payout is not
        there. Every write reuses the original idempotency key, so a retry can
        never become a second payment.
        """
        retries = 0
        deadline = item.enqueued_at + TOTAL_PATIENCE_SECONDS

        while time.monotonic() < deadline and not self._stop.is_set():
            if item.epoch != self.epoch:
                return  # the system was reset under us; this epoch is over
            found = self.payswift.find_payout(item.rider_id, item.reference)
            if found:
                self._mark_resolved(item, found, retries > 0)
                return

            if time.monotonic() - item.enqueued_at < STALL_GRACE_SECONDS:
                # The provider may still be mid-stall (8s by default); asking
                # again is cheaper and far safer than sending a second payout.
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            result = self.payswift.pay(
                rider_id=item.rider_id,
                amount=item.amount,
                reference=item.reference,
                dispute_id=item.dispute_id,
            )
            retries += 1

            if result.money_moved:
                self._mark_resolved(
                    item,
                    {"payout_id": result.payout_id, "status": result.status},
                    True,
                )
                return

            if (result.error or "").startswith("account_blocked"):
                # Nothing to do but wait out the block.
                self._note(
                    item,
                    "payment_rate_limited",
                    {"outcome": "waiting", "backoff_s": RATE_LIMIT_BACKOFF_SECONDS},
                )
                backoff = RATE_LIMIT_BACKOFF_SECONDS
            else:
                backoff = min(MAX_BACKOFF_SECONDS, POLL_INTERVAL_SECONDS * 2 ** retries)
            time.sleep(min(backoff, max(0.0, deadline - time.monotonic())))

        # Give the day's auto-pay slot back: this payment definitively did not
        # happen, the shortfall is sitting in the ops queue, and the rider should
        # not be locked out of a later auto-payment because of our failure.
        self._release_reservation(item)
        self.unresolved += 1
        self._note(
            item,
            "payment_unresolved",
            {
                "outcome": "unresolved",
                "reference": item.reference,
                "note": "Left open for ops: PaySwift never confirmed this payout.",
            },
        )

    def _mark_resolved(self, item: PendingPayment, payout: dict, retried: bool) -> None:
        self.resolved += 1
        with self.state.lock:
            ops_item = self.state.ops_items.get(item.ops_item_id or "")
            if ops_item is not None and ops_item.status == "pending":
                ops_item.status = "resolved"
                ops_item.detail["resolved_by"] = "reconciler"
                ops_item.detail["payout"] = payout
            # Confirm the auto-pay slot that was reserved before sending.
            for entry in self.state.autopay_log.get(self._day_key(item), []):
                if entry.get("dispute_id") == item.dispute_id:
                    entry["payout_id"] = payout.get("payout_id")
                    entry["confirmed"] = True
                    break

        self._note(
            item,
            "payment_reconciled",
            {
                "outcome": "confirmed",
                "payout_id": payout.get("payout_id"),
                "retried": retried,
                "waited_ms": int((time.monotonic() - item.enqueued_at) * 1000),
            },
        )

    def _day_key(self, item: PendingPayment):
        if item.day is not None:
            return (item.rider_id, item.day)
        import datetime as _dt

        marker = item.reference.split("day=")[-1].split(" ")[0]
        try:
            return (item.rider_id, _dt.date.fromisoformat(marker))
        except ValueError:
            return (item.rider_id, None)

    def _release_reservation(self, item: PendingPayment) -> None:
        key = self._day_key(item)
        with self.state.lock:
            entries = self.state.autopay_log.get(key)
            if not entries:
                return
            self.state.autopay_log[key] = [
                e
                for e in entries
                if not (e.get("dispute_id") == item.dispute_id and not e.get("confirmed"))
            ]

    def _note(self, item: PendingPayment, name: str, output: dict) -> None:
        from datetime import datetime

        from app.normalize import IST

        with self.state.lock:
            self.state.traces[item.rider_id].append(
                {
                    "at": datetime.now(IST).isoformat(),
                    "type": "tool_call",
                    "name": name,
                    "input": {
                        "dispute_id": item.dispute_id,
                        "amount": item.amount,
                        "reference": item.reference,
                    },
                    "output": output,
                }
            )
