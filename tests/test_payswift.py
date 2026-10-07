"""PaySwift client behaviour against a scripted provider.

The sandbox is random by design, so these drive a stub that reproduces each of
its documented behaviours deterministically.
"""

from datetime import date

import httpx
import pytest

from app.payswift import (
    PaySwiftClient,
    PayoutOutcome,
    TokenBucket,
    idempotency_key,
    payout_reference,
)

REFERENCE = "qd-agent dispute=D1 rider=R003 day=2026-09-22 rules=test"


def client_with(handler) -> PaySwiftClient:
    transport = httpx.MockTransport(handler)
    return PaySwiftClient("http://payswift", client=httpx.Client(transport=transport))


def test_idempotency_key_is_stable_across_retries():
    assert idempotency_key("R003", "D1", 25) == idempotency_key("R003", "D1", 25)


def test_idempotency_key_changes_with_the_amount():
    """The sandbox returns 409 when a key is reused with a different body, so the
    amount has to be part of the key."""
    assert idempotency_key("R003", "D1", 25) != idempotency_key("R003", "D1", 26)
    assert idempotency_key("R003", "D1", 25) != idempotency_key("R003", "D2", 25)
    assert idempotency_key("R003", "D1", 25) != idempotency_key("R004", "D1", 25)


def test_successful_payout():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(201, json={"payout_id": "pout_1", "status": "processed"})

    result = client_with(handler).pay("R003", 25, REFERENCE, "D1")
    assert result.outcome == PayoutOutcome.PAID
    assert result.payout_id == "pout_1"
    assert result.money_moved


def test_retries_through_a_transient_error_with_the_same_key():
    seen_keys = []
    state = {"posts": 0}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        seen_keys.append(request.headers["Idempotency-Key"])
        state["posts"] += 1
        if state["posts"] == 1:
            return httpx.Response(503, json={"error": "service_unavailable"})
        return httpx.Response(201, json={"payout_id": "pout_2", "status": "processed"})

    result = client_with(handler).pay("R003", 25, REFERENCE, "D1")
    assert result.outcome == PayoutOutcome.PAID
    assert result.attempts == 2
    assert len(set(seen_keys)) == 1, "a retry must reuse the original key"


def test_a_lost_response_is_recovered_by_reading_the_ledger():
    """5% of responses are dropped. The payout still happened, so the client must
    not conclude that nothing was paid."""
    state = {"posted": False}

    def handler(request):
        if request.method == "GET":
            if state["posted"]:
                return httpx.Response(
                    200,
                    json={"data": [{"payout_id": "pout_3", "reference": REFERENCE, "status": "processed"}]},
                )
            return httpx.Response(200, json={"data": []})
        state["posted"] = True
        raise httpx.ReadTimeout("response lost")

    result = client_with(handler).pay("R003", 25, REFERENCE, "D1")
    assert result.outcome == PayoutOutcome.ALREADY_PAID
    assert result.payout_id == "pout_3"
    assert result.money_moved


def test_an_already_settled_dispute_is_not_paid_again():
    posts = {"n": 0}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"data": [{"payout_id": "pout_4", "reference": REFERENCE, "status": "processed"}]},
            )
        posts["n"] += 1
        return httpx.Response(201, json={"payout_id": "pout_new"})

    result = client_with(handler).pay("R003", 25, REFERENCE, "D1")
    assert result.outcome == PayoutOutcome.ALREADY_PAID
    assert posts["n"] == 0, "must not POST when the ledger already has this dispute"


def test_rate_limit_fails_fast_instead_of_hammering():
    """The sandbox blocks an account for 60s, which no in-request retry can
    outlast, so retrying would only deepen the block."""
    posts = {"n": 0}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        posts["n"] += 1
        return httpx.Response(429, json={"error": "account_blocked"})

    result = client_with(handler).pay("R003", 25, REFERENCE, "D1")
    assert result.outcome == PayoutOutcome.UNKNOWN
    assert result.error == "account_blocked"
    assert posts["n"] == 1, "429 must not be retried inside the request"


def test_bad_request_is_not_retried():
    posts = {"n": 0}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        posts["n"] += 1
        return httpx.Response(400, json={"error": "invalid_amount"})

    result = client_with(handler).pay("R003", 0, REFERENCE, "D1")
    assert result.outcome == PayoutOutcome.REJECTED
    assert result.error == "invalid_amount"
    assert posts["n"] == 1


def test_unconfirmed_payout_is_reported_as_unknown_not_paid():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(503, json={"error": "service_unavailable"})

    result = client_with(handler).pay("R003", 25, REFERENCE, "D1")
    assert result.outcome == PayoutOutcome.UNKNOWN
    assert not result.money_moved, "never tell the rider it is paid when it is not"


def test_autopay_count_ignores_the_epoch_baseline():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "data": [
                    {"payout_id": "old", "reference": "qd-agent day=2026-09-22 x"},
                    {"payout_id": "new", "reference": "qd-agent day=2026-09-22 y"},
                ]
            },
        )

    client = client_with(handler)
    day = date(2026, 9, 22)
    assert client.autopay_count_for_day("R003", day, "qd-agent") == 2
    assert client.autopay_count_for_day("R003", day, "qd-agent", exclude_payout_ids={"old"}) == 1


def test_ledger_envelope_shapes_are_all_understood():
    for body in (
        {"data": [{"payout_id": "a", "amount": 5}]},
        {"payouts": [{"payout_id": "a", "amount": 5}]},
        [{"payout_id": "a", "amount": 5}],
    ):
        client = client_with(lambda request, body=body: httpx.Response(200, json=body))
        assert len(client.list_payouts("R003")) == 1


def test_token_bucket_limits_the_rate():
    bucket = TokenBucket(rate_per_second=100.0, burst=2)
    assert bucket.take(timeout=0.0) is True
    assert bucket.take(timeout=0.0) is True
    assert bucket.take(timeout=0.0) is False   # burst spent
    assert bucket.take(timeout=0.5) is True    # refills


def test_reference_carries_the_day_for_the_autopay_count():
    reference = payout_reference("D1", "R003", date(2026, 9, 22), ["within_auto_pay_limits"])
    assert "day=2026-09-22" in reference
    assert "qd-agent" in reference
