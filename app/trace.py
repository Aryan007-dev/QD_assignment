"""The audit trail.

Meera asked for a team that "needs to see what happened and why", so a step is
not just an API log line: decision steps carry the rule that fired and the
figures it used, which is what makes a payout arguable after the fact.

Shape is fixed by SUBMISSION.md: at, type, name, input, output, where type is
one of message_in | tool_call | decision | reply | error.
"""

from __future__ import annotations

from datetime import datetime

from app.normalize import IST

MESSAGE_IN = "message_in"
TOOL_CALL = "tool_call"
DECISION = "decision"
REPLY = "reply"
ERROR = "error"


def _now() -> str:
    return datetime.now(IST).isoformat()


class Tracer:
    """Collects the steps for one inbound message, then appends them to the
    rider's trace in one go so a half-processed message never leaves a
    half-written story."""

    def __init__(self, state, rider_id: str, message_id: str | None = None):
        self.state = state
        self.rider_id = rider_id
        self.message_id = message_id
        self.steps: list[dict] = []

    def _add(self, step_type: str, name: str, payload_in, payload_out) -> dict:
        step = {
            "at": _now(),
            "type": step_type,
            "name": name,
            "input": payload_in,
            "output": payload_out,
            # Extra beyond the required shape: lets a reader group the steps that
            # one inbound message caused, which is how both ops and the message
            # simulator read the trace.
            "message_id": self.message_id,
        }
        self.steps.append(step)
        return step

    def message_in(self, message_id: str, text: str, received_at: str) -> None:
        self.message_id = message_id
        self._add(
            MESSAGE_IN,
            "rider_message",
            {"message_id": message_id, "text": text, "received_at": received_at},
            None,
        )

    def tool_call(self, name: str, payload_in: dict, payload_out) -> None:
        self._add(TOOL_CALL, name, payload_in, payload_out)

    def decision(self, name: str, payload_in: dict, payload_out: dict) -> None:
        self._add(DECISION, name, payload_in, payload_out)

    def reply(self, text: str, name: str = "reply_to_rider") -> None:
        self._add(REPLY, name, None, text)

    def error(self, name: str, payload_in: dict, message: str) -> None:
        self._add(ERROR, name, payload_in, {"message": message})

    def flush(self) -> list[dict]:
        with self.state.lock:
            self.state.traces[self.rider_id].extend(self.steps)
        flushed, self.steps = self.steps, []
        if getattr(self.state, "persistence", None) is not None:
            self.state.persistence.save_steps(self.rider_id, flushed)
        return flushed
