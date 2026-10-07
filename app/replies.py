"""Composing the reply the rider reads.

Code assembles a fact bundle from the decisions; this module turns it into
Hinglish. The facts are produced before any language is generated, so a model
polishing the wording cannot invent an amount or drop an order id -- the
sample conversations are checked on exactly those substrings.

Templates are the default rather than a fallback: they are reproducible, they
cost nothing, and in this domain the rider wants the number, not prose. A model
is used only to smooth phrasing when one is configured, and never to decide
what the facts are.
"""

from __future__ import annotations

import re
from datetime import date

from app.decide import Action, Decision
from app.policy import DISPUTE_WINDOW_DAYS

MONTH_SHORT = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}


def _day_text(value: str | date | None) -> str:
    if value is None:
        return ""
    day = value if isinstance(value, date) else date.fromisoformat(str(value))
    return f"{day.day} {MONTH_SHORT[day.month]}"


def _trip_list(trip_ids: list[str], limit: int = 3) -> str:
    """Name the orders only when there are few enough to be worth reading.

    Past three, the count and the total tell the rider more than a list of ids
    does, and the full breakdown is in the trace for ops.
    """
    ids = [t for t in trip_ids if t]
    if not ids or len(ids) > limit:
        return ""
    return ", ".join(ids)


def _aggregate_sentences(decision: Decision) -> list[str]:
    """Group reasons of the same kind into one sentence.

    A rider owed five unpaid orders does not want five near-identical sentences;
    they want the count, the total, and the orders. One sentence per reason reads
    like a machine filling in a form.
    """
    settlement = decision.settlement
    if settlement is None:
        return []

    buckets: dict[str, list] = {}
    for reason in settlement.reasons:
        buckets.setdefault(reason.kind, []).append(reason)

    out: list[str] = []
    for kind, reasons in buckets.items():
        if len(reasons) == 1:
            out.extend(_reason_sentences_for(reasons))
            continue

        total = sum(r.amount for r in reasons)
        day = _day_text(reasons[0].day)
        trips = _trip_list([r.trip_id for r in reasons])
        same_day = len({r.day for r in reasons}) == 1
        when = f"{day} ko " if same_day and day else ""

        if kind == "trip_unpaid":
            out.append(
                f"{when}aapke {len(reasons)} orders ka payout bilkul nahi gaya tha"
                f"{' (' + trips + ')' if trips else ''}, total Rs{total}."
            )
        elif kind == "surge_or_fare_short":
            out.append(
                f"{when}{len(reasons)} orders ka fare kam laga tha"
                f"{' (' + trips + ')' if trips else ''}, total Rs{total} ka farak hai."
            )
        elif kind == "penalty_duplicated":
            out.append(
                f"{when}{len(reasons)} orders pe cancel penalty do baar kata, "
                f"total Rs{total} extra kat gaya."
            )
        else:
            out.extend(_reason_sentences_for(reasons))
    return out


def _reason_sentences(decision: Decision) -> list[str]:
    """Figures for this decision, aggregated where there is more than one."""
    if decision.settlement is None:
        return []
    return _aggregate_sentences(decision)


def _reason_sentences_for(reasons: list) -> list[str]:
    """One sentence per component of the shortfall, each with its figures."""
    out: list[str] = []
    for reason in reasons:
        day = _day_text(reason.day)
        if reason.kind == "surge_or_fare_short":
            owed, paid = reason.detail.get("owed"), reason.detail.get("paid")
            surge = reason.detail.get("surge_multiplier")
            if surge and float(surge) > 1.0:
                out.append(
                    f"{day} ko order {reason.trip_id} pe {surge}x surge laga tha: "
                    f"Rs{owed} banta tha, Rs{paid} mila."
                )
            else:
                out.append(
                    f"{day} ko order {reason.trip_id} ka fare Rs{owed} banta tha, "
                    f"Rs{paid} mila."
                )
        elif reason.kind == "trip_unpaid":
            out.append(
                f"{day} ko order {reason.trip_id} ka Rs{reason.amount} payout "
                "bilkul nahi gaya tha."
            )
        elif reason.kind == "incentive_missing":
            trips = reason.detail.get("completed_trips")
            out.append(
                f"{day} ko aapke {trips} trips complete hue the, "
                f"to Rs{reason.amount} ka daily incentive banta tha."
            )
        elif reason.kind == "penalty_duplicated":
            times = reason.detail.get("times_charged")
            out.append(
                f"{day} ko order {reason.trip_id} ka cancel penalty {times} baar kata, "
                f"Rs{reason.amount} extra kat gaya."
            )
    return out


def _explain_nothing_owed(decision: Decision) -> str:
    """Explain why nothing is owed, answering the complaint that was raised.

    Earlier this concatenated a sentence for every flag, so a rider asking about
    one day's incentive was read the fare breakdown of all ten of their trips
    before reaching the answer. Flags are ranked instead, and only the ones that
    actually answer the question are said out loud -- the rest stay in the trace,
    where ops can see them.
    """
    settlement = decision.settlement
    if settlement is None:
        return "Humare record mein is par koi kami nahi mili."

    by_kind: dict[str, list] = {}
    for flag in settlement.flags:
        by_kind.setdefault(flag.kind, []).append(flag)

    # Most specific answer first.
    if "incentive_not_earned" in by_kind:
        flag = by_kind["incentive_not_earned"][0]
        return (
            f"{_day_text(flag.detail.get('day'))} ko aapke "
            f"{flag.detail.get('completed_trips')} trips complete hue the. "
            f"Incentive {flag.detail.get('threshold')} trips pe milta hai, isliye nahi bana."
        )

    if "incentive_already_paid" in by_kind:
        flag = by_kind["incentive_already_paid"][0]
        return (
            f"{_day_text(flag.detail.get('day'))} ka daily incentive "
            "already aapke payout mein gaya hai."
        )

    if "customer_cancellation" in by_kind:
        flag = by_kind["customer_cancellation"][0]
        return (
            f"Order {flag.trip_id} customer ne cancel kiya tha. Customer cancellation pe "
            "koi payment nahi banta, aur penalty bhi nahi lagti."
        )

    if "penalty_legitimate" in by_kind:
        flag = by_kind["penalty_legitimate"][0]
        return (
            f"Order {flag.trip_id} aapne cancel kiya tha, isliye Rs10 ka "
            "cancellation penalty laga hai."
        )

    if "unknown_trip" in by_kind:
        return f"Order {by_kind['unknown_trip'][0].trip_id} humare system mein nahi mil raha."

    if "no_trips_that_day" in by_kind:
        flag = by_kind["no_trips_that_day"][0]
        return f"{_day_text(flag.detail.get('day'))} ko humare record mein koi trip nahi hai."

    correct = by_kind.get("trip_paid_correctly", [])
    if len(correct) == 1:
        flag = correct[0]
        surge = flag.detail.get("surge_multiplier")
        surge_text = (
            f" {surge}x surge ke saath" if surge and float(surge) > 1.0
            else " (koi surge nahi tha)"
        )
        return (
            f"Order {flag.trip_id}: {flag.detail.get('distance_km')} km{surge_text}, "
            f"Rs{flag.detail.get('owed')} banta tha aur Rs{flag.detail.get('owed')} hi mila hai."
        )
    if correct:
        day = _day_text(correct[0].detail.get("day")) if correct[0].detail.get("day") else ""
        total = sum(int(f.detail.get("owed") or 0) for f in correct)
        return (
            f"{day + ' ko ' if day else ''}aapke {len(correct)} orders ka payout check kiya, "
            f"sab poora gaya hai - total Rs{total}."
        )

    return "Humare record ke hisaab se poora payment ho gaya hai."


def mentions(text: str, needle: str) -> bool:
    """Is `needle` present as a figure in its own right?

    A plain substring test is wrong for numbers: "9" is inside "19 Sep", so a
    reply about the 19th would appear to mention a trip count of 9 that it never
    stated. Numeric facts therefore have to match on their own digit boundaries.
    """
    if needle.isdigit():
        return re.search(rf"(?<!\d){re.escape(needle)}(?!\d)", text) is not None
    return needle in text


def required_mentions(
    decisions: list[Decision],
    payment_results: dict[int, object],
    draft: str = "",
) -> list[str]:
    """Facts the rider must be told, whoever writes the sentence.

    A model may reword or summarise the reply, but it may not drop these. The
    list is the authoritative figures only -- not everything the draft happened
    to say -- which is what lets the model turn five near-identical sentences
    into one readable one.

    Anything absent from `draft` is discarded: requiring a figure the
    deterministic reply does not itself contain would make the guard impossible
    to satisfy, so every rewrite would be rejected and the feature would
    silently do nothing.
    """
    must: list[str] = []

    for decision in decisions:
        if decision.action in (Action.PAY, Action.APPROVAL) and decision.amount:
            must.append(str(decision.amount))

        settlement = decision.settlement
        if settlement is None:
            continue

        # Name the orders only when there are few enough for it to help.
        trips = [r.trip_id for r in settlement.reasons if r.trip_id]
        if 0 < len(trips) <= 3:
            must.extend(trips)

        correct = [f for f in settlement.flags if f.kind == "trip_paid_correctly"]
        for flag in settlement.flags:
            if flag.kind == "incentive_not_earned":
                # Case 3 turns on the rider being told their real trip count.
                must.append(str(flag.detail.get("completed_trips")))
            elif flag.kind == "trip_paid_correctly" and flag.trip_id and len(correct) == 1:
                # Only when the rider named that one order. On a day scope the
                # other correctly-paid trips are not what was asked about.
                must.append(flag.trip_id)
                must.append(str(flag.detail.get("owed")))
            elif flag.kind in ("customer_cancellation", "penalty_legitimate") and flag.trip_id:
                must.append(flag.trip_id)
            elif flag.kind == "outside_window" and flag.detail.get("days_ago"):
                must.append(str(DISPUTE_WINDOW_DAYS))

    seen: set[str] = set()
    unique = [m for m in must if m and not (m in seen or seen.add(m))]
    if not draft:
        return unique
    return [m for m in unique if mentions(draft, m)]


def compose(decisions: list[Decision], payment_results: dict[int, object]) -> str:
    """Build the rider's reply from the decisions, in the order they were made."""
    sentences: list[str] = []

    for index, decision in enumerate(decisions):
        if decision.action == Action.CLARIFY:
            sentences.append(
                "Kaunse din ya kaunse order ka payout galat laga? "
                "Date ya order ID bata dijiye, main check kar deta hoon."
            )

        elif decision.action == Action.PAY:
            sentences.extend(_reason_sentences(decision))
            result = payment_results.get(index)
            moved = getattr(result, "money_moved", False)
            if moved:
                sentences.append(
                    f"Farak Rs{decision.amount} ka payment PaySwift pe process ho gaya hai."
                )
            else:
                sentences.append(
                    f"Rs{decision.amount} ka payment process kar rahe hain, "
                    "thodi der mein aa jayega."
                )

        elif decision.action == Action.APPROVAL:
            sentences.extend(_reason_sentences(decision))
            if decision.rule == "auto_pay_already_used_today":
                sentences.append(
                    f"Ek din mein ek hi auto-payment ho sakta hai, isliye Rs{decision.amount} "
                    "ops team approve karegi."
                )
            else:
                sentences.append(
                    f"Rs{decision.amount} ka amount auto-payment limit se zyada hai, "
                    "isliye ops team approve karegi. Approve hote hi aa jayega."
                )

        elif decision.action == Action.EXPLAIN:
            if decision.rule == "nothing_owed":
                sentences.append(_explain_nothing_owed(decision))
            elif decision.rule == "already_compensated":
                sentences.append(
                    "Is order ka farak hum already process kar chuke hain, "
                    "dobara payment nahi hoga. Agar abhi tak nahi aaya to ops team dekh legi."
                )
            elif decision.rule == "status_question":
                sentences.append(
                    "Aapka case process ho chuka hai. Payment PaySwift se aata hai, "
                    "pending approval ho to ops approve karte hi aa jayega."
                )

        elif decision.action == Action.ESCALATION:
            if decision.rule == "outside_dispute_window":
                flag = next(
                    (f for f in decision.settlement.flags if f.kind == "outside_window"),
                    None,
                ) if decision.settlement else None
                days_ago = flag.detail.get("days_ago") if flag else None
                sentences.append(
                    f"Ye order {days_ago} din purana hai aur hum sirf last 7 din ke "
                    "disputes dekh paate hain. Main ise ops team ko bhej raha hoon."
                    if days_ago
                    else "Ye dispute 7 din se purana hai, main ise ops team ko bhej raha hoon."
                )
            elif decision.rule == "rider_disputes_our_records":
                sentences.append(
                    "Dobara check kiya, humare record mein wahi number aa raha hai. "
                    "Agar aapko lagta hai record galat hai, main ise ops team ko bhej deta hoon."
                )
            elif decision.rule == "another_riders_data_requested":
                sentences.append(
                    "Main sirf isi number se jude rider ki payout details de sakta hoon. "
                    "Kisi dusre rider ki jaankari main share nahi kar sakta."
                )
            elif decision.rule == "identity_claim_ignored":
                sentences.append(
                    "Main sirf isi number se jude rider ka payout check kar sakta hoon. "
                    "Kisi dusre rider ke liye ops team se baat karni hogi."
                )
            elif decision.rule == "prompt_injection_attempt":
                sentences.append(
                    "Main sirf payout disputes check karta hoon aur policy ke bahar "
                    "payment nahi kar sakta. Aapka message ops team ko bhej diya hai."
                )
            elif decision.rule == "trip_belongs_to_another_rider":
                sentences.append(
                    "Ye order aapke record mein nahi mil raha. Main ise ops team se "
                    "check karwa raha hoon."
                )
            elif decision.rule == "unknown_trip_id":
                sentences.append(
                    "Ye order ID humare system mein nahi mili. Ek baar ID check kar ke "
                    "bata dijiye, ya main ops team se dekhwa leta hoon."
                )
            elif decision.rule == "classifier_disagreement":
                sentences.append(
                    "Aapki baat samajhne mein confusion hui, isliye main ise ops team ko "
                    "bhej raha hoon taaki koi galti na ho."
                )
            else:
                sentences.extend(_reason_sentences(decision))
                sentences.append(
                    "Is case ko verify karne ke liye main ise ops team ko bhej raha hoon, "
                    "wo aapko update denge."
                )

    # Deduplicate while keeping order: two claims can produce the same closing line.
    seen: set[str] = set()
    unique = [s for s in sentences if not (s in seen or seen.add(s))]
    return " ".join(unique) or "Aapka message mil gaya hai, main check kar raha hoon."
