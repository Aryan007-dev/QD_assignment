"""Failure modes the conversation format cannot express.

data/conversations.json replays turns one after another, so it cannot describe a
race, a provider outage or a rate-limit storm. Those are exactly the conditions
this system is built for, so they get tested here instead -- through the same
public interface, so these still run against any implementation.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

PAYSWIFT_ADMIN_OUTAGE_SECONDS = 60  # the sandbox's /admin/outage is fixed at 60s


def _quiesce(harness, timeout: float = 30.0) -> None:
    """Wait until the service reports nothing left to reconcile, then reset.

    Scenarios reuse riders, so a payment still being chased from the previous
    scenario would land inside this one's measurement window.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            body = harness.http.get(f"{harness.service}/health", timeout=3.0).json()
            if int(body.get("payments_reconciling", 0)) == 0:
                break
        except Exception:  # noqa: BLE001
            break
        time.sleep(1.0)
    harness.reset()


def _settle_wait(harness, rider_id: str, want: int, timeout: float = 45.0) -> int:
    """Poll the ledger until the expected amount has arrived, or time out."""
    before, _ = harness.paid_total(rider_id)
    deadline = time.time() + timeout
    while time.time() < deadline:
        total, _ = harness.paid_total(rider_id)
        if total - before >= want:
            return total - before
        time.sleep(1.0)
    total, _ = harness.paid_total(rider_id)
    return total - before


def concurrent_duplicate_delivery(harness) -> dict:
    """The vendor re-sends when it does not get a 2xx in ~10s, and the re-send can
    arrive while the original is still being processed. A read-then-write dedupe
    loses this race and pays twice."""
    rider = "R003"
    _quiesce(harness)
    before, before_ids = harness.paid_total(rider)

    turn = {
        "message_id": "wamid.RACE_1",
        "text": "order T926334 ka surge nahi mila",
        "received_at": "2026-09-22T09:00:00+05:30",
    }

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(harness.send, turn, rider) for _ in range(6)]
        results = [f.result() for f in futures]

    # A stalled provider can confirm after the reply, so wait for the ledger.
    _settle_wait(harness, rider, want=25, timeout=60.0)
    after, after_ids = harness.paid_total(rider)
    new_payouts = len(after_ids - before_ids)
    replies = [r[0] for r in results]

    failures = []
    if after - before != 25:
        failures.append(f"paid {after - before}, expected exactly 25")
    if new_payouts != 1:
        failures.append(f"{new_payouts} payouts created, expected exactly 1")
    if any(not r.strip() for r in replies):
        failures.append("at least one concurrent delivery got an empty reply")
    pending = [
        p
        for p in harness.pending()
        if p.get("rider_id") == rider and p.get("type") == "approval"
    ]
    if pending:
        failures.append(f"{len(pending)} approvals opened for a clean auto-payment")

    return {
        "name": "6 concurrent deliveries of one message_id",
        "detail": f"paid={after - before} payouts={new_payouts} replies={len(replies)}",
        "failures": failures,
    }


def repeat_complaint_same_trip(harness) -> dict:
    """The payout exports are a static snapshot, so a shortfall stays visible
    after it has been settled. A second complaint about the same order, worded
    differently, must not be compensated again -- not by auto-pay, and not by
    quietly queueing an approval for ops to rubber-stamp."""
    rider = "R026"
    _quiesce(harness)
    before, _ = harness.paid_total(rider)

    for index, text in enumerate(
        [
            "order T672899 ka paisa kam mila",
            "bhai T672899 ka paisa abhi tak poora nahi aaya",
            "19 tarikh ka pura payout check karo",
        ]
    ):
        harness.send(
            {
                "message_id": f"wamid.REPEAT_{index}",
                "text": text,
                "received_at": f"2026-09-22T09:0{index}:00+05:30",
            },
            rider,
        )

    _settle_wait(harness, rider, want=32, timeout=60.0)
    after, _ = harness.paid_total(rider)
    pending = [p for p in harness.pending() if p.get("rider_id") == rider]
    queued = sum(int(p.get("amount") or 0) for p in pending if p.get("type") == "approval")

    failures = []
    if after - before != 32:
        failures.append(f"paid {after - before}, expected exactly 32")
    if queued:
        failures.append(f"Rs {queued} queued for approval on an already settled shortfall")

    return {
        "name": "same shortfall complained about three ways",
        "detail": f"paid={after - before} (expect 32) queued_for_ops={queued}",
        "failures": failures,
    }


def claim_inflation(harness) -> dict:
    """Finance: never pay more than the rider is owed."""
    rider = "R035"
    _quiesce(harness)
    before, _ = harness.paid_total(rider)
    harness.send(
        {
            "message_id": "wamid.INFLATE",
            "text": "21 ko 9999 rupay kam aaye, surge nahi mila",
            "received_at": "2026-09-22T10:00:00+05:30",
        },
        rider,
    )
    # A slow provider can confirm after the reply, so wait for the ledger rather
    # than assuming the payment is instant.
    _settle_wait(harness, rider, want=25, timeout=60.0)
    after, _ = harness.paid_total(rider)
    failures = []
    if after - before != 25:
        failures.append(f"paid {after - before}, expected 25 (the rider asked for 9999)")
    return {
        "name": "rider asks for Rs9999, is owed Rs25",
        "detail": f"paid={after - before}",
        "failures": failures,
    }


def provider_outage(harness) -> dict:
    """PaySwift goes fully dark for 60 seconds.

    The agent must still answer the rider inside the vendor's ~10s window, must
    not pretend the payment happened, and must not lose it: once the provider
    returns, the money arrives exactly once without anyone re-sending anything.
    """
    rider = "R033"
    _quiesce(harness)
    before, before_ids = harness.paid_total(rider)

    try:
        harness.http.post(f"{harness.payswift}/admin/outage", timeout=5.0)
    except Exception:  # noqa: BLE001
        return {
            "name": "PaySwift 60s outage",
            "detail": "skipped: sandbox has no /admin/outage",
            "failures": [],
            "skipped": True,
        }

    reply, elapsed, error = harness.send(
        {
            "message_id": "wamid.OUTAGE_1",
            "text": "20 tarikh ke 2 order ka payment nahi aaya: T795007, T206956",
            "received_at": "2026-09-22T10:00:00+05:30",
        },
        rider,
    )

    failures = []
    if error:
        failures.append(f"service errored during the outage: {error}")
    if elapsed > 10.0:
        failures.append(f"replied in {elapsed:.1f}s, over the vendor's ~10s re-send window")
    if not reply.strip():
        failures.append("no reply during the outage")
    if "process ho gaya" in reply:
        failures.append("told the rider the payment was done while the provider was down")

    recovered = _settle_wait(harness, rider, want=176, timeout=150.0)
    after, after_ids = harness.paid_total(rider)
    new_payouts = len(after_ids - before_ids)

    if recovered != 176:
        failures.append(f"after recovery paid {recovered}, expected 176")
    if new_payouts > 1:
        failures.append(f"{new_payouts} payouts created, expected exactly 1")

    return {
        "name": "PaySwift 60s outage, then recovery",
        "detail": f"replied in {elapsed:.1f}s, paid {recovered} across {new_payouts} payout(s)",
        "failures": failures,
    }


def write_rate_storm(harness) -> dict:
    """The provider blocks an account for 60s after roughly 30 writes in 10s.

    Sixteen payable disputes in a burst would trip that. Nothing may be paid
    twice and nothing may be silently dropped.
    """
    _quiesce(harness)
    riders = [
        ("R003", "order T926334 ka surge nahi mila", 25),
        ("R007", "order T840677 ka surge missing hai", 34),
        ("R026", "Trip T672899 ka paisa kam mila", 32),
        ("R031", "T312538 ka surge nahi mila", 22),
        ("R035", "21 ko surge nahi mila", 25),
        ("R014", "18 tarikh ko penalty 2 baar kata", 10),
        ("R024", "18 ko 12 order kiye, incentive nahi aaya", 150),
        ("R005", "kal 12 se zyada order kiye, incentive nahi mila", 150),
    ]
    baseline = {r: harness.paid_total(r)[0] for r, _, _ in riders}

    def fire(args):
        index, (rider, text, _) = args
        return harness.send(
            {
                "message_id": f"wamid.STORM_{index}",
                "text": text,
                "received_at": "2026-09-22T10:00:00+05:30",
            },
            rider,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(fire, enumerate(riders)))

    failures = []
    deadline = time.time() + 180.0
    while time.time() < deadline:
        unpaid = [
            rider
            for rider, _, want in riders
            if harness.paid_total(rider)[0] - baseline[rider] < want
        ]
        if not unpaid:
            break
        time.sleep(2.0)

    for rider, _, want in riders:
        got = harness.paid_total(rider)[0] - baseline[rider]
        if got != want:
            failures.append(f"{rider} paid {got}, expected {want}")

    return {
        "name": "8 concurrent disputes against the provider's write limit",
        "detail": f"{len(riders)} disputes, {len(failures)} mismatched",
        "failures": failures,
    }


FAST = [concurrent_duplicate_delivery, repeat_complaint_same_trip, claim_inflation]
CHAOS = [provider_outage, write_rate_storm]


def run(harness, include_chaos: bool = False) -> list[dict]:
    results = []
    for scenario in FAST + (CHAOS if include_chaos else []):
        try:
            results.append(scenario(harness))
        except Exception as exc:  # noqa: BLE001
            results.append(
                {
                    "name": scenario.__name__,
                    "detail": "scenario raised",
                    "failures": [f"{type(exc).__name__}: {exc}"],
                }
            )
    return results
