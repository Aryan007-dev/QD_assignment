"""Cross-referencing the PaySwift ledger against what the agent believes it paid.

Pure functions, no I/O, so the reconciliation logic can be tested without a
provider. The endpoint in app/main.py supplies the two inputs and serves the
result.

Finance reconcile against PaySwift rather than against us, so the useful output
is not the list of payouts -- it is the three disagreements an ops executive has
to resolve before trusting a number:

  unconfirmed          we sent it and never heard back
  missing_from_ledger  we hold a confirmed payout id the ledger does not have
  unattributed         the ledger has a payout no dispute of ours explains
"""

from __future__ import annotations

SOURCE_AGENT = "agent"
SOURCE_OPS = "ops"
SOURCE_EXTERNAL = "external"

OPS_RULE = "ops_approved"


def parse_reference(reference: str) -> dict[str, str]:
    """Pull the key=value fields out of a payout reference.

    References are written by app.payswift.payout_reference, but this also sees
    payouts made by hand outside the agent, so anything unparseable yields {}
    rather than raising.
    """
    fields: dict[str, str] = {}
    for part in str(reference or "").split():
        if part.count("=") == 1:
            key, value = part.split("=", 1)
            if key and value:
                fields[key] = value
    return fields


def classify_source(reference: str, rule: str, marker: str) -> str:
    if rule == OPS_RULE:
        return SOURCE_OPS
    if str(reference or "").startswith(marker):
        return SOURCE_AGENT
    return SOURCE_EXTERNAL


def describe_payouts(raw: list[dict], marker: str) -> tuple[list[dict], set[str]]:
    """Annotate each ledger row with the dispute and rule that authorised it."""
    payouts: list[dict] = []
    disputes: set[str] = set()

    for entry in raw:
        reference = str(entry.get("reference", ""))
        fields = parse_reference(reference)
        dispute_id = fields.get("dispute", "")
        rule = fields.get("rules", "")
        if dispute_id:
            disputes.add(dispute_id)
        payouts.append(
            {
                "payout_id": entry.get("payout_id"),
                "rider_id": entry.get("rider_id"),
                "amount": entry.get("amount"),
                "status": entry.get("status"),
                "created_at": entry.get("created_at"),
                "dispute_id": dispute_id,
                "day": fields.get("day"),
                "rule": rule,
                "source": classify_source(reference, rule, marker),
                "reference": reference,
            }
        )

    payouts.sort(key=lambda p: str(p.get("created_at") or ""), reverse=True)
    return payouts, disputes


def find_discrepancies(
    autopay_log: dict,
    disputes_in_ledger: set[str],
) -> tuple[list[dict], list[dict]]:
    """Split our own records into 'never confirmed' and 'confirmed but absent'.

    The distinction matters: the first is in flight and the reconciler is still
    working on it, the second means the ledger lost something we believe we
    paid, which no amount of retrying will fix on its own.
    """
    unconfirmed: list[dict] = []
    missing: list[dict] = []

    for (rider_id, day), entries in autopay_log.items():
        for entry in entries:
            dispute_id = entry.get("dispute_id", "")
            if dispute_id and dispute_id in disputes_in_ledger:
                continue
            row = {
                "rider_id": rider_id,
                "day": str(day),
                "amount": entry.get("amount"),
                "dispute_id": dispute_id,
                "payout_id": entry.get("payout_id"),
            }
            if entry.get("confirmed"):
                row["note"] = "we hold a confirmed payout id the ledger does not have"
                missing.append(row)
            else:
                row["note"] = "sent to PaySwift, no confirmation yet"
                unconfirmed.append(row)

    return unconfirmed, missing


def totals(payouts: list[dict]) -> dict[str, int]:
    by_rider: dict[str, int] = {}
    for payout in payouts:
        try:
            amount = int(payout.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        rider = str(payout.get("rider_id"))
        by_rider[rider] = by_rider.get(rider, 0) + amount
    return by_rider


def build(
    raw: list[dict],
    autopay_log: dict,
    marker: str,
    *,
    payswift_reachable: bool,
    payments_reconciling: int,
) -> dict:
    payouts, disputes = describe_payouts(raw, marker)
    unconfirmed, missing = find_discrepancies(autopay_log, disputes)
    by_rider = totals(payouts)

    return {
        "payswift_reachable": payswift_reachable,
        "total_paid": sum(by_rider.values()),
        "payout_count": len(payouts),
        "rider_count": len(by_rider),
        "by_rider": by_rider,
        "payouts": payouts,
        "unconfirmed": unconfirmed,
        "missing_from_ledger": missing,
        "unattributed": [p for p in payouts if p["source"] == SOURCE_EXTERNAL],
        "payments_reconciling": payments_reconciling,
        "reconciles": not (unconfirmed or missing),
    }
