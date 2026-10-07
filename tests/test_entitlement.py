"""Scope-aware settlement, checked against every sample conversation.

The scopes below are hand-derived from each rider's own words -- they are what
app/grounding.py must produce. Keeping them explicit here isolates the
arithmetic from the language understanding, so a failure points at one layer.
"""

from datetime import date

import pytest

from app.entitlement import audit_window, settle_day, settle_trips
from app.store import ReferenceData

D = date.fromisoformat


@pytest.fixture(scope="module")
def ref():
    return ReferenceData()


# case -> (rider, anchor_day, trip_ids | None, days | None, expected_shortfall)
CASES = [
    (1, "R003", "2026-09-22", None, ["2026-09-20"], 25),
    (2, "R027", "2026-09-23", None, ["2026-09-20", "2026-09-21"], 25),
    (3, "R011", "2026-09-22", None, ["2026-09-19"], 0),
    (5, "R003", "2026-09-22", ["T926334"], None, 25),
    (6, "R005", "2026-09-22", None, ["2026-09-21"], 150),
    (7, "R008", "2026-09-22", ["T252921"], None, 0),
    (9, "R014", "2026-09-22", None, ["2026-09-18"], 10),
    (10, "R016", "2026-09-22", None, ["2026-09-19"], 425),
    (11, "R021", "2026-09-22", ["T482410"], None, 0),
    (12, "R022", "2026-09-23", ["T502951"], None, 0),
    (13, "R024", "2026-09-22", None, ["2026-09-18"], 150),
    (14, "R025", "2026-09-22", None, ["2026-09-19"], 0),
    (15, "R007", "2026-09-22", ["T840677"], None, 34),
    (16, "R031", "2026-09-22", ["T312538"], None, 22),
    (17, "R009", "2026-09-22", None, ["2026-09-17"], 0),
    (18, "R026", "2026-09-22", ["T672899"], None, 32),
    (19, "R033", "2026-09-22", ["T795007", "T206956"], None, 176),
    (20, "R034", "2026-09-23", None, ["2026-09-19"], 249),
    (21, "R035", "2026-09-22", ["T980582"], None, 25),
    (22, "R036", "2026-09-23", ["T990831"], None, 0),
    (23, "R028", "2026-09-23", ["T849302"], None, 0),
]


@pytest.mark.parametrize("case,rider,anchor,trip_ids,days,expected", CASES)
def test_sample_conversation_shortfall(ref, case, rider, anchor, trip_ids, days, expected):
    anchor_day = D(anchor)
    if trip_ids:
        total = settle_trips(ref, rider, anchor_day, trip_ids).shortfall
    else:
        total = sum(settle_day(ref, rider, anchor_day, D(d)).shortfall for d in days)
    assert total == expected, f"case {case}"


def test_other_rider_trip_is_flagged_not_paid(ref):
    """Case 11: R021 asks about T482410, which belongs to R030."""
    s = settle_trips(ref, "R021", D("2026-09-22"), ["T482410"])
    assert s.shortfall == 0
    assert s.has_flag("other_rider")
    assert s.flags[0].detail["belongs_to"] == "R030"


def test_stale_trip_is_flagged_not_paid(ref):
    """Case 12: a genuine Rs 22 shortfall, but 10 days before the message."""
    s = settle_trips(ref, "R022", D("2026-09-23"), ["T502951"])
    assert s.shortfall == 0
    assert s.has_flag("outside_window")
    assert s.flags[0].detail["days_ago"] == 10

    # Inside the window the same trip really is worth Rs 22.
    fresh = settle_trips(ref, "R022", D("2026-09-16"), ["T502951"])
    assert fresh.shortfall == 22


def test_window_boundary_is_inclusive_at_seven_days(ref):
    """Exactly 7 days old is in; 8 days is out."""
    assert settle_trips(ref, "R022", D("2026-09-20"), ["T502951"]).shortfall == 22
    assert settle_trips(ref, "R022", D("2026-09-21"), ["T502951"]).shortfall == 0


def test_correct_trip_reports_the_figures(ref):
    """Case 7: T252921 was paid exactly right; the rider still needs the numbers."""
    s = settle_trips(ref, "R008", D("2026-09-22"), ["T252921"])
    assert s.shortfall == 0
    flag = next(f for f in s.flags if f.kind == "trip_paid_correctly")
    assert flag.detail["owed"] == 46 and flag.detail["paid"] == 46


def test_incentive_not_earned_carries_the_true_count(ref):
    """Case 3: the reply has to say 10, so the flag has to carry 10."""
    s = settle_day(ref, "R011", D("2026-09-22"), D("2026-09-19"))
    assert s.shortfall == 0
    flag = next(f for f in s.flags if f.kind == "incentive_not_earned")
    assert flag.detail["completed_trips"] == 10


def test_legitimate_penalty_is_not_refunded(ref):
    """Case 23: the rider did cancel, the penalty stands, a human hears the reason."""
    s = settle_trips(ref, "R028", D("2026-09-23"), ["T849302"])
    assert s.shortfall == 0
    assert s.has_flag("penalty_legitimate")


def test_unknown_trip_is_flagged(ref):
    s = settle_trips(ref, "R003", D("2026-09-22"), ["T999999"])
    assert s.shortfall == 0
    assert s.has_flag("unknown_trip")


def test_metres_fault_resolves_to_a_real_shortfall(ref):
    """Case 16: distance stored as 5000 metres, not 5000 km."""
    s = settle_trips(ref, "R031", D("2026-09-22"), ["T312538"])
    assert s.shortfall == 22
    assert s.reasons[0].detail["owed"] == 65 and s.reasons[0].detail["paid"] == 43


def test_non_canonical_rider_id_resolves(ref):
    """Case 15: T840677 is stored against 'R7'."""
    s = settle_trips(ref, "R007", D("2026-09-22"), ["T840677"])
    assert s.shortfall == 34


def test_window_audit_exceeds_claim_scope(ref):
    """The reason scope exists: a whole-window audit over-computes.

    R035 asked about one trip worth Rs 25 but is owed Rs 62 across the week.
    Paying 62 would fail the eval and hand ops an unreconcilable payment.
    """
    claim = settle_trips(ref, "R035", D("2026-09-22"), ["T980582"])
    everything = audit_window(ref, "R035", D("2026-09-22"))
    assert claim.shortfall == 25
    assert everything.shortfall == 62
    assert everything.scope["advisory"] is True


def test_overpayment_is_flagged_never_netted(ref):
    """No trip in this export is overpaid, so assert the branch directly."""
    from app.entitlement import Settlement, _settle_trip
    from app.normalize import Trip

    class FakeRef:
        def amount_paid_for_trip(self, trip_id):
            return 100

    trip = Trip("T000001", "R001", D("2026-09-20"), "completed", 3.0, 1.0)
    s = Settlement(rider_id="R001")
    _settle_trip(FakeRef(), s, trip)
    assert s.shortfall == 0
    assert s.has_flag("overpaid")
    assert s.flags[0].detail["excess"] == 69


def test_reply_never_requires_a_fact_it_does_not_state(ref):
    """The guard must always be satisfiable.

    required_mentions is filtered against the draft, because demanding a figure
    the deterministic reply does not itself contain would make every rewrite
    fail and silently disable the model pass.
    """
    import json
    from pathlib import Path

    from app import replies
    from app.claims import classify_deterministic
    from app.decide import AutopayBudget, decide

    cases = json.loads((Path(__file__).parent.parent / "data" / "conversations.json").read_text())
    for case in cases:
        rider = case["rider_id"]
        for turn in case["turns"]:
            if turn.get("from") != "rider":
                continue
            anchor = D(turn["received_at"][:10])
            decisions = decide(
                ref, rider, anchor,
                classify_deterministic(turn["text"], anchor),
                AutopayBudget(rider_id=rider, day=anchor),
            )
            draft = replies.compose(decisions, {})
            for needed in replies.required_mentions(decisions, {}, draft):
                assert replies.mentions(draft, needed), (
                    f"{rider}: draft does not state required fact {needed!r}: {draft}"
                )


def test_many_reasons_are_aggregated_not_listed_one_by_one(ref):
    """R016 is owed five unpaid orders. The rider should get a count and a total,
    not five near-identical sentences."""
    from app import replies
    from app.claims import classify_deterministic
    from app.decide import AutopayBudget, decide

    anchor = D("2026-09-22")
    decisions = decide(
        ref, "R016", anchor,
        classify_deterministic("19 sept ke 5 orders ka paisa nahi aaya", anchor),
        AutopayBudget(rider_id="R016", day=anchor),
    )
    draft = replies.compose(decisions, {})
    assert "5 orders" in draft
    assert "425" in draft
    assert draft.count("payout bilkul nahi gaya") == 1, draft
    assert len(draft) < 260, f"still too long: {draft}"


def test_nothing_owed_answers_the_question_asked(ref):
    """Case 3: the rider asked about the incentive, so the reply leads with the
    trip count -- not with a fare breakdown of all ten trips."""
    from app import replies
    from app.claims import classify_deterministic
    from app.decide import AutopayBudget, decide

    anchor = D("2026-09-22")
    decisions = decide(
        ref, "R011", anchor,
        classify_deterministic("19 ko incentive nahi mila", anchor),
        AutopayBudget(rider_id="R011", day=anchor),
    )
    draft = replies.compose(decisions, {})
    assert "10 trips" in draft and "12 trips" in draft
    assert "km" not in draft, f"should not recite trip-by-trip fares: {draft}"
    assert len(draft) < 160, draft
