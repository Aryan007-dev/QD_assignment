"""Who is allowed to settle this, and for how much.

Every branch here is a line from docs/policy.md or the Finance note, written as
code. There is no model call in this module and no randomness: given the same
claims and the same ledger it returns the same decision, which is what makes
the trace reviewable and the behaviour arguable in front of an ops team.

Finance's constraints, restated as the rules they become:
  "auto-pay is fine for small amounts: up to Rs 200 per dispute"   -> AUTO_PAY_MAX
  "once per rider per day"                                        -> autopay budget
  "bigger or repeat ones need someone from ops to approve"         -> approval item
  "never pay more than the rider is owed"                          -> amount is
      always the computed shortfall; the rider's own figure is never an input
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.claims import Claim, ClaimKind, ClaimSet
from app.entitlement import Settlement, settle_day, settle_trips
from app.policy import AUTO_PAY_MAX
from app.store import ReferenceData


class Action:
    PAY = "pay"
    APPROVAL = "approval"
    ESCALATION = "escalation"
    EXPLAIN = "explain"
    CLARIFY = "clarify"


@dataclass
class Decision:
    """One outcome for one claim, with the rule that produced it."""

    action: str
    rule: str
    reason: str
    amount: int | None = None
    claim: Claim | None = None
    settlement: Settlement | None = None
    detail: dict = field(default_factory=dict)

    def as_trace(self) -> dict:
        return {
            "action": self.action,
            "rule": self.rule,
            "reason": self.reason,
            "amount": self.amount,
            "claim_kind": str(self.claim.kind) if self.claim else None,
            "scope": self.settlement.scope if self.settlement else None,
            "settlement": self.settlement.as_trace() if self.settlement else None,
            "detail": self.detail,
        }


@dataclass
class AutopayBudget:
    """Finance: one auto-payment per rider per day. Seeded from PaySwift, which
    is the ledger of record, so a lost response cannot buy a second payment."""

    rider_id: str
    day: date
    already_used: int = 0
    limit: int = 1

    @property
    def available(self) -> bool:
        return self.already_used < self.limit

    def consume(self) -> None:
        self.already_used += 1


def _scope_settlement(
    ref: ReferenceData,
    rider_id: str,
    anchor_day: date,
    claim: Claim,
    compensated: set[str] | None = None,
) -> Settlement | None:
    """Settle inside the claim's own scope. Trips win over days: when a rider
    names an order, that order is the dispute."""
    if claim.trip_ids:
        return settle_trips(ref, rider_id, anchor_day, claim.trip_ids, compensated)
    if claim.date_hint:
        try:
            day = date.fromisoformat(claim.date_hint)
        except (TypeError, ValueError):
            # Defence in depth: hints are normalised upstream, but an unparseable
            # one must mean "ask the rider", never an exception on a payment path.
            return None
        return settle_day(ref, rider_id, anchor_day, day, compensated)
    return None


def _route_settled_claim(
    claim: Claim,
    settlement: Settlement,
    budget: AutopayBudget,
) -> Decision:
    """Turn a computed shortfall into pay / approval / escalation / explain."""

    # Guards first: these are zero-money outcomes regardless of arithmetic.
    if settlement.has_flag("other_rider"):
        return Decision(
            action=Action.ESCALATION,
            rule="trip_belongs_to_another_rider",
            reason=(
                "Rider asked about an order that is not theirs. Identity comes from the "
                "sending number, so this needs a human."
            ),
            claim=claim,
            settlement=settlement,
        )

    if settlement.has_flag("unknown_trip"):
        return Decision(
            action=Action.ESCALATION,
            rule="unknown_trip_id",
            reason="Order id not found in the trips export; cannot verify the claim.",
            claim=claim,
            settlement=settlement,
        )

    if settlement.has_flag("outside_window"):
        return Decision(
            action=Action.ESCALATION,
            rule="outside_dispute_window",
            reason=(
                "Dispute is older than the 7-day window. Explained to the rider; "
                "ops may still choose to look."
            ),
            claim=claim,
            settlement=settlement,
            detail={"explain_to_rider": True},
        )

    if claim.needs_human:
        return Decision(
            action=Action.ESCALATION,
            rule=f"unverifiable_claim:{claim.kind}",
            reason=(
                "Claim cannot be checked against the trips or payout exports "
                f"({claim.kind}); a human has to decide."
            ),
            claim=claim,
            settlement=settlement,
        )

    if settlement.shortfall <= 0 and settlement.has_flag("already_compensated"):
        return Decision(
            action=Action.EXPLAIN,
            rule="already_compensated",
            reason=(
                "This shortfall was already settled earlier in this conversation. "
                "Not compensating it twice."
            ),
            amount=0,
            claim=claim,
            settlement=settlement,
        )

    if settlement.shortfall <= 0:
        return Decision(
            action=Action.EXPLAIN,
            rule="nothing_owed",
            reason="Records match what was paid; explaining the figures to the rider.",
            amount=0,
            claim=claim,
            settlement=settlement,
        )

    amount = settlement.shortfall

    if amount > AUTO_PAY_MAX:
        return Decision(
            action=Action.APPROVAL,
            rule="above_auto_pay_ceiling",
            reason=f"Rs {amount} exceeds the Rs {AUTO_PAY_MAX} auto-pay ceiling.",
            amount=amount,
            claim=claim,
            settlement=settlement,
        )

    if not budget.available:
        return Decision(
            action=Action.APPROVAL,
            rule="auto_pay_already_used_today",
            reason=(
                f"Rs {amount} is within the ceiling but this rider has already had an "
                "auto-payment today."
            ),
            amount=amount,
            claim=claim,
            settlement=settlement,
        )

    budget.consume()
    return Decision(
        action=Action.PAY,
        rule="within_auto_pay_limits",
        reason=f"Rs {amount} is within the Rs {AUTO_PAY_MAX} ceiling and today's first auto-payment.",
        amount=amount,
        claim=claim,
        settlement=settlement,
    )


def decide(
    ref: ReferenceData,
    rider_id: str,
    anchor_day: date,
    claim_set: ClaimSet,
    budget: AutopayBudget,
    *,
    has_open_question: bool = False,
    compensated: set[str] | None = None,
) -> list[Decision]:
    """Route every claim in the message. Returns one Decision per claim."""
    decisions: list[Decision] = []

    # The model and the rule layer disagreed about whether money is owed. Pay
    # nothing on a reading we cannot corroborate.
    if claim_set.disagreement:
        return [
            Decision(
                action=Action.ESCALATION,
                rule="classifier_disagreement",
                reason=(
                    "The language model and the rule-based parser disagreed about whether "
                    "this message is a payable claim."
                ),
                detail={"kinds": [str(k) for k in claim_set.kinds]},
            )
        ]

    for claim in claim_set.claims:
        if claim.kind is ClaimKind.INJECTION:
            decisions.append(
                Decision(
                    action=Action.ESCALATION,
                    rule="prompt_injection_attempt",
                    reason=(
                        "Message contained instructions aimed at the assistant rather than "
                        "a payout complaint. Logged verbatim, not acted on."
                    ),
                    claim=claim,
                )
            )
            continue

        if claim.kind is ClaimKind.IDENTITY_CLAIM:
            decisions.append(
                Decision(
                    action=Action.ESCALATION,
                    rule="identity_claim_ignored",
                    reason=(
                        "Message claimed to be a different rider. Identity comes from the "
                        "sending phone number, never from the text."
                    ),
                    claim=claim,
                )
            )
            continue

        if claim.kind is ClaimKind.DATA_REQUEST:
            decisions.append(
                Decision(
                    action=Action.ESCALATION,
                    rule="another_riders_data_requested",
                    reason=(
                        "Message asked about another rider's payout or personal details. "
                        "Refused and logged; a human can decide whether it is benign."
                    ),
                    claim=claim,
                )
            )
            continue

        if claim.kind is ClaimKind.DISPUTE_RECORDS:
            decisions.append(
                Decision(
                    action=Action.ESCALATION,
                    rule="rider_disputes_our_records",
                    reason=(
                        "Rider rejects a correct explanation. Holding the figure and "
                        "offering a human review."
                    ),
                    claim=claim,
                )
            )
            continue

        if claim.kind is ClaimKind.STATUS_QUERY:
            decisions.append(
                Decision(
                    action=Action.EXPLAIN,
                    rule="status_question",
                    reason="Rider is asking about something already handled in this thread.",
                    claim=claim,
                )
            )
            continue

        settlement = _scope_settlement(ref, rider_id, anchor_day, claim, compensated)

        if settlement is None:
            # Nothing to compute against: ask, rather than audit the whole week
            # and volunteer money that was never disputed.
            decisions.append(
                Decision(
                    action=Action.CLARIFY,
                    rule="scope_unknown",
                    reason="Complaint has no order id or date yet; asking the rider which one.",
                    claim=claim,
                    detail={"asked_for": ["date", "trip_id"]},
                )
            )
            continue

        decisions.append(_route_settled_claim(claim, settlement, budget))

    return decisions or [
        Decision(
            action=Action.CLARIFY,
            rule="no_claim_detected",
            reason="Could not read a payout complaint from this message.",
        )
    ]
