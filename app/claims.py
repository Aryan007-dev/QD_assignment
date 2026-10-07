"""Turning a rider's message into typed claims.

This is the only place a language model is involved in deciding anything, and
its output type is deliberately incapable of moving money: there is no field on
Claim that becomes a payment. The rupee figure always comes from
app/entitlement.py. `amount_claimed` is carried for the trace and then dropped.

Four layers, strongest first:
  1. native JSON-schema mode at the provider, so off-schema tokens cannot be
     sampled in the first place;
  2. Pydantic validation, with one bounded repair retry;
  3. a deterministic clause classifier, which is also the only layer CI uses --
     the eval suite has to run with no API key;
  4. if the two layers disagree about a *payable* kind, escalate instead of
     paying.
"""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from app.grounding import Grounding, ground


class ClaimKind(StrEnum):
    # Payable: the amount is computed from the ledger, never from the message.
    SURGE_MISSING = "surge_missing"
    TRIP_UNPAID = "trip_unpaid"
    INCENTIVE_MISSING = "incentive_missing"
    PENALTY_WRONG = "penalty_wrong"
    # Not verifiable from the exports: a human has to look.
    DISTANCE_WRONG = "distance_wrong"
    CANCELLATION_REASON = "cancellation_reason"
    DISPUTE_RECORDS = "dispute_records"
    # Adversarial: never acted on, always logged.
    IDENTITY_CLAIM = "identity_claim"
    INJECTION = "injection"
    DATA_REQUEST = "data_request"
    # Conversational.
    STATUS_QUERY = "status_query"
    VAGUE = "vague"


PAYABLE_KINDS = {
    ClaimKind.SURGE_MISSING,
    ClaimKind.TRIP_UNPAID,
    ClaimKind.INCENTIVE_MISSING,
    ClaimKind.PENALTY_WRONG,
}

NEEDS_HUMAN_KINDS = {
    ClaimKind.DISTANCE_WRONG,
    ClaimKind.CANCELLATION_REASON,
    ClaimKind.DISPUTE_RECORDS,
}

ADVERSARIAL_KINDS = {
    ClaimKind.IDENTITY_CLAIM,
    ClaimKind.INJECTION,
    ClaimKind.DATA_REQUEST,
}


class Claim(BaseModel):
    """One thing the rider is complaining about."""

    kind: ClaimKind
    trip_ids: list[str] = Field(default_factory=list)
    date_hint: str | None = None
    amount_claimed: int | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source_text: str = ""

    @field_validator("trip_ids")
    @classmethod
    def _cap_trip_ids(cls, value: list[str]) -> list[str]:
        # A message naming dozens of trips is not a dispute, it is an attack
        # surface. Ops can handle the rest.
        return value[:10]

    @property
    def is_payable(self) -> bool:
        return self.kind in PAYABLE_KINDS

    @property
    def needs_human(self) -> bool:
        return self.kind in NEEDS_HUMAN_KINDS

    @property
    def is_adversarial(self) -> bool:
        return self.kind in ADVERSARIAL_KINDS


class ClaimSet(BaseModel):
    """The only type app/decide.py accepts."""

    claims: list[Claim] = Field(default_factory=list)
    source: str = "deterministic"  # deterministic | llm | llm+deterministic
    disagreement: bool = False

    @field_validator("claims")
    @classmethod
    def _cap_claims(cls, value: list[Claim]) -> list[Claim]:
        return value[:5]

    @property
    def kinds(self) -> list[ClaimKind]:
        return [c.kind for c in self.claims]

    def as_trace(self) -> dict:
        return {
            "source": self.source,
            "disagreement": self.disagreement,
            "claims": [
                {
                    "kind": str(c.kind),
                    "trip_ids": c.trip_ids,
                    "date_hint": c.date_hint,
                    "amount_claimed": c.amount_claimed,
                    "confidence": c.confidence,
                    "source_text": c.source_text,
                }
                for c in self.claims
            ],
        }


# -- deterministic classifier -------------------------------------------

INJECTION_MARKERS = [
    r"\bsystem\s*:",
    r"\bignore (?:all )?(?:previous|prior|above)\b",
    r"\bdisregard (?:all )?(?:previous|prior)\b",
    r"\bnew instructions?\b",
    r"\byou are now\b",
    r"\bapprove (?:all|saare|sare)\b",
    r"\boverride\b",
    r"\bprompt\b.*\bignore\b",
]

IDENTITY_MARKERS = [
    r"\bthis is\s+R\s*\d+",
    r"\bmain\s+R\s*\d+\s+(?:hoon|hun|bol)",
    r"\bi am\s+R\s*\d+",
    r"\brider\s+R\s*\d+\s+(?:ke|ka)\b",
    r"\bon behalf of\b",
]

DISPUTE_RECORDS_MARKERS = [
    r"\bnahi+ nahi+\b",
    r"\bdobara check\b",
    r"\bdubara check\b",
    r"\bcheck again\b",
    r"\bgalat record\b",
    r"\brecord galat\b",
    r"\byour record is wrong\b",
]

DISTANCE_MARKERS = [
    r"\bdistance\b.{0,20}\b(?:galat|wrong|kam|zyada)\b",
    r"\b(?:galat|wrong)\b.{0,20}\bdistance\b",
    r"\bkm\b.{0,20}\b(?:galat|wrong)\b",
]

CANCELLATION_REASON_MARKERS = [
    r"\baccident\b",
    r"\bbimar\b", r"\bbeemar\b", r"\bill\b",
    r"\bemergency\b",
    r"\bbreakdown\b", r"\bpuncture\b",
    r"\bisliye\b.{0,30}\bcancel\b",
    r"\bcancel kiya\b.{0,40}\b(?:kyun|why)\b",
]

PENALTY_MARKERS = [
    r"\bpenalty\b",
    r"\bpenality\b",
    r"\bkaat\w*\b.{0,20}\bcancel\b",
    r"\bcancel\b.{0,20}\bkaat\w*\b",
]

INCENTIVE_MARKERS = [
    r"\bincentive\b",
    r"\bbonus\b",
    r"\b(?:12|baarah)\s*(?:se\s*zyada\s*)?(?:order|orders|trip|trips)\b",
]

SURGE_MARKERS = [r"\bsurge\b", r"\bmultiplier\b", r"\b\d\.\dx\b", r"\b\dx\b"]

UNPAID_MARKERS = [
    r"\b(?:payment|paisa|paise|paisaa|amount|money)\b.{0,25}\b(?:nahi|nhi|missing|not)\b",
    r"\b(?:nahi|nhi|missing|not)\b.{0,25}\b(?:payment|paisa|paise|amount|money)\b",
    r"\bmissing hai\b",
    r"\bnot (?:received|paid|credited)\b",
    r"\bi got only\b",
    r"\bonly (?:rs|₹)\s*\d+\b",
]

VAGUE_MARKERS = [
    r"\bpayout\b.{0,20}\b(?:galat|kam|wrong|less|short)\b",
    r"\b(?:galat|kam|wrong|less|short)\b.{0,20}\bpayout\b",
    r"\bkam aaya\b",
    r"\bgalat aaya\b",
]

STATUS_MARKERS = [
    r"\bkab tak\b", r"\bkab aayega\b", r"\bkab milega\b",
    r"\bwhen will\b", r"\bhow long\b", r"\bstatus\b",
    r"\babhi de do\b", r"\babhi do\b", r"\bjaldi do\b",
    r"\bthik hai\b", r"\bthanks\b", r"\bthank you\b", r"\bok\b",
]

# Another rider's id, or a request for someone's personal details.
OTHER_RIDER_ID = re.compile(r"\b[Rr]0*(\d{1,4})\b")
PII_MARKERS = [
    r"\bphone\b", r"\bnumber bhej\b", r"\bmobile\b", r"\baddress\b",
    r"\bdetails bhej\b", r"\bkiska\b", r"\bkitna hua\b",
]

# Clause boundaries. Semicolons are deliberately NOT a boundary: they appear in
# pasted payloads far more often than in how riders actually write, and splitting
# on one made a single message produce a spurious second, empty complaint.
_CLAUSE_SPLIT = re.compile(r"\s+(?:aur|and)\s+|\n")


def _matches(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def _segment(text: str) -> list[str]:
    """Split a message into complaint clauses.

    Only on explicit conjunctions -- never on commas, because riders use commas
    to list trip ids ("T795007, T206956") and splitting there would scatter one
    claim across two.
    """
    parts = [p.strip() for p in _CLAUSE_SPLIT.split(text or "") if p and p.strip()]
    return parts or [text or ""]


def _classify_clause(clause: str) -> ClaimKind:
    """Most specific kind wins. Order is the policy."""
    if _matches(clause, INJECTION_MARKERS):
        return ClaimKind.INJECTION
    if _matches(clause, IDENTITY_MARKERS):
        return ClaimKind.IDENTITY_CLAIM
    if _matches(clause, DISPUTE_RECORDS_MARKERS):
        return ClaimKind.DISPUTE_RECORDS
    if _matches(clause, DISTANCE_MARKERS):
        return ClaimKind.DISTANCE_WRONG
    if _matches(clause, CANCELLATION_REASON_MARKERS):
        return ClaimKind.CANCELLATION_REASON
    if _matches(clause, PENALTY_MARKERS):
        return ClaimKind.PENALTY_WRONG
    if _matches(clause, SURGE_MARKERS):
        return ClaimKind.SURGE_MISSING
    if _matches(clause, INCENTIVE_MARKERS):
        return ClaimKind.INCENTIVE_MISSING
    if _matches(clause, UNPAID_MARKERS):
        return ClaimKind.TRIP_UNPAID
    if _matches(clause, VAGUE_MARKERS):
        return ClaimKind.VAGUE
    if _matches(clause, STATUS_MARKERS):
        return ClaimKind.STATUS_QUERY
    return ClaimKind.VAGUE


def mentions_other_rider(text: str, rider_id: str | None) -> str | None:
    """Does the message name a rider other than the sender?

    Identity comes from the phone number the message arrived on, so any other
    rider id in the body is either an impersonation attempt or a request for
    somebody else's data. Both are for a human.
    """
    if not rider_id:
        return None
    for match in OTHER_RIDER_ID.finditer(text or ""):
        named = "R%03d" % int(match.group(1))
        if named != rider_id:
            return named
    return None


def classify_deterministic(
    text: str,
    anchor: date,
    rider_id: str | None = None,
) -> ClaimSet:
    """Rule-based classifier. Runs with no API key, so CI and the eval suite
    never depend on a provider being reachable."""
    claims: list[Claim] = []

    # Adversarial content is judged on the whole message, not per clause: an
    # injection buried after a legitimate complaint still taints the message.
    whole = text or ""
    if _matches(whole, INJECTION_MARKERS):
        return ClaimSet(
            claims=[
                Claim(
                    kind=ClaimKind.INJECTION,
                    confidence=1.0,
                    source_text=whole[:500],
                )
            ]
        )
    if _matches(whole, IDENTITY_MARKERS):
        return ClaimSet(
            claims=[
                Claim(
                    kind=ClaimKind.IDENTITY_CLAIM,
                    confidence=1.0,
                    source_text=whole[:500],
                )
            ]
        )

    other = mentions_other_rider(whole, rider_id)
    if other and _matches(whole, PII_MARKERS):
        # Asking what another rider was paid, or for their contact details.
        return ClaimSet(
            claims=[
                Claim(
                    kind=ClaimKind.DATA_REQUEST,
                    confidence=1.0,
                    source_text=whole[:500],
                )
            ]
        )

    for clause in _segment(whole):
        grounding = ground(clause, anchor)
        kind = _classify_clause(clause)
        claims.append(
            Claim(
                kind=kind,
                trip_ids=grounding.trip_ids,
                date_hint=str(grounding.days[0]) if grounding.days else None,
                amount_claimed=grounding.amount_claimed,
                confidence=0.9 if kind is not ClaimKind.VAGUE else 0.5,
                source_text=clause[:300],
            )
        )

    # A clause with no scope of its own inherits scope from its siblings, so
    # "20 ko surge nahi mila aur penalty bhi kata" keeps both on day 20.
    known_days = [c.date_hint for c in claims if c.date_hint]
    for claim in claims:
        if not claim.date_hint and not claim.trip_ids and known_days:
            claim.date_hint = known_days[0]

    # Collapse duplicate (kind, scope) pairs produced by repetitive phrasing.
    deduped: list[Claim] = []
    seen: set[tuple] = set()
    for claim in claims:
        key = (claim.kind, tuple(claim.trip_ids), claim.date_hint)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(claim)

    # Drop filler when a real complaint is present in the same message: "surge
    # nahi mila, thanks" is one claim, not two. A vague fragment is dropped the
    # same way -- otherwise a message that both names an order and trails off
    # gets answered and then asked about, in the same breath.
    substantive = [
        c
        for c in deduped
        if c.kind not in (ClaimKind.STATUS_QUERY, ClaimKind.VAGUE)
    ]
    if substantive:
        deduped = substantive

    return ClaimSet(claims=deduped or [Claim(kind=ClaimKind.VAGUE, source_text=whole[:300])])


def merge_claim_sets(primary: ClaimSet, fallback: ClaimSet) -> ClaimSet:
    """Reconcile the model's reading with the rule-based one.

    The useful question is not "did these two produce identical output" -- they
    rarely will, because one is a language model and the other is regexes. It is
    "do they disagree about something that changes what we do".

      * Either layer spotting an attack is enough to stop acting on the message.
      * If they name any kind in common they agree on the substance; the extra
        facets one of them saw are kept and routed individually, because a
        message can legitimately be both a payable claim and something a human
        must look at.
      * If one reads money owed and the other reads a claim only a human can
        verify, take the cautious reading.
      * Only when one sees money owed and the other sees no claim at all is there
        a real conflict -- and then we pay nothing, because we cannot corroborate
        the reading that would move money.

    An earlier version flagged any difference as a disagreement, which told a
    rider explaining an accident that we could not understand them -- when both
    layers had understood perfectly and merely differed in detail.
    """
    adversarial = [c for c in (*primary.claims, *fallback.claims) if c.is_adversarial]
    if adversarial:
        return ClaimSet(
            claims=[adversarial[0]], source="llm+deterministic", disagreement=False
        )

    primary_kinds, fallback_kinds = set(primary.kinds), set(fallback.kinds)
    primary_payable = primary_kinds & PAYABLE_KINDS
    fallback_payable = fallback_kinds & PAYABLE_KINDS
    primary_human = primary_kinds & NEEDS_HUMAN_KINDS
    fallback_human = fallback_kinds & NEEDS_HUMAN_KINDS

    if primary_kinds & fallback_kinds:
        # They agree on at least one reading. Keep the union so nothing either
        # layer noticed is lost; decide() routes each claim on its own.
        claims = list(primary.claims)
        for claim in fallback.claims:
            if claim.kind not in primary_kinds:
                claims.append(claim)
        return _with_deterministic_scope(
            ClaimSet(claims=claims, source="llm+deterministic", disagreement=False),
            fallback,
        )

    if (primary_payable and fallback_human) or (fallback_payable and primary_human):
        # One layer reads money owed, the other reads something only a human can
        # verify. Keep both rather than picking: decide() routes each claim on its
        # own, so the verifiable part gets settled and the unverifiable part goes
        # to ops.
        #
        # Discarding the payable claim here was worse than it sounds -- a rider
        # who attaches a false premise to a genuine shortfall ("policy says 200
        # minimum, check the 18th") stopped being paid the Rs150 they were
        # actually owed. Nothing is paid that is not computed from the ledger, so
        # keeping both is safe as well as more useful.
        claims = list(primary.claims)
        for claim in fallback.claims:
            if claim.kind not in primary_kinds:
                claims.append(claim)
        return _with_deterministic_scope(
            ClaimSet(claims=claims, source="llm+deterministic", disagreement=False),
            fallback,
        )

    disagreement = bool(primary_payable) != bool(fallback_payable)
    chosen = primary.claims or fallback.claims
    return _with_deterministic_scope(
        ClaimSet(claims=chosen, source="llm+deterministic", disagreement=disagreement),
        fallback,
    )


def _with_deterministic_scope(merged: ClaimSet, fallback: ClaimSet) -> ClaimSet:
    """Scope is arithmetic, so the rule layer wins on trip ids and dates wherever
    the model left them empty."""
    by_kind = {c.kind: c for c in fallback.claims}
    for claim in merged.claims:
        twin = by_kind.get(claim.kind)
        if twin is None:
            continue
        if not claim.trip_ids and twin.trip_ids:
            claim.trip_ids = twin.trip_ids
        if not claim.date_hint and twin.date_hint:
            claim.date_hint = twin.date_hint
    return merged
