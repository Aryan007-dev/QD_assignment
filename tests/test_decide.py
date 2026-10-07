"""Routing boundaries. Every one of these is a line from the Finance note."""

from datetime import date

import pytest

from app.claims import Claim, ClaimKind, ClaimSet
from app.decide import Action, AutopayBudget, decide
from app.policy import AUTO_PAY_MAX
from app.store import ReferenceData

D = date.fromisoformat


@pytest.fixture(scope="module")
def ref():
    return ReferenceData()


def budget(rider="R003", day="2026-09-22", used=0):
    return AutopayBudget(rider_id=rider, day=D(day), already_used=used)


def one(kind, **kwargs):
    return ClaimSet(claims=[Claim(kind=kind, **kwargs)])


def test_within_ceiling_and_first_today_is_auto_paid(ref):
    decisions = decide(
        ref, "R003", D("2026-09-22"),
        one(ClaimKind.SURGE_MISSING, trip_ids=["T926334"]), budget(),
    )
    assert [d.action for d in decisions] == [Action.PAY]
    assert decisions[0].amount == 25
    assert decisions[0].rule == "within_auto_pay_limits"


def test_above_ceiling_goes_to_ops(ref):
    """R017 is owed Rs 352 on 20 Sep, over the Rs 200 ceiling."""
    decisions = decide(
        ref, "R017", D("2026-09-22"),
        one(ClaimKind.TRIP_UNPAID, date_hint="2026-09-20"), budget("R017"),
    )
    assert decisions[0].action == Action.APPROVAL
    assert decisions[0].amount == 352
    assert decisions[0].rule == "above_auto_pay_ceiling"


def test_ceiling_is_inclusive(ref):
    """Rs 176 is under the ceiling and pays; Rs 261 is over and does not."""
    pay = decide(
        ref, "R017", D("2026-09-22"),
        one(ClaimKind.TRIP_UNPAID, trip_ids=["T472165", "T417277"]), budget("R017"),
    )[0]
    assert pay.action == Action.PAY and pay.amount == 176 and pay.amount <= AUTO_PAY_MAX

    approve = decide(
        ref, "R017", D("2026-09-22"),
        one(ClaimKind.TRIP_UNPAID, trip_ids=["T472165", "T417277", "T621978"]),
        budget("R017"),
    )[0]
    assert approve.action == Action.APPROVAL and approve.amount == 261


def test_second_dispute_the_same_day_goes_to_ops(ref):
    """Finance: once per rider per day."""
    decisions = decide(
        ref, "R003", D("2026-09-22"),
        one(ClaimKind.SURGE_MISSING, trip_ids=["T926334"]), budget(used=1),
    )
    assert decisions[0].action == Action.APPROVAL
    assert decisions[0].rule == "auto_pay_already_used_today"
    assert decisions[0].amount == 25


def test_two_claims_in_one_message_split_across_pay_and_approval(ref):
    """Case 2: Rs 15 and Rs 10 are both owed, but only one may auto-pay."""
    claims = ClaimSet(
        claims=[
            Claim(kind=ClaimKind.SURGE_MISSING, date_hint="2026-09-20"),
            Claim(kind=ClaimKind.PENALTY_WRONG, date_hint="2026-09-21"),
        ]
    )
    decisions = decide(ref, "R027", D("2026-09-23"), claims, budget("R027", "2026-09-23"))
    actions = {d.action for d in decisions}
    assert actions == {Action.PAY, Action.APPROVAL}
    assert sum(d.amount for d in decisions) == 25


def test_rider_figure_is_never_the_amount(ref):
    """Case 21: claims Rs 300, is owed Rs 25."""
    decisions = decide(
        ref, "R035", D("2026-09-22"),
        one(ClaimKind.SURGE_MISSING, trip_ids=["T980582"], amount_claimed=300),
        budget("R035"),
    )
    assert decisions[0].amount == 25


def test_absurd_claimed_amount_changes_nothing(ref):
    """The guard that makes the determinism boundary real."""
    decisions = decide(
        ref, "R035", D("2026-09-22"),
        one(ClaimKind.SURGE_MISSING, trip_ids=["T980582"], amount_claimed=99999),
        budget("R035"),
    )
    assert decisions[0].amount == 25


def test_injection_is_escalated_never_acted_on(ref):
    decisions = decide(
        ref, "R037", D("2026-09-22"),
        one(ClaimKind.INJECTION), budget("R037"),
    )
    assert decisions[0].action == Action.ESCALATION
    assert decisions[0].rule == "prompt_injection_attempt"
    assert decisions[0].amount is None


def test_identity_claim_is_escalated(ref):
    decisions = decide(
        ref, "R020", D("2026-09-22"),
        one(ClaimKind.IDENTITY_CLAIM), budget("R020"),
    )
    assert decisions[0].action == Action.ESCALATION
    assert decisions[0].rule == "identity_claim_ignored"


def test_classifier_disagreement_escalates_rather_than_pays(ref):
    claims = ClaimSet(
        claims=[Claim(kind=ClaimKind.SURGE_MISSING, trip_ids=["T926334"])],
        disagreement=True,
    )
    decisions = decide(ref, "R003", D("2026-09-22"), claims, budget())
    assert decisions[0].action == Action.ESCALATION
    assert decisions[0].rule == "classifier_disagreement"


def test_vague_with_no_scope_asks_instead_of_auditing(ref):
    """The rule that keeps the agent from volunteering undisputed money."""
    decisions = decide(ref, "R035", D("2026-09-22"), one(ClaimKind.VAGUE), budget("R035"))
    assert decisions[0].action == Action.CLARIFY
    assert decisions[0].rule == "scope_unknown"


def test_already_compensated_is_not_paid_twice(ref):
    """The payout export is a static snapshot, so the shortfall is still visible
    after it has been settled."""
    decisions = decide(
        ref, "R003", D("2026-09-22"),
        one(ClaimKind.SURGE_MISSING, trip_ids=["T926334"]),
        budget(),
        compensated={"trip:T926334"},
    )
    assert decisions[0].action == Action.EXPLAIN
    assert decisions[0].rule == "already_compensated"
    assert decisions[0].amount == 0


def test_unverifiable_claims_go_to_a_human(ref):
    for kind, trip in [
        (ClaimKind.DISTANCE_WRONG, "T990831"),
        (ClaimKind.CANCELLATION_REASON, "T849302"),
    ]:
        decisions = decide(
            ref, "R036" if trip == "T990831" else "R028", D("2026-09-23"),
            one(kind, trip_ids=[trip]), budget("R036", "2026-09-23"),
        )
        assert decisions[0].action == Action.ESCALATION
        assert decisions[0].rule.startswith("unverifiable_claim")
