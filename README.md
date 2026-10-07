# Rider Payout Dispute Desk

An agent that handles QuickDrop rider payout disputes end to end: it reads the message,
works out what the rider was owed, auto-pays small clear-cut shortfalls, sends anything bigger or
murkier to ops, and leaves a trace an ops executive can read.

## Run it

```bash
cp .env.example .env     # works as-is; an LLM key is optional
docker compose up        # app :8000, Postgres :5432, PaySwift :8081
```

- **`/chat`** — stands in for the messaging vendor, Pick a rider, type
  anything, see the reply and the rules that fired under it. **Send twice** and **Send 6× at once**
  reproduce a vendor re-send, sequentially and racing the original. Each rider shows what the export
  genuinely owes them. The date defaults to **22 Sep 2026**: exports cover 12–21 Sep and disputes
  cap at 7 days, so today's date would correctly answer "too old" to everything.
- **`/`** — ops: the queue awaiting a human, every conversation with its trace, and the PaySwift
  ledger reconciled against what the agent believes it paid.

```bash
python evals/run.py http://localhost:8000          # 38 cases + 3 race/guard scenarios
python evals/run.py http://localhost:8000 --chaos  # adds outage + write-limit scenarios (~4 min)
pytest tests/                                      # 176 unit tests, no services needed
```

## Design

**The model handles language; code handles money.** The type the model is constrained to has no
field that can become a payment, so "approve ₹999" has nothing to call. It decides which of 12 kinds
each complaint is, and the wording of the reply. Code decides scope, arithmetic, authority, payment
and escalation.

Per message (`agent.py`): deduplicate → anchor the clock to `received_at` → ground references →
classify → settle inside scope → route → act → reply → trace.

- **`grounding.py`** resolves "20 wala", "kal", `T926334`, and tells a date from a trip count
  ("18 ko 12 order kiye"). Fuzzy order matching is scoped to the asking rider *before* similarity, so
  a typo can never pay out on someone else's order.
- **`claims.py`** schema-constrained output → Pydantic validation → a deterministic regex classifier
  → reconciliation between them. "One says pay, the other says a human must verify" keeps both, so a
  false premise attached to a real claim doesn't cancel it. **With no API key the deterministic layer
  carries the whole eval suite** — which is how CI runs.
- **`entitlement.py`** `owed − paid`, positive deltas only, **inside the scope the rider raised**
  (named orders, else the named day). Overpayments are flagged, never netted or clawed back.
- **`decide.py`** ≤₹200 and today's first auto-pay → pay; else ops approval; unverifiable,
  adversarial, stale or another rider's order → escalation. The amount is always the computed
  shortfall: claim ₹300 against a ₹25 shortfall and you get ₹25.
- **`replies.py`** aggregates repeats so five unpaid orders read as a count and a total. A model may
  reword the draft but may invent **no** number (delivery promises included) and must keep the
  authoritative figures; a rejected rewrite falls back to the draft.
- **`payswift.py` / `reconciler.py`** stable `Idempotency-Key` per `(rider, dispute, amount)`, 6.5s
  budget, jittered backoff, and a **token bucket** that stays under the write limit rather than
  recovering from it. PaySwift stalls 8s while the vendor re-sends after ~10s — those don't both fit
  in one request, so unconfirmed payments go to a worker that retries with the same key for 200s,
  outlasting a 60s outage *and* a 60s rate-limit block.
- **`ledger_view.py` / `db.py`** reconcile PaySwift against our records — sent but unanswered,
  confirmed but absent from the ledger, present but unexplained — over state written through to
  Postgres. The critical row is which shortfalls are already compensated: lose it and a restart
  re-pays a settled dispute.

Adversarial input is handled structurally, not by prompt wording. Injection, impersonation and
requests for another rider's data are classified, logged verbatim, escalated, never acted on —
identity comes from the sending number, never the text.

## Assumptions

1. **Settle the claim, not the week.** Riders have other unclaimed shortfalls in the same 7 days; a
   whole-window audit over-pays on 4 of the 24 sample cases. Vague complaints get a question.
2. **Three export faults are formatting, not disputes.** `distance_km` holds metres in 222 rows
   (multiples of 100); `rider_id` needs canonicalising (`R7`, `r19`); 10 trip rows are exact
   duplicates, which left alone conjure incentives nobody earned. The metres reading is evidence, not
   assumption: read as metres, 202 of the 204 paid rows match policy fare exactly; as km, **zero** do.
3. **The 7-day window anchors to `received_at`, never the wall clock.** The exports are Sep 2026.
4. **A settled shortfall stays visible** in the static exports, so compensated components are tracked
   explicitly; otherwise a reworded repeat complaint is paid twice.
5. **An in-flight payment consumes the day's auto-pay slot.** Counting only *confirmed* payments let
   a stalled provider authorise two auto-payments to one rider.
6. **No RAG.** The policy is six bullets and goes in the prompt verbatim; the trips data is a SQL
   problem. Embeddings would add a retrieval-miss failure mode for no gain.
7. `POST /admin/reset` is an extra endpoint so the runner can honour "run each conversation against a
   fresh system". It snapshots the PaySwift ledger as a new epoch, since that ledger is in-memory.

## Eval results

| Suite | Result |
|---|---|
| `data/conversations.json` | **24 / 24** |
| `evals/cases_extra.json` — ceiling and window edges, injection after a real claim, unknown and foreign orders, inflated claim, empty message, unknown rider | **14 / 14** |
| Race and guard scenarios — 6 concurrent deliveries of one `message_id`; one shortfall complained about three ways; a ₹9999 claim | **3 / 3** |
| `--chaos` — 60s outage then recovery; 8 concurrent disputes against the write limit | **2 / 2** |
| Unit tests | **176 passed** |

Measured against the live sandbox at its defaults (15% errors, 10% 8-second stalls, 5% lost
responses, writes blocked 60s past ~30 in 10s) — not a mock. No dispute was ever paid twice across
repeated runs; the race scenario asserts it and `GET /ops/ledger` audits it.

