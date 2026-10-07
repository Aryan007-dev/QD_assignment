"""The agent loop for one inbound rider message.

Control flow is owned by code, not by the model. The model's only job is to say
what kind of complaint this is (app/claims.py); everything that follows --
scope, arithmetic, authority, payment -- is deterministic. That is what makes
the trace reviewable and what stops "approve Rs 999" from being actionable.

Order of work, and why:
  1. Deduplicate. The messaging vendor re-sends if it does not get a 2xx in
     ~10s, and a re-send can arrive *concurrently* with the original, so this
     is a lock-and-claim, not a read-then-write check.
  2. Anchor the clock to the message's own received_at. The exports are
     September 2026; using the wall clock would make every dispute stale.
  3. Classify, then ground, then settle inside the claim's scope.
  4. Seed today's auto-pay budget from PaySwift, the ledger of record.
  5. Route, act, reply, flush the trace.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date

from app import replies
from app.claims import ClaimSet, classify_deterministic, merge_claim_sets
from app.decide import Action, AutopayBudget, Decision, decide
from app.grounding import ground
from app.llm import classify_with_model, is_model_configured
from app.normalize import canonical_rider_id, ist_now_from
from app.payswift import PaySwiftClient, PayoutOutcome, payout_reference
from app.store import AgentState, OpsItem, ReferenceData
from app.trace import Tracer

AUTOPAY_MARKER = "qd-agent"


@dataclass
class InboundMessage:
    message_id: str
    rider_id: str
    text: str
    received_at: str


class Agent:
    def __init__(
        self,
        ref: ReferenceData,
        state: AgentState,
        payswift: PaySwiftClient,
        reconciler=None,
    ) -> None:
        self.ref = ref
        self.state = state
        self.payswift = payswift
        self.reconciler = reconciler
        self._inflight: dict[str, threading.Event] = {}

    # -- idempotency -----------------------------------------------------

    def _claim_message(self, message_id: str) -> tuple[bool, str | None]:
        """Claim this message_id for processing.

        Returns (should_process, stored_reply). A concurrent duplicate waits for
        the original to finish and then returns its reply, so the vendor's retry
        can never produce a second payment or a second trace.
        """
        while True:
            with self.state.lock:
                if message_id in self.state.handled_messages:
                    return False, self.state.handled_messages[message_id]
                waiter = self._inflight.get(message_id)
                if waiter is None:
                    self._inflight[message_id] = threading.Event()
                    return True, None
            waiter.wait(timeout=9.0)
            with self.state.lock:
                if message_id in self.state.handled_messages:
                    return False, self.state.handled_messages[message_id]
                if message_id not in self._inflight:
                    # Original failed without storing a reply; take over.
                    continue
            return False, None

    def _release_message(self, message_id: str, reply: str | None, rider_id: str = "") -> None:
        with self.state.lock:
            if reply is not None:
                self.state.handled_messages[message_id] = reply
            waiter = self._inflight.pop(message_id, None)
        if reply is not None and self.state.persistence is not None:
            self.state.persistence.save_handled_message(message_id, rider_id, reply)
        if waiter is not None:
            waiter.set()

    # -- main entry point -------------------------------------------------

    def handle(self, message: InboundMessage) -> str:
        rider_id = canonical_rider_id(message.rider_id)
        should_process, stored = self._claim_message(message.message_id)

        if not should_process:
            tracer = Tracer(self.state, rider_id, message.message_id)
            tracer.decision(
                "duplicate_message_ignored",
                {"message_id": message.message_id},
                {
                    "action": "none",
                    "rule": "idempotent_replay",
                    "reason": (
                        "Vendor re-sent a message we have already handled. Returning the "
                        "stored reply without re-running tools or re-paying."
                    ),
                },
            )
            tracer.flush()
            return stored or "Aapka message mil gaya hai, main check kar raha hoon."

        reply = None
        try:
            reply = self._process(message, rider_id)
            return reply
        except Exception as exc:  # noqa: BLE001 - must always answer the vendor
            tracer = Tracer(self.state, rider_id, message.message_id)
            tracer.error("unhandled_exception", {"message_id": message.message_id}, str(exc))
            tracer.flush()
            self._open_ops_item(
                rider_id=rider_id,
                item_type="escalation",
                amount=None,
                reason=f"Agent failed while handling this message: {exc}",
                created_at=message.received_at,
                detail={"message_id": message.message_id, "text": message.text},
            )
            reply = (
                "Aapka message mil gaya hai. Ek technical dikkat aa gayi, "
                "main ise ops team ko bhej raha hoon."
            )
            return reply
        finally:
            self._release_message(message.message_id, reply, rider_id)

    # -- the pipeline -----------------------------------------------------

    def _process(self, message: InboundMessage, rider_id: str) -> str:
        tracer = Tracer(self.state, rider_id, message.message_id)
        tracer.message_in(message.message_id, message.text, message.received_at)

        # Event time, not processing time.
        received = ist_now_from(message.received_at)
        anchor_day = received.date()

        if not self.ref.rider_exists(rider_id):
            tracer.decision(
                "unknown_rider",
                {"rider_id": rider_id},
                {"action": "escalation", "rule": "rider_not_in_export"},
            )
            self._open_ops_item(
                rider_id=rider_id,
                item_type="escalation",
                amount=None,
                reason="Message from a rider id that is not in the riders export.",
                created_at=message.received_at,
                detail={"message_id": message.message_id, "text": message.text},
            )
            tracer.reply("Aapka number humare rider record se match nahi ho raha.")
            tracer.flush()
            return "Aapka number humare rider record se match nahi ho raha. Ops team check karegi."

        grounding = ground(message.text, anchor_day)
        tracer.tool_call(
            "ground_references",
            {"text": message.text, "anchor_day": str(anchor_day)},
            grounding.as_trace(),
        )

        claim_set = self._classify(message.text, anchor_day, tracer, rider_id)

        budget = self._seed_budget(rider_id, anchor_day, tracer)

        conversation = self._conversation(rider_id)
        with self.state.lock:
            compensated = set(self.state.compensated.get(rider_id, set()))

        decisions = decide(
            self.ref,
            rider_id,
            anchor_day,
            claim_set,
            budget,
            has_open_question=conversation.get("open_question", False),
            compensated=compensated,
        )

        for decision in decisions:
            tracer.decision(decision.rule, {"claim": str(decision.claim.kind) if decision.claim else None}, decision.as_trace())

        payment_results = self._execute(
            rider_id, anchor_day, decisions, message, tracer
        )

        reply = replies.compose(decisions, payment_results)
        reply = self._polish(
            reply,
            message.text,
            tracer,
            must_mention=replies.required_mentions(decisions, payment_results, reply),
        )

        tracer.reply(reply)
        self._remember(rider_id, message, decisions, reply)
        tracer.flush()
        return reply

    def _classify(
        self, text: str, anchor_day: date, tracer: Tracer, rider_id: str | None = None
    ) -> ClaimSet:
        deterministic = classify_deterministic(text, anchor_day, rider_id)

        if not is_model_configured():
            tracer.tool_call(
                "classify_claims",
                {"text": text, "layer": "deterministic_only"},
                deterministic.as_trace(),
            )
            return deterministic

        model_set, error = classify_with_model(text, anchor_day)
        if model_set is None:
            tracer.tool_call(
                "classify_claims",
                {"text": text, "layer": "llm_failed_fell_back"},
                {"error": error, **deterministic.as_trace()},
            )
            return deterministic

        merged = merge_claim_sets(model_set, deterministic)
        tracer.tool_call(
            "classify_claims",
            {"text": text, "layer": "llm+deterministic"},
            {
                "llm": model_set.as_trace(),
                "deterministic": deterministic.as_trace(),
                "merged": merged.as_trace(),
            },
        )
        return merged

    def _seed_budget(self, rider_id: str, anchor_day: date, tracer: Tracer) -> AutopayBudget:
        """Finance: once per rider per day, reconciled against PaySwift."""
        with self.state.lock:
            epoch = set(self.state.ledger_epoch)
            used_local = len(self.state.autopay_log.get((rider_id, anchor_day), []))
        used_remote = self.payswift.autopay_count_for_day(
            rider_id, anchor_day, AUTOPAY_MARKER, exclude_payout_ids=epoch
        )
        used = max(used_remote, used_local)
        tracer.tool_call(
            "check_autopay_budget",
            {"rider_id": rider_id, "day": str(anchor_day)},
            {
                "from_payswift_ledger": used_remote,
                "from_local_state": used_local,
                "ledger_epoch_size": len(epoch),
                "used": used,
                "limit": 1,
            },
        )
        return AutopayBudget(rider_id=rider_id, day=anchor_day, already_used=used)

    def _execute(
        self,
        rider_id: str,
        anchor_day: date,
        decisions: list[Decision],
        message: InboundMessage,
        tracer: Tracer,
    ) -> dict[int, object]:
        results: dict[int, object] = {}

        for index, decision in enumerate(decisions):
            if decision.action in (Action.PAY, Action.APPROVAL) and decision.settlement:
                # Reserve the components before acting, so a second message about
                # the same order cannot be compensated again while this one is in
                # flight.
                with self.state.lock:
                    self.state.compensated[rider_id].update(
                        decision.settlement.component_keys
                    )
                if self.state.persistence is not None:
                    self.state.persistence.save_compensated(
                        rider_id, decision.settlement.component_keys
                    )

            if decision.action == Action.PAY:
                dispute_id = self.state.next_dispute_id(rider_id)
                reference = payout_reference(dispute_id, rider_id, anchor_day, [decision.rule])

                # Reserve today's auto-pay slot BEFORE sending, not after it is
                # confirmed. The provider stalls for seconds at a time, so a
                # second message can arrive while the first payment is still in
                # flight; counting only confirmed payments let both auto-pay and
                # broke Finance's one-per-rider-per-day rule.
                reservation = {
                    "dispute_id": dispute_id,
                    "amount": int(decision.amount or 0),
                    "payout_id": None,
                    "confirmed": False,
                }
                with self.state.lock:
                    self.state.autopay_log[(rider_id, anchor_day)].append(reservation)

                result = self.payswift.pay(
                    rider_id=rider_id,
                    amount=int(decision.amount or 0),
                    reference=reference,
                    dispute_id=dispute_id,
                )
                results[index] = result
                tracer.tool_call(
                    "payswift_payout",
                    {
                        "rider_id": rider_id,
                        "amount": decision.amount,
                        "reference": reference,
                        "dispute_id": dispute_id,
                    },
                    result.as_trace(),
                )

                if result.money_moved:
                    with self.state.lock:
                        reservation["payout_id"] = result.payout_id
                        reservation["confirmed"] = True
                    if self.state.persistence is not None:
                        self.state.persistence.save_autopay(rider_id, anchor_day, reservation)
                else:
                    # Unconfirmed is not the same as unpaid: the provider drops
                    # responses and stalls for longer than we are allowed to
                    # wait. Open an item so ops can see it, then let the
                    # reconciler keep working on it in the background -- it
                    # closes the item itself if the money turns out to be there.
                    item = self._open_ops_item(
                        rider_id=rider_id,
                        item_type="escalation",
                        amount=decision.amount,
                        reason=(
                            f"Auto-payment of Rs {decision.amount} not yet confirmed by "
                            f"PaySwift ({result.error or result.outcome}). "
                            "Reconciling in the background."
                        ),
                        created_at=message.received_at,
                        detail={
                            "dispute_id": dispute_id,
                            "reference": reference,
                            "payswift": result.as_trace(),
                        },
                    )
                    with self.state.lock:
                        self.state.reserved_by_item[item.id] = (
                            rider_id,
                            set(decision.settlement.component_keys) if decision.settlement else set(),
                        )
                    if self.reconciler is not None:
                        self.reconciler.enqueue(
                            rider_id=rider_id,
                            amount=int(decision.amount or 0),
                            reference=reference,
                            dispute_id=dispute_id,
                            ops_item_id=item.id,
                            day=anchor_day,
                        )

            elif decision.action == Action.APPROVAL:
                item = self._open_ops_item(
                    rider_id=rider_id,
                    item_type="approval",
                    amount=int(decision.amount or 0),
                    reason=decision.reason,
                    created_at=message.received_at,
                    detail={
                        "rule": decision.rule,
                        "settlement": decision.settlement.as_trace() if decision.settlement else None,
                        "message_id": message.message_id,
                        "components": sorted(
                            decision.settlement.component_keys if decision.settlement else []
                        ),
                    },
                )
                if decision.settlement:
                    with self.state.lock:
                        self.state.reserved_by_item[item.id] = (
                            rider_id,
                            set(decision.settlement.component_keys),
                        )
                tracer.tool_call(
                    "open_ops_approval",
                    {"rider_id": rider_id, "amount": decision.amount},
                    {"id": item.id, "reason": item.reason},
                )

            elif decision.action == Action.ESCALATION:
                item = self._open_ops_item(
                    rider_id=rider_id,
                    item_type="escalation",
                    amount=None,
                    reason=decision.reason,
                    created_at=message.received_at,
                    detail={
                        "rule": decision.rule,
                        "rider_text": message.text,
                        "settlement": decision.settlement.as_trace() if decision.settlement else None,
                        "message_id": message.message_id,
                    },
                )
                tracer.tool_call(
                    "open_ops_escalation",
                    {"rider_id": rider_id, "rule": decision.rule},
                    {"id": item.id, "reason": item.reason},
                )

        return results

    def _polish(
        self,
        reply: str,
        rider_text: str,
        tracer: Tracer,
        must_mention: list[str] | None = None,
    ) -> str:
        """Optional model pass over wording only.

        The facts are already in `reply`. If a model is configured it may rewrite
        the phrasing, and the result is rejected unless every number and order id
        from the template survives -- so polish can never change the substance.
        """
        if not is_model_configured():
            return reply
        from app.llm import polish_reply

        polished, error = polish_reply(reply, rider_text, must_mention)
        if polished is None:
            # Rejected or unavailable: the deterministic draft goes out unchanged.
            tracer.tool_call(
                "write_reply",
                {"template": reply, "must_mention": must_mention},
                {"used": "template", "rejected_because": error},
            )
            return reply
        tracer.tool_call(
            "write_reply",
            {"template": reply, "must_mention": must_mention},
            {"used": "model", "polished": polished},
        )
        return polished

    # -- state ------------------------------------------------------------

    def _conversation(self, rider_id: str) -> dict:
        with self.state.lock:
            return dict(self.state.conversations.get(rider_id, {}))

    def _remember(
        self,
        rider_id: str,
        message: InboundMessage,
        decisions: list[Decision],
        reply: str,
    ) -> None:
        with self.state.lock:
            thread = self.state.conversations.setdefault(
                rider_id, {"turns": [], "open_question": False, "pending": []}
            )
            thread["turns"].append(
                {
                    "message_id": message.message_id,
                    "text": message.text,
                    "received_at": message.received_at,
                    "reply": reply,
                    "actions": [d.action for d in decisions],
                    "rules": [d.rule for d in decisions],
                }
            )
            thread["open_question"] = any(d.action == Action.CLARIFY for d in decisions)
            thread["pending"] = [
                {"amount": d.amount, "rule": d.rule}
                for d in decisions
                if d.action in (Action.APPROVAL, Action.ESCALATION)
            ]
            snapshot = dict(thread)
        if self.state.persistence is not None:
            self.state.persistence.save_conversation(rider_id, snapshot)

    def _open_ops_item(
        self,
        rider_id: str,
        item_type: str,
        amount: int | None,
        reason: str,
        created_at: str,
        detail: dict,
    ) -> OpsItem:
        with self.state.lock:
            self.state.ops_seq += 1
            item_id = f"OPS{self.state.ops_seq:05d}"
            item = OpsItem(
                id=item_id,
                rider_id=rider_id,
                type=item_type,
                amount=amount,
                reason=reason,
                created_at=created_at,
                detail=detail,
            )
            self.state.ops_items[item_id] = item
        if self.state.persistence is not None:
            self.state.persistence.save_ops_item(item)
        return item
