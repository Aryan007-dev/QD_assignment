"""The guards around the model: schema shape, hint resolution, fact preservation.

No network. These test the code that surrounds the provider call, which is where
every safety property actually lives.
"""

from datetime import date

import pytest

from app.claims import Claim, ClaimKind, ClaimSet
from app.decide import Action, AutopayBudget, decide
from app.llm import (
    CLAIM_KINDS,
    CLAIM_SCHEMA,
    _gemini_schema,
    _resolve_date_hints,
    is_model_configured,
    polish_reply,
)
from app.store import ReferenceData

ANCHOR = date(2026, 9, 23)


def test_schema_is_valid_for_openai_strict_mode():
    """Strict mode requires every property to be listed in `required`.

    Groq answers 400 if it is not, so this is a hard contract, not a nicety.
    """
    item = CLAIM_SCHEMA["properties"]["claims"]["items"]
    assert set(item["required"]) == set(item["properties"]), (
        "strict mode needs every property in `required`"
    )
    assert item["additionalProperties"] is False
    assert CLAIM_SCHEMA["required"] == ["claims"]


def test_optional_fields_are_declared_nullable_not_omitted():
    props = CLAIM_SCHEMA["properties"]["claims"]["items"]["properties"]
    assert props["date_hint"]["type"] == ["string", "null"]
    assert props["amount_claimed"]["type"] == ["integer", "null"]


def test_schema_cannot_express_a_payment():
    """The structural guarantee: no field the model fills can become an amount
    the system pays."""
    props = CLAIM_SCHEMA["properties"]["claims"]["items"]["properties"]
    assert "amount_claimed" in props          # traced only
    for forbidden in ("amount_to_pay", "payout", "approve", "pay", "amount_owed"):
        assert forbidden not in props


def test_schema_kinds_match_the_claim_enum():
    assert set(CLAIM_KINDS) == {str(k) for k in ClaimKind}


@pytest.mark.parametrize(
    "hint,expected",
    [
        ("20", "2026-09-20"),          # bare day, as riders write it
        ("21", "2026-09-21"),
        ("kal", "2026-09-22"),
        ("19 sept", "2026-09-19"),
        ("Sep 19", "2026-09-19"),
        ("2026-09-18", "2026-09-18"),  # already a date
        ("total nonsense", None),
        ("", None),
        (None, None),
    ],
)
def test_model_date_hints_are_resolved_by_code(hint, expected):
    """The model returns the date as the rider wrote it; resolving it is ours.

    Regression: an unresolved hint like "20" reached date.fromisoformat() and
    raised, which turned every dated dispute into an escalation the moment an API
    key was configured.
    """
    claim_set = ClaimSet(claims=[Claim(kind=ClaimKind.SURGE_MISSING, date_hint=hint)])
    _resolve_date_hints(claim_set, ANCHOR)
    assert claim_set.claims[0].date_hint == expected


def test_unparseable_hint_asks_the_rider_instead_of_raising():
    """Defence in depth: even if normalisation were skipped, no exception may
    reach a payment path."""
    ref = ReferenceData()
    claims = ClaimSet(claims=[Claim(kind=ClaimKind.SURGE_MISSING, date_hint="20")])
    decisions = decide(
        ref, "R003", ANCHOR, claims,
        AutopayBudget(rider_id="R003", day=ANCHOR),
    )
    assert decisions[0].action == Action.CLARIFY
    assert decisions[0].rule == "scope_unknown"


def test_gemini_schema_unpacks_nullable_unions():
    """Gemini wants `nullable: true`, not a ["string", "null"] union."""
    converted = _gemini_schema(CLAIM_SCHEMA)
    hint = converted["properties"]["claims"]["items"]["properties"]["date_hint"]
    assert hint["type"] == "STRING"
    assert hint["nullable"] is True
    assert "additionalProperties" not in converted["properties"]["claims"]["items"]


def test_polish_rejects_an_invented_amount(monkeypatch):
    import app.llm as llm

    monkeypatch.setattr(llm, "_provider", lambda: "groq")
    monkeypatch.setattr(llm, "_call_groq", lambda s, u, sc: "Order T926334 pe Rs99 bhej rahe hain.")
    polished, error = polish_reply("Order T926334 pe Rs25 bhej rahe hain.", "x", ["25"])
    assert polished is None and error.startswith("invented_numbers")


def test_polish_rejects_an_invented_delivery_promise(monkeypatch):
    """The guard also stops the model promising a timeline nobody authorised."""
    import app.llm as llm

    monkeypatch.setattr(llm, "_provider", lambda: "groq")
    monkeypatch.setattr(llm, "_call_groq", lambda s, u, sc: "Rs25 bhej diya, 2 din mein aayega.")
    polished, error = polish_reply("Order T926334 pe Rs25 bhej rahe hain.", "x", ["25"])
    assert polished is None and error.startswith("invented_numbers")


def test_polish_rejects_dropping_an_authoritative_figure(monkeypatch):
    import app.llm as llm

    monkeypatch.setattr(llm, "_provider", lambda: "groq")
    monkeypatch.setattr(llm, "_call_groq", lambda s, u, sc: "Aapka paisa bhej diya hai.")
    polished, error = polish_reply("Order T926334 pe Rs25 bhej rahe hain.", "x", ["25", "T926334"])
    assert polished is None and error.startswith("dropped_facts")


def test_polish_may_summarise_a_repetitive_draft(monkeypatch):
    """The whole point of the relaxed guard: five near-identical sentences may
    become one, as long as the total survives and nothing is invented."""
    import app.llm as llm

    draft = (
        "19 Sep ko aapke 5 orders ka payout bilkul nahi gaya tha "
        "(T481678, T900949, T563729), total Rs425. "
        "Rs425 ka amount auto-payment limit se zyada hai, isliye ops team approve karegi."
    )
    better = "19 Sep ke aapke 5 orders ka payout nahi gaya tha, total Rs425. Ye limit se zyada hai to ops team approve karegi."
    monkeypatch.setattr(llm, "_provider", lambda: "groq")
    monkeypatch.setattr(llm, "_call_groq", lambda s, u, sc: better)
    polished, error = polish_reply(draft, "x", ["425"])
    assert error is None
    assert polished == better
    assert "T481678" not in polished, "order ids may be summarised away"


def test_polish_rejects_mangling_a_required_order_id(monkeypatch):
    """Same digits, but 'T926334' is no longer an order id.

    A single-trip dispute always lists that id in must_mention (see
    replies.required_mentions), so this is caught as a dropped fact.
    """
    import app.llm as llm

    monkeypatch.setattr(llm, "_provider", lambda: "groq")
    monkeypatch.setattr(llm, "_call_groq", lambda s, u, sc: "926334 ka Rs25 bhej rahe hain.")
    polished, error = polish_reply(
        "Order T926334 pe Rs25 bhej rahe hain.", "x", ["25", "T926334"]
    )
    assert polished is None and error.startswith("dropped_facts")


def test_polish_rejects_fabricating_an_order_id(monkeypatch):
    """An id we never computed must never reach the rider."""
    import app.llm as llm

    monkeypatch.setattr(llm, "_provider", lambda: "groq")
    monkeypatch.setattr(llm, "_call_groq", lambda s, u, sc: "Order T926334 aur T999999 ka Rs25 bhej rahe hain.")
    polished, error = polish_reply(
        "Order T926334 pe Rs25 bhej rahe hain.", "x", ["25", "T926334"]
    )
    assert polished is None
    assert error.startswith("invented_numbers") or error.startswith("invented_trip_ids")


def test_polish_rejects_a_reply_that_drops_the_order_id_entirely(monkeypatch):
    import app.llm as llm

    monkeypatch.setattr(llm, "_provider", lambda: "groq")
    monkeypatch.setattr(llm, "_call_groq", lambda s, u, sc: "Rs25 bhej rahe hain.")
    polished, error = polish_reply("Order T926334 pe Rs25 bhej rahe hain.", "x", ["25", "T926334"])
    assert polished is None and error.startswith("dropped_facts")


def test_polish_accepts_a_pure_rewording(monkeypatch):
    import app.llm as llm

    monkeypatch.setattr(llm, "_provider", lambda: "groq")
    monkeypatch.setattr(llm, "_call_groq", lambda s, u, sc: "Order T926334 ka Rs25 bhej diya hai.")
    polished, error = polish_reply("Order T926334 pe Rs25 bhej rahe hain.", "x", ["25", "T926334"])
    assert polished == "Order T926334 ka Rs25 bhej diya hai." and error is None


def test_everything_degrades_without_a_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert is_model_configured() is False
    assert polish_reply("x", "y", []) == (None, "no_provider_configured")


def test_numeric_mentions_match_on_digit_boundaries():
    """"9" must not count as mentioned just because the reply says "19 Sep"."""
    from app.replies import mentions

    assert mentions("19 Sep ko total Rs425", "425") is True
    assert mentions("19 Sep ko total Rs425", "19") is True
    assert mentions("19 Sep ko total Rs425", "9") is False
    assert mentions("Order T926334 ka", "T926334") is True
