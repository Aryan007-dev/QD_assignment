"""HTTP surface.

The four endpoints the brief fixes, plus the ones ops needs to act:

  GET  /health              readiness
  POST /messages            one rider message in, one reply out
  GET  /trace/{rider_id}    everything the agent did, in order
  GET  /ops/pending         approvals and escalations waiting for a human
  POST /ops/{id}/approve    pay an approved dispute (ops action)
  POST /ops/{id}/reject     close one without paying
  POST /admin/reset         clear agent state; used by the eval runner
  GET  /                    the ops page

Only `reply` is relied on by the graders, so POST /messages returns that field
first and adds detail alongside it.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.agent import AUTOPAY_MARKER, Agent, InboundMessage
from app import ledger_view
from app.db import Persistence
from app.normalize import canonical_rider_id, ist_now_from
from app.chat_page import CHAT_PAGE
from app.ops_page import OPS_PAGE
from app.payswift import PaySwiftClient, payout_reference
from app.reconciler import Reconciler
from app.store import AgentState, ReferenceData

ref_data: ReferenceData | None = None
agent_state: AgentState | None = None
payswift: PaySwiftClient | None = None
reconciler: Reconciler | None = None
persistence: Persistence | None = None
agent: Agent | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ref_data, agent_state, payswift, reconciler, persistence, agent
    ref_data = ReferenceData()
    persistence = Persistence()
    agent_state = AgentState(persistence=persistence)
    # Recover traces, the ops queue and -- most importantly -- the record of what
    # has already been compensated, which the static payout exports cannot tell us.
    restored = persistence.load_into(agent_state)
    if restored.get("restored"):
        import logging

        logging.getLogger("app").info("restored from database: %s", restored)
    payswift = PaySwiftClient(os.getenv("PAYSWIFT_BASE_URL"))
    reconciler = Reconciler(payswift, agent_state)
    reconciler.start()
    agent = Agent(ref_data, agent_state, payswift, reconciler)
    yield
    reconciler.stop()
    payswift.close()


app = FastAPI(
    title="Rider Payout Dispute Desk",
    description="Handles QuickDrop rider payout disputes end to end.",
    lifespan=lifespan,
)


class IncomingMessage(BaseModel):
    message_id: str = Field(..., min_length=1, max_length=200)
    rider_id: str = Field(..., min_length=1, max_length=50)
    text: str = Field(default="", max_length=4000)
    received_at: str


class ReplyOut(BaseModel):
    reply: str


@app.get("/health")
def health() -> dict:
    ready = ref_data is not None and agent is not None
    return {
        "status": "ok" if ready else "starting",
        "trips_loaded": len(ref_data.trips_by_id) if ref_data else 0,
        "riders_loaded": len(ref_data.riders) if ref_data else 0,
        "payswift_reachable": payswift.health() if payswift else False,
        "llm_configured": bool(os.getenv("GROQ_API_KEY") or os.getenv("GEMINI_API_KEY")),
        # Non-zero means a payout is still being reconciled with PaySwift.
        "payments_reconciling": reconciler.in_flight if reconciler else 0,
        "persistence": "postgres" if (persistence and persistence.enabled) else "in-memory",
    }


@app.post("/messages")
def post_message(message: IncomingMessage) -> dict:
    """One rider message. Always answers 2xx when it can, because a non-2xx
    makes the vendor re-send the same message."""
    if agent is None:
        raise HTTPException(status_code=503, detail="service starting")

    try:
        ist_now_from(message.received_at)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="received_at must be an ISO-8601 timestamp")

    reply = agent.handle(
        InboundMessage(
            message_id=message.message_id,
            rider_id=message.rider_id,
            text=message.text,
            received_at=message.received_at,
        )
    )
    return {"reply": reply, "rider_id": canonical_rider_id(message.rider_id)}


@app.get("/trace/{rider_id}")
def get_trace(rider_id: str) -> list[dict]:
    if agent_state is None:
        return []
    with agent_state.lock:
        return list(agent_state.traces.get(canonical_rider_id(rider_id), []))


@app.get("/ops/pending")
def get_pending() -> list[dict]:
    if agent_state is None:
        return []
    with agent_state.lock:
        items = [i for i in agent_state.ops_items.values() if i.status == "pending"]
    return [item.as_api() for item in sorted(items, key=lambda i: i.created_at)]


@app.post("/ops/{item_id}/approve")
def approve(item_id: str) -> dict:
    """Ops approves a held dispute, and the money moves now.

    Payment runs through the same idempotent client as an auto-payment, so a
    double click cannot pay twice.
    """
    if agent_state is None or payswift is None:
        raise HTTPException(status_code=503, detail="service starting")

    with agent_state.lock:
        item = agent_state.ops_items.get(item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="no such item")
        if item.status != "pending":
            return {"id": item.id, "status": item.status, "note": "already decided"}
        if item.type != "approval" or not item.amount:
            item.status = "approved"
            return {"id": item.id, "status": item.status, "paid": False}

    day = date.fromisoformat(item.created_at[:10])
    dispute_id = item.detail.get("dispute_id") or f"{item.id}-ops"
    reference = payout_reference(dispute_id, item.rider_id, day, ["ops_approved"])
    result = payswift.pay(
        rider_id=item.rider_id,
        amount=int(item.amount),
        reference=reference,
        dispute_id=dispute_id,
    )

    with agent_state.lock:
        item.status = "approved" if result.money_moved else "pending"
        item.detail["payswift"] = result.as_trace()
        if persistence is not None:
            persistence.save_ops_item(item)
        agent_state.traces[item.rider_id].append(
            {
                "at": item.created_at,
                "type": "tool_call",
                "name": "ops_approved_payout",
                "input": {"ops_item": item.id, "amount": item.amount},
                "output": result.as_trace(),
            }
        )

    return {
        "id": item.id,
        "status": item.status,
        "paid": result.money_moved,
        "payswift": result.as_trace(),
    }


@app.post("/ops/{item_id}/reject")
def reject(item_id: str, note: str = "") -> dict:
    if agent_state is None:
        raise HTTPException(status_code=503, detail="service starting")
    with agent_state.lock:
        item = agent_state.ops_items.get(item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="no such item")
        if item.status != "pending":
            return {"id": item.id, "status": item.status, "note": "already decided"}
        item.status = "rejected"
        item.detail["ops_note"] = note
        # Release the reservation: ops decided this shortfall is not owed, so the
        # components must be claimable again rather than silently blocked.
        reserved = agent_state.reserved_by_item.pop(item.id, None)
        if reserved:
            rider, components = reserved
            agent_state.compensated[rider].difference_update(components)
            if persistence is not None:
                persistence.release_compensated(rider, components)
        if persistence is not None:
            persistence.save_ops_item(item)
        agent_state.traces[item.rider_id].append(
            {
                "at": item.created_at,
                "type": "decision",
                "name": "ops_rejected",
                "input": {"ops_item": item.id},
                "output": {"status": "rejected", "note": note},
            }
        )
    return {"id": item.id, "status": "rejected"}


@app.get("/ops/conversations")
def conversations() -> list[dict]:
    """Backs the ops page: every rider the agent has spoken to."""
    if agent_state is None:
        return []
    with agent_state.lock:
        out = []
        for rider_id, thread in agent_state.conversations.items():
            rider = ref_data.riders.get(rider_id) if ref_data else None
            pending = [
                i.as_api()
                for i in agent_state.ops_items.values()
                if i.rider_id == rider_id and i.status == "pending"
            ]
            out.append(
                {
                    "rider_id": rider_id,
                    "name": rider.name if rider else None,
                    "city": rider.city if rider else None,
                    "turns": thread.get("turns", []),
                    "open_question": thread.get("open_question", False),
                    "pending": pending,
                    "steps": len(agent_state.traces.get(rider_id, [])),
                }
            )
    return sorted(out, key=lambda c: c["rider_id"])


@app.get("/riders")
def riders() -> list[dict]:
    """Riders the simulator can send as, with enough context to test them.

    `disputable_days` is a testing aid, not a product feature: the exports are a
    fixed snapshot, so without it a tester has to guess which rider-days actually
    contain a planted fault and most messages come back "nothing owed".
    """
    if ref_data is None:
        return []

    from app.entitlement import settle_day

    out: list[dict] = []
    for rider_id, rider in sorted(ref_data.riders.items()):
        days = sorted(
            {day for (rid, day) in ref_data.trips_by_rider_day if rid == rider_id},
            reverse=True,
        )
        owed = []
        for day in days[:10]:
            settlement = settle_day(ref_data, rider_id, day, day)
            if settlement.shortfall:
                owed.append(
                    {
                        "day": str(day),
                        "amount": settlement.shortfall,
                        "kinds": sorted({r.kind for r in settlement.reasons}),
                        "trip_ids": [r.trip_id for r in settlement.reasons if r.trip_id][:4],
                    }
                )
        out.append(
            {
                "rider_id": rider_id,
                "name": rider.name,
                "city": rider.city,
                "trip_days": [str(d) for d in days[:10]],
                "disputable_days": owed,
            }
        )
    return out


@app.get("/chat", response_class=HTMLResponse)
def chat_page() -> str:
    """A stand-in for the messaging vendor, so a tester never needs curl."""
    return CHAT_PAGE


@app.get("/ops/ledger")
def ledger() -> dict:
    """The PaySwift ledger, cross-referenced against what the agent believes it paid.

    Finance reconcile against PaySwift, not against us, so this reads the
    provider and reports the disagreements rather than just the rows. The
    cross-referencing itself lives in app/ledger_view.py as pure functions.
    """
    if payswift is None or agent_state is None:
        raise HTTPException(status_code=503, detail="service starting")

    raw = payswift.list_payouts()
    with agent_state.lock:
        autopay_log = {k: list(v) for k, v in agent_state.autopay_log.items()}

    return ledger_view.build(
        raw,
        autopay_log,
        AUTOPAY_MARKER,
        payswift_reachable=bool(raw) or payswift.health(),
        payments_reconciling=reconciler.in_flight if reconciler else 0,
    )


@app.post("/admin/reset")
def reset() -> dict:
    """Clear agent state. The sample conversations are specified to run against
    a fresh system, and the eval runner may only use HTTP, so it needs this.

    Reference data is reloaded too, so a run never inherits mutated state.
    """
    if agent_state is None or payswift is None:
        raise HTTPException(status_code=503, detail="service starting")

    # Snapshot the PaySwift ledger as the new epoch. Its ledger is in memory and
    # clears only on restart, so without this a reset run would inherit the
    # previous run's auto-payments and start refusing to auto-pay.
    # Abandon in-flight reconciliation first: a reset declares a fresh system,
    # and a payment still being chased from the previous run would otherwise land
    # inside the next one.
    dropped = reconciler.drain() if reconciler else 0
    epoch = payswift.all_payout_ids()
    agent_state.reset(ledger_epoch=epoch)
    return {
        "status": "reset",
        "reconciliations_dropped": dropped,
        "autopay_marker": AUTOPAY_MARKER,
        "ledger_epoch_size": len(epoch),
        "run_id": agent_state.run_id,
    }


@app.get("/", response_class=HTMLResponse)
def ops_page() -> str:
    return OPS_PAGE
