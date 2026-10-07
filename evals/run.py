#!/usr/bin/env python3
"""Eval runner.

    python evals/run.py http://localhost:8000

Uses only the public interface (POST /messages, GET /trace, GET /ops/pending)
and the PaySwift ledger, because the same runner has to score other people's
implementations.

Scoring, per the contract in data/README.md:
  payout      rupees that reached the rider in PaySwift because of this case
  approval    rupees sitting in GET /ops/pending as type "approval"
  escalation  required | optional | no
  mentions    facts the reply has to contain

Isolation: each case runs against a fresh system. The runner asks the service to
reset (POST /admin/reset, which is an extra endpoint, not part of the required
interface) and measures PaySwift as a *delta* per case, so it still scores
correctly against a service that has no reset endpoint and against a PaySwift
whose ledger cannot be cleared.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

import scenarios

sys.path.insert(0, str(Path(__file__).resolve().parent))
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PAYSWIFT = os.getenv("PAYSWIFT_BASE_URL", "http://localhost:8081")

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)


def colour(text: str, code: str) -> str:
    return text if os.getenv("NO_COLOR") else f"{code}{text}{RESET}"


class Harness:
    def __init__(self, service_url: str, payswift_url: str, timeout: float = 20.0):
        self.service = service_url.rstrip("/")
        self.payswift = payswift_url.rstrip("/")
        self.http = httpx.Client(timeout=timeout)
        self.can_reset = True

    # -- service ---------------------------------------------------------

    def wait_until_ready(self, seconds: float = 30.0) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                if self.http.get(f"{self.service}/health", timeout=3.0).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        return False

    def reset(self) -> bool:
        if not self.can_reset:
            return False
        try:
            response = self.http.post(f"{self.service}/admin/reset", timeout=10.0)
            if response.status_code == 404:
                self.can_reset = False
                return False
            return response.status_code < 300
        except httpx.HTTPError:
            return False

    def send(self, turn: dict, rider_id: str) -> tuple[str, float, str | None]:
        payload = {
            "message_id": turn["message_id"],
            "rider_id": rider_id,
            "text": turn["text"],
            "received_at": turn["received_at"],
        }
        started = time.time()
        try:
            response = self.http.post(f"{self.service}/messages", json=payload)
        except httpx.HTTPError as exc:
            return "", time.time() - started, f"{type(exc).__name__}"
        elapsed = time.time() - started
        if response.status_code >= 300:
            return "", elapsed, f"http_{response.status_code}"
        try:
            return str(response.json().get("reply", "")), elapsed, None
        except ValueError:
            return "", elapsed, "non_json_reply"

    def still_reconciling(self) -> bool:
        """Optional signal: a service may report payouts it is still chasing.

        Nothing depends on it -- an implementation without the field just gets
        the base settle window.
        """
        try:
            body = self.http.get(f"{self.service}/health", timeout=3.0).json()
        except (httpx.HTTPError, ValueError):
            return False
        try:
            return int(body.get("payments_reconciling", 0)) > 0
        except (TypeError, ValueError):
            return False

    def pending(self) -> list[dict]:
        try:
            response = self.http.get(f"{self.service}/ops/pending", timeout=10.0)
            body = response.json()
            return body if isinstance(body, list) else body.get("data", [])
        except (httpx.HTTPError, ValueError):
            return []

    def trace(self, rider_id: str) -> list[dict]:
        try:
            body = self.http.get(f"{self.service}/trace/{rider_id}", timeout=10.0).json()
            return body if isinstance(body, list) else []
        except (httpx.HTTPError, ValueError):
            return []

    # -- payswift (the ledger of record) ---------------------------------

    def ledger(self, rider_id: str) -> list[dict]:
        try:
            body = self.http.get(
                f"{self.payswift}/v1/payouts", params={"rider_id": rider_id}, timeout=10.0
            ).json()
        except (httpx.HTTPError, ValueError):
            return []
        if isinstance(body, dict):
            for key in ("data", "payouts", "items", "results"):
                if isinstance(body.get(key), list):
                    return body[key]
            return []
        return body if isinstance(body, list) else []

    def paid_total(self, rider_id: str) -> tuple[int, set[str]]:
        total, ids = 0, set()
        for payout in self.ledger(rider_id):
            payout_id = str(payout.get("payout_id", ""))
            if payout_id in ids:
                continue
            ids.add(payout_id)
            try:
                total += int(payout.get("amount", 0))
            except (TypeError, ValueError):
                pass
        return total, ids


def _expected_payout_targets(expected: dict) -> set[int]:
    """Amounts that would let us stop waiting. Includes 0, so a case that should
    pay nothing settles immediately instead of burning the whole timeout."""
    if "one_of" in expected:
        return {int(o.get("payout") or 0) for o in expected["one_of"]}
    return {int(expected.get("payout") or 0)}


def run_case(harness: Harness, case: dict, index: int) -> dict:
    rider_id = case["rider_id"]
    expected = case.get("expected", {})

    harness.reset()
    before_total, before_ids = harness.paid_total(rider_id)

    replies: list[str] = []
    errors: list[str] = []
    slowest = 0.0

    turns = case.get("turns") or [case]
    for turn in turns:
        if turn.get("from") == "agent":
            continue
        if "message_id" not in turn:
            continue
        reply, elapsed, error = harness.send(turn, rider_id)
        slowest = max(slowest, elapsed)
        replies.append(reply)
        if error:
            errors.append(f"{turn['message_id']}: {error}")

    # Wait for the ledger to settle before scoring.
    #
    # A service may confirm a payout after it has replied -- PaySwift stalls for
    # 8 seconds on some calls, which is longer than anyone can hold a reply open
    # given the vendor re-sends after ~10. So poll until the expected amount has
    # arrived, and stop early as soon as it has. This only costs time when the
    # provider actually misbehaved, and it uses nothing but the ledger, so it
    # scores any implementation fairly.
    expected_payout = _expected_payout_targets(expected)
    wants_no_escalation = expected.get("escalation", "no") == "no"

    # The provider blocks an account for 60s once its write limit is tripped, so a
    # settle window shorter than that turns a provider hiccup into a false
    # failure. Clean runs never pay this cost: polling stops the moment the money
    # arrives.
    base_timeout = float(os.getenv("EVAL_SETTLE_SECONDS", "75"))
    hard_timeout = float(os.getenv("EVAL_SETTLE_HARD_CAP", "150"))
    deadline = time.time() + base_timeout
    after_total, after_ids = harness.paid_total(rider_id)
    while time.time() < deadline:
        money_settled = (after_total - before_total) in expected_payout
        # A service that reconciles in the background may still be holding an
        # item open for a payout that has in fact landed, so when the case is
        # not supposed to escalate, wait for that to clear too.
        queue_settled = True
        if money_settled and wants_no_escalation:
            queue_settled = not [
                p
                for p in harness.pending()
                if p.get("rider_id") == rider_id and p.get("type") == "escalation"
            ]
        if money_settled and queue_settled:
            break
        # If the service says it is still reconciling a payout, give it longer --
        # but only up to a hard cap, and only if it volunteers that signal.
        # Implementations that do not expose it simply use the base window.
        if time.time() > deadline - 1.0 and harness.still_reconciling():
            deadline = min(deadline + 15.0, time.time() + hard_timeout)
        time.sleep(1.0)
        after_total, after_ids = harness.paid_total(rider_id)
    paid = after_total - before_total
    new_payouts = len(after_ids - before_ids)

    pending = [p for p in harness.pending() if p.get("rider_id") == rider_id]
    approvals = sum(
        int(p.get("amount") or 0) for p in pending if p.get("type") == "approval"
    )
    escalations = [p for p in pending if p.get("type") == "escalation"]

    blob = " ".join(replies)
    missing = [
        str(m) for m in expected.get("reply_mentions", []) if str(m) not in blob
    ]

    # -- score -----------------------------------------------------------
    failures: list[str] = []

    if "one_of" in expected:
        options = expected["one_of"]
        if not any(
            paid == int(o.get("payout") or 0) and approvals == int(o.get("approval") or 0)
            for o in options
        ):
            shapes = " | ".join(
                f"pay={o.get('payout')} appr={o.get('approval')}" for o in options
            )
            failures.append(f"paid={paid} appr={approvals}, expected one of [{shapes}]")
    else:
        want_paid = int(expected.get("payout") or 0)
        want_appr = int(expected.get("approval") or 0)
        if paid != want_paid:
            failures.append(f"payout {paid} != {want_paid}")
        if approvals != want_appr:
            failures.append(f"approval {approvals} != {want_appr}")

    want_escalation = expected.get("escalation", "no")
    if want_escalation == "required" and not escalations:
        failures.append("escalation required but none raised")
    elif want_escalation == "no" and escalations:
        failures.append(f"escalated when not expected ({escalations[0].get('reason','')[:60]})")

    if missing:
        failures.append(f"reply missing {missing}")
    if errors:
        failures.append(f"transport {errors}")
    if replies and not replies[-1].strip():
        failures.append("empty reply")

    return {
        "index": index,
        "rider_id": rider_id,
        "scenario": case.get("scenario", ""),
        "paid": paid,
        "payouts": new_payouts,
        "approvals": approvals,
        "escalations": len(escalations),
        "expected": expected,
        "failures": failures,
        "passed": not failures,
        "replies": replies,
        "slowest_ms": int(slowest * 1000),
    }


def expectation_text(expected: dict) -> str:
    if "one_of" in expected:
        return "one_of " + "/".join(
            f"{o.get('payout')}+{o.get('approval')}" for o in expected["one_of"]
        )
    return f"pay={expected.get('payout') or 0} appr={expected.get('approval') or 0}"


def report(title: str, results: list[dict]) -> tuple[int, int]:
    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    if not total:
        return 0, 0

    print(f"\n{colour(BOLD + title + RESET, BOLD)}")
    print(f"{DIM}{'':3} {'rider':6} {'paid':>6} {'appr':>6} {'esc':>4} {'ms':>6}  expected{RESET}")
    for r in results:
        mark = colour(" ok ", GREEN) if r["passed"] else colour("FAIL", RED)
        print(
            f"{mark} {r['index']:<3} {r['rider_id']:6} {r['paid']:>6} {r['approvals']:>6} "
            f"{r['escalations']:>4} {r['slowest_ms']:>6}  {expectation_text(r['expected'])}"
            f"  {DIM}{r['scenario'][:42]}{RESET}"
        )
        for failure in r["failures"]:
            print(f"      {colour('-> ' + failure, RED)}")

    rate = 100.0 * passed / total
    code = GREEN if passed == total else (YELLOW if rate >= 80 else RED)
    print(colour(f"\n  {passed}/{total} passed ({rate:.0f}%)", code))
    return passed, total


def load_cases(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path) as handle:
        return json.load(handle)


def main() -> int:
    parser = argparse.ArgumentParser(description="Score the dispute desk against its eval set.")
    parser.add_argument("service_url", nargs="?", default="http://localhost:8000")
    parser.add_argument("--payswift", default=DEFAULT_PAYSWIFT)
    parser.add_argument("--only", type=int, action="append", help="run only these case numbers")
    parser.add_argument("--skip-extra", action="store_true", help="provided cases only")
    parser.add_argument("--json", dest="json_out", help="write the full result to this path")
    parser.add_argument(
        "--chaos",
        action="store_true",
        help="also run the slow provider-failure scenarios (outage, write-limit storm)",
    )
    parser.add_argument("--skip-scenarios", action="store_true", help="conversations only")
    args = parser.parse_args()

    harness = Harness(args.service_url, args.payswift)

    print(f"{BOLD}Rider Payout Dispute Desk - evals{RESET}")
    print(f"  service  {args.service_url}")
    print(f"  payswift {args.payswift}")

    if not harness.wait_until_ready():
        print(colour(f"\n  service at {args.service_url} never became ready", RED))
        return 2
    if not harness.ledger("R001") and not harness.reset():
        pass  # both are advisory; the run continues either way

    provided = load_cases(ROOT / "data" / "conversations.json")
    extra = [] if args.skip_extra else load_cases(Path(__file__).parent / "cases_extra.json")

    results_provided = [
        run_case(harness, case, i)
        for i, case in enumerate(provided, 1)
        if not args.only or i in args.only
    ]
    results_extra = [
        run_case(harness, case, i)
        for i, case in enumerate(extra, 1)
        if not args.only
    ]

    if not harness.can_reset:
        print(
            colour(
                "\n  note: service has no POST /admin/reset, so cases were not isolated.\n"
                "        payouts were still scored as a per-case delta against PaySwift.",
                YELLOW,
            )
        )

    passed_p, total_p = report("Provided eval set (data/conversations.json)", results_provided)
    passed_e, total_e = report("Our adversarial set (evals/cases_extra.json)", results_extra)

    scenario_results = []
    if not args.skip_scenarios and not args.only:
        scenario_results = scenarios.run(harness, include_chaos=args.chaos)

    passed_s = total_s = 0
    if scenario_results:
        print(f"\n{colour(BOLD + 'Failure-mode scenarios' + RESET, BOLD)}")
        for result in scenario_results:
            ok = not result["failures"]
            total_s += 1
            passed_s += ok
            mark = colour(" ok ", GREEN) if ok else colour("FAIL", RED)
            if result.get("skipped"):
                mark = colour("skip", YELLOW)
            print(f"{mark} {result['name']:52} {DIM}{result['detail']}{RESET}")
            for failure in result["failures"]:
                print(f"      {colour('-> ' + failure, RED)}")
        rate_colour = GREEN if passed_s == total_s else RED
        print(colour(f"\n  {passed_s}/{total_s} passed", rate_colour))
        if not args.chaos:
            print(f"  {DIM}(run with --chaos for the outage and write-limit scenarios){RESET}")

    passed, total = passed_p + passed_e + passed_s, total_p + total_e + total_s
    print(f"\n{BOLD}  overall {passed}/{total}{RESET}")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    "service": args.service_url,
                    "provided": results_provided,
                    "extra": results_extra,
                    "scenarios": scenario_results,
                    "passed": passed,
                    "total": total,
                },
                indent=2,
            )
        )
        print(f"  wrote {args.json_out}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
