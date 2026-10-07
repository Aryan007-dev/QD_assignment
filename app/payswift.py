"""PaySwift client. PaySwift is the ledger of record, not our database.

The sandbox is hostile by design, and its own defaults say how hostile:
PROVIDER_ERROR_RATE=0.15, PROVIDER_SLOW_RATE=0.10, PROVIDER_SLOW_SECONDS=8,
PROVIDER_LOST_RESPONSE_RATE=0.05, plus rate limiting, 409 on an idempotency key
reused with a different body, and a 409 while a key is still in flight.

Two consequences shape this module:

1. A lost response means the payout *did* happen and we did not hear about it.
   So a failed attempt is never evidence that nothing was paid: we reconcile by
   reading the ledger back before concluding anything.

2. Slow responses take 8 seconds and the messaging vendor re-sends a message if
   it does not get a 2xx in about 10. So the whole payment attempt lives inside
   a budget well under that, and running out of budget leaves the payout in a
   pending state for ops rather than blocking the reply.
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import date

import httpx

log = logging.getLogger("payswift")

DEFAULT_BASE_URL = os.getenv("PAYSWIFT_BASE_URL", "http://localhost:8081")

# Total wall-clock budget for one payout, including retries. Must stay well
# under the messaging vendor's ~10s re-send window.
TOTAL_BUDGET_SECONDS = float(os.getenv("PAYSWIFT_BUDGET_SECONDS", "6.5"))
ATTEMPT_TIMEOUT_SECONDS = float(os.getenv("PAYSWIFT_ATTEMPT_TIMEOUT", "2.5"))
MAX_ATTEMPTS = int(os.getenv("PAYSWIFT_MAX_ATTEMPTS", "4"))

# 429 is deliberately absent: the sandbox answers "account_blocked" and keeps
# answering it for 60 seconds, which no in-request retry can outlast. Retrying
# through a block only deepens it, so we fail fast and hand the payout to the
# background reconciler instead.
RETRYABLE_STATUS = {408, 409, 425, 500, 502, 503, 504}
RATE_LIMITED_STATUS = 429

# The provider allows roughly 30 writes per 10 seconds before blocking an account
# for a minute. Staying under that is cheaper than recovering from it, so every
# write passes a shared token bucket first.
WRITE_RATE_PER_SECOND = float(os.getenv("PAYSWIFT_WRITE_RATE", "2.0"))
WRITE_BURST = int(os.getenv("PAYSWIFT_WRITE_BURST", "8"))


class TokenBucket:
    """Shared across threads: the provider's limit is per account, not per caller."""

    def __init__(self, rate_per_second: float, burst: int) -> None:
        self.rate = rate_per_second
        self.capacity = float(burst)
        self._tokens = float(burst)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def take(self, timeout: float) -> bool:
        """Spend one token, waiting up to `timeout` seconds for one to refill."""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._updated) * self.rate
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                shortfall = (1.0 - self._tokens) / self.rate
            if time.monotonic() + shortfall > deadline:
                return False
            time.sleep(min(shortfall, max(0.0, deadline - time.monotonic())))


_write_bucket = TokenBucket(WRITE_RATE_PER_SECOND, WRITE_BURST)


class PayoutOutcome:
    PAID = "paid"
    ALREADY_PAID = "already_paid"
    REJECTED = "rejected"
    UNKNOWN = "unknown"  # may or may not have landed; ops must reconcile


@dataclass
class PayoutResult:
    outcome: str
    amount: int
    rider_id: str
    reference: str
    payout_id: str | None = None
    status: str | None = None
    attempts: int = 0
    idempotency_key: str = ""
    error: str | None = None
    timeline: list[dict] = field(default_factory=list)

    @property
    def money_moved(self) -> bool:
        return self.outcome in (PayoutOutcome.PAID, PayoutOutcome.ALREADY_PAID)

    def as_trace(self) -> dict:
        return {
            "outcome": self.outcome,
            "amount": self.amount,
            "rider_id": self.rider_id,
            "reference": self.reference,
            "payout_id": self.payout_id,
            "status": self.status,
            "attempts": self.attempts,
            "idempotency_key": self.idempotency_key,
            "error": self.error,
            "timeline": self.timeline,
        }


def idempotency_key(rider_id: str, dispute_id: str, amount: int) -> str:
    """Stable across retries, distinct across disputes.

    The amount is part of the key on purpose: the sandbox returns 409
    'Idempotency-Key was used with a different request body' if a key is reused
    with a different amount, so binding them keeps a recomputed amount from
    colliding with an earlier attempt.
    """
    digest = hashlib.sha256(f"{rider_id}|{dispute_id}|{amount}".encode()).hexdigest()
    return f"qd-{digest[:32]}"


class PaySwiftClient:
    def __init__(self, base_url: str | None = None, client: httpx.Client | None = None):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._client = client or httpx.Client(timeout=ATTEMPT_TIMEOUT_SECONDS)

    def close(self) -> None:
        self._client.close()

    # -- reads -----------------------------------------------------------

    def health(self) -> bool:
        try:
            return self._client.get(f"{self.base_url}/health", timeout=2.0).status_code == 200
        except httpx.HTTPError:
            return False

    def list_payouts(self, rider_id: str | None = None) -> list[dict]:
        """Read the ledger. This is the authority on what has been paid."""
        params = {"rider_id": rider_id} if rider_id else {}
        try:
            response = self._client.get(
                f"{self.base_url}/v1/payouts", params=params, timeout=3.0
            )
            if response.status_code != 200:
                return []
            body = response.json()
        except (httpx.HTTPError, ValueError):
            return []
        if isinstance(body, dict):
            for key in ("payouts", "data", "items", "results"):
                if isinstance(body.get(key), list):
                    return body[key]
            return []
        return body if isinstance(body, list) else []

    def find_payout(self, rider_id: str, reference: str) -> dict | None:
        """Has this exact dispute already been paid? Used to recover from a lost
        response, and to decide whether today's auto-payment is spent."""
        for payout in self.list_payouts(rider_id):
            if str(payout.get("reference", "")) == reference:
                return payout
        return None

    def autopay_count_for_day(
        self,
        rider_id: str,
        day: date,
        marker: str,
        exclude_payout_ids: set[str] | None = None,
    ) -> int:
        """How many agent auto-payments this rider has had on `day`.

        Counted from the ledger rather than from local state, so a wiped database
        or a lost response cannot hand out a second auto-payment.

        `exclude_payout_ids` carries the epoch baseline: payouts that were already
        in the ledger before this run began. PaySwift's ledger is in memory and
        clears only on restart, so without the baseline a reset service would
        inherit a previous run's payments and refuse to auto-pay.
        """
        skip = exclude_payout_ids or set()
        count = 0
        for payout in self.list_payouts(rider_id):
            if str(payout.get("payout_id", "")) in skip:
                continue
            reference = str(payout.get("reference", ""))
            if marker in reference and f"day={day.isoformat()}" in reference:
                count += 1
        return count

    def all_payout_ids(self) -> set[str]:
        """Every payout id currently in the ledger, for the epoch baseline."""
        return {str(p.get("payout_id", "")) for p in self.list_payouts() if p.get("payout_id")}

    # -- write -----------------------------------------------------------

    def pay(
        self,
        rider_id: str,
        amount: int,
        reference: str,
        dispute_id: str,
    ) -> PayoutResult:
        """Send one payout, idempotently, inside a bounded time budget."""
        key = idempotency_key(rider_id, dispute_id, amount)
        result = PayoutResult(
            outcome=PayoutOutcome.UNKNOWN,
            amount=amount,
            rider_id=rider_id,
            reference=reference,
            idempotency_key=key,
        )

        # If a previous attempt landed and we never heard back, do not send again.
        existing = self.find_payout(rider_id, reference)
        if existing:
            result.outcome = PayoutOutcome.ALREADY_PAID
            result.payout_id = existing.get("payout_id")
            result.status = existing.get("status")
            result.timeline.append({"step": "pre_check", "found": True})
            return result

        deadline = time.monotonic() + TOTAL_BUDGET_SECONDS
        payload = {"rider_id": rider_id, "amount": int(amount), "reference": reference}

        for attempt in range(1, MAX_ATTEMPTS + 1):
            result.attempts = attempt
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result.error = "time_budget_exhausted"
                break

            # Stay inside the provider's write limit rather than discovering it.
            if not _write_bucket.take(timeout=max(0.0, remaining - 0.2)):
                result.error = "local_rate_limit_wait_exceeded_budget"
                break

            started = time.monotonic()
            try:
                response = self._client.post(
                    f"{self.base_url}/v1/payouts",
                    json=payload,
                    headers={"Idempotency-Key": key, "Content-Type": "application/json"},
                    timeout=min(ATTEMPT_TIMEOUT_SECONDS, remaining),
                )
            except httpx.HTTPError as exc:
                # Timeout or dropped response. The payout may still have landed.
                result.timeline.append(
                    {
                        "step": "attempt",
                        "n": attempt,
                        "error": type(exc).__name__,
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                    }
                )
                result.error = type(exc).__name__
                if not self._sleep_before_retry(attempt, deadline):
                    break
                continue

            entry = {
                "step": "attempt",
                "n": attempt,
                "status_code": response.status_code,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }
            result.timeline.append(entry)

            if response.status_code in (200, 201):
                body = self._json(response)
                result.outcome = PayoutOutcome.PAID
                result.payout_id = body.get("payout_id")
                result.status = body.get("status")
                result.error = None
                return result

            if response.status_code == 400:
                body = self._json(response)
                result.outcome = PayoutOutcome.REJECTED
                result.error = body.get("error") or "bad_request"
                return result

            if response.status_code == RATE_LIMITED_STATUS:
                body = self._json(response)
                entry["error"] = body.get("error")
                result.error = body.get("error") or "rate_limited"
                result.timeline.append(
                    {"step": "give_up_fast", "why": "provider_block_outlasts_request_budget"}
                )
                break

            if response.status_code in RETRYABLE_STATUS:
                body = self._json(response)
                entry["error"] = body.get("error")
                result.error = body.get("error") or f"http_{response.status_code}"
                if not self._sleep_before_retry(attempt, deadline):
                    break
                continue

            result.error = f"http_{response.status_code}"
            break

        # Out of attempts or out of budget: ask the ledger what actually happened.
        settled = self.find_payout(rider_id, reference)
        if settled:
            result.outcome = PayoutOutcome.ALREADY_PAID
            result.payout_id = settled.get("payout_id")
            result.status = settled.get("status")
            result.timeline.append({"step": "reconcile", "found": True})
        else:
            result.outcome = PayoutOutcome.UNKNOWN
            result.timeline.append({"step": "reconcile", "found": False})
        return result

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _json(response: httpx.Response) -> dict:
        try:
            body = response.json()
            return body if isinstance(body, dict) else {}
        except ValueError:
            return {}

    @staticmethod
    def _sleep_before_retry(attempt: int, deadline: float) -> bool:
        """Exponential backoff with jitter, clipped to the remaining budget.

        Jitter matters because the vendor retries too: without it, our retry and
        the vendor's re-sent message hit the provider in lockstep.
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0.05:
            return False
        delay = min(0.2 * (2 ** (attempt - 1)), 1.5)
        delay = min(delay * (0.5 + random.random()), remaining - 0.05)
        if delay <= 0:
            return False
        time.sleep(delay)
        return True


def payout_reference(dispute_id: str, rider_id: str, day: date, rules: list[str]) -> str:
    """Human-readable and machine-parseable: ops reads it in the PaySwift
    dashboard, and autopay_count_for_day() parses it back out."""
    rule_text = ",".join(rules)[:60] if rules else "dispute"
    return f"qd-agent dispute={dispute_id} rider={rider_id} day={day.isoformat()} rules={rule_text}"
