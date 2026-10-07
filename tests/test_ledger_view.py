"""Reconciliation against the PaySwift ledger.

The point of this view is not to list payouts, it is to answer the three
questions an ops executive has before trusting a number. Each is tested here.
"""

from datetime import date

from app.agent import AUTOPAY_MARKER
from app.ledger_view import (
    SOURCE_AGENT,
    SOURCE_EXTERNAL,
    SOURCE_OPS,
    build,
    classify_source,
    describe_payouts,
    find_discrepancies,
    parse_reference,
)

DAY = date(2026, 9, 22)


def agent_payout(payout_id, rider, amount, dispute, rule="within_auto_pay_limits"):
    return {
        "payout_id": payout_id,
        "rider_id": rider,
        "amount": amount,
        "status": "processed",
        "created_at": "2026-10-06T19:00:00Z",
        "reference": f"{AUTOPAY_MARKER} dispute={dispute} rider={rider} day=2026-09-22 rules={rule}",
    }


def test_reference_is_parsed_back_into_fields():
    fields = parse_reference(
        "qd-agent dispute=D00001-R003-abc rider=R003 day=2026-09-22 rules=within_auto_pay_limits"
    )
    assert fields["dispute"] == "D00001-R003-abc"
    assert fields["day"] == "2026-09-22"
    assert fields["rules"] == "within_auto_pay_limits"


def test_unparseable_reference_does_not_raise():
    """Payouts made by hand outside the agent have free-text references."""
    assert parse_reference("manual adjustment by finance") == {}
    assert parse_reference("") == {}
    assert parse_reference(None) == {}


def test_source_classification():
    assert classify_source("qd-agent dispute=D1", "within_auto_pay_limits", AUTOPAY_MARKER) == SOURCE_AGENT
    assert classify_source("qd-agent dispute=D1", "ops_approved", AUTOPAY_MARKER) == SOURCE_OPS
    assert classify_source("manual adjustment", "", AUTOPAY_MARKER) == SOURCE_EXTERNAL


def test_each_agent_payout_is_traced_to_its_dispute_and_rule():
    payouts, disputes = describe_payouts(
        [agent_payout("pout_1", "R003", 25, "D00001-R003-abc")], AUTOPAY_MARKER
    )
    assert payouts[0]["dispute_id"] == "D00001-R003-abc"
    assert payouts[0]["rule"] == "within_auto_pay_limits"
    assert payouts[0]["source"] == SOURCE_AGENT
    assert disputes == {"D00001-R003-abc"}


def test_a_payout_nobody_can_explain_is_flagged():
    """Finance paying a rider by hand must not look like agent activity."""
    view = build(
        [{"payout_id": "p1", "rider_id": "R009", "amount": 500,
          "reference": "manual adjustment by finance", "created_at": "x"}],
        {},
        AUTOPAY_MARKER,
        payswift_reachable=True,
        payments_reconciling=0,
    )
    assert len(view["unattributed"]) == 1
    assert view["unattributed"][0]["rider_id"] == "R009"
    assert view["total_paid"] == 500


def test_in_flight_payout_is_unconfirmed_not_missing():
    """Still being chased: the reconciler will resolve it."""
    unconfirmed, missing = find_discrepancies(
        {("R003", DAY): [{"dispute_id": "D1", "amount": 25, "confirmed": False}]},
        disputes_in_ledger=set(),
    )
    assert len(unconfirmed) == 1 and not missing
    assert "no confirmation yet" in unconfirmed[0]["note"]


def test_confirmed_payout_absent_from_the_ledger_is_a_real_alarm():
    """We hold a payout id the ledger does not have. Retrying cannot fix this."""
    unconfirmed, missing = find_discrepancies(
        {("R003", DAY): [{"dispute_id": "D1", "amount": 25, "payout_id": "pout_1", "confirmed": True}]},
        disputes_in_ledger=set(),
    )
    assert len(missing) == 1 and not unconfirmed
    assert missing[0]["payout_id"] == "pout_1"


def test_a_payout_present_in_the_ledger_raises_no_alarm():
    unconfirmed, missing = find_discrepancies(
        {("R003", DAY): [{"dispute_id": "D1", "amount": 25, "confirmed": True}]},
        disputes_in_ledger={"D1"},
    )
    assert not unconfirmed and not missing


def test_a_clean_ledger_reports_that_it_reconciles():
    view = build(
        [agent_payout("pout_1", "R003", 25, "D1"), agent_payout("pout_2", "R024", 150, "D2")],
        {("R003", DAY): [{"dispute_id": "D1", "amount": 25, "confirmed": True}]},
        AUTOPAY_MARKER,
        payswift_reachable=True,
        payments_reconciling=0,
    )
    assert view["reconciles"] is True
    assert view["total_paid"] == 175
    assert view["rider_count"] == 2
    assert view["by_rider"] == {"R003": 25, "R024": 150}


def test_ops_approved_payouts_are_distinguished_from_automatic_ones():
    """Ops needs to see which payments a human authorised."""
    view = build(
        [
            agent_payout("pout_1", "R003", 25, "D1"),
            agent_payout("pout_2", "R016", 425, "OPS00001-ops", rule="ops_approved"),
        ],
        {},
        AUTOPAY_MARKER,
        payswift_reachable=True,
        payments_reconciling=0,
    )
    sources = {p["rider_id"]: p["source"] for p in view["payouts"]}
    assert sources == {"R003": SOURCE_AGENT, "R016": SOURCE_OPS}


def test_newest_payout_comes_first():
    view = build(
        [
            {"payout_id": "old", "rider_id": "R1", "amount": 1, "reference": "x", "created_at": "2026-10-01T00:00:00Z"},
            {"payout_id": "new", "rider_id": "R1", "amount": 1, "reference": "x", "created_at": "2026-10-06T00:00:00Z"},
        ],
        {}, AUTOPAY_MARKER, payswift_reachable=True, payments_reconciling=0,
    )
    assert [p["payout_id"] for p in view["payouts"]] == ["new", "old"]


def test_an_unreachable_provider_is_reported_not_hidden():
    view = build([], {}, AUTOPAY_MARKER, payswift_reachable=False, payments_reconciling=0)
    assert view["payswift_reachable"] is False
    assert view["total_paid"] == 0


def test_malformed_amounts_do_not_break_the_totals():
    view = build(
        [{"payout_id": "p", "rider_id": "R1", "amount": None, "reference": "x", "created_at": "x"}],
        {}, AUTOPAY_MARKER, payswift_reachable=True, payments_reconciling=0,
    )
    assert view["total_paid"] == 0
