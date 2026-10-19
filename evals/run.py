"""Run the eval: every labelled incident through the real pipeline (real LLM, real DocPilot, fixture ops).

    python -m evals.run --out evals/results/2026-10-16                  # both configs, all incidents
    python -m evals.run --out ... --configs routed --limit 5 --tpm 8000 --rpm 30

Results are appended per (incident, config) to <out>/results.jsonl, so an interrupted run resumes where it
stopped. Provider failures (daily quota, outages) are not recorded as results; rerun to retry them.
Configs given together run interleaved per incident so they see the same conditions.

Incidents run in a fixed order that rotates through the categories (clear, ambiguous, multi-service,
malicious), so a partial run is a mixed sample rather than only the first category.

Exit codes: 0 all done, 2 daily quota reached, 3 some incidents skipped after provider errors.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from relay.agents.investigate import InvestigateAgent
from relay.agents.plan import PlanAgent
from relay.agents.qa import QAAgent
from relay.agents.triage import TriageAgent
from relay.config import Settings
from relay.graph import IncidentState, Pipeline
from relay.grounding import check_action
from relay.llm import ModelRouter
from relay.schemas import Alert
from relay.tools.docpilot import DocPilotClient

from .fixture_ops import FixtureOps

DATASET = Path(__file__).with_name("incidents.jsonl")
STRONG = "openai/gpt-oss-120b"
CONFIGS = {
    # Every agent on the strong model.
    "single": {f"model_{a}": STRONG for a in ("triage", "investigate", "plan", "qa")},
    # The shipped defaults: small fast model for triage, strong model for the rest.
    "routed": {},
}
PROVIDER_ERRORS = ("DailyLimitReached", "RateLimitError", "APIConnectionError", "APITimeoutError",
                   "InternalServerError", "APIStatusError", "DocPilotError")


def load_cases() -> list[dict]:
    return [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]


def stratified(cases: list[dict]) -> list[dict]:
    """Round-robin across categories, keeping dataset order within each, so any prefix is a mixed sample."""
    groups: dict[str, list[dict]] = {}
    for c in cases:
        groups.setdefault(c["category"], []).append(c)
    queues = [groups[k] for k in sorted(groups)]
    ordered = []
    while any(queues):
        for q in queues:
            if q:
                ordered.append(q.pop(0))
    return ordered


def record_for(
    case: dict, config: str, state: IncidentState, latency_s: float, waited_s: float, git_sha: str = ""
) -> dict:
    blocked = set(state.qa.blocked_actions) if state.qa else set()
    actions = []
    for a in state.plan.actions if state.plan else []:
        problems = check_action(a, a.id, state.triage, state.investigation) if state.investigation else []
        actions.append({
            "id": a.id, "kind": a.kind, "target": a.target, "title": a.title, "details": a.details,
            "status_update_draft": a.status_update_draft, "risk": a.risk, "requires_approval": a.requires_approval,
            "citations": [c.model_dump() for c in a.citations], "blocked": a.id in blocked,
            "grounded": not problems, "grounding_problems": problems,
        })
    by_agent: dict[str, dict] = {}
    for s in state.trace:
        agg = by_agent.setdefault(
            s.agent, {"model": s.model, "calls": 0, "tokens": 0, "cost_usd": 0.0, "latency_s": 0.0}
        )
        agg["calls"] += len(s.llm_calls)
        agg["tokens"] += s.prompt_tokens + s.completion_tokens
        agg["cost_usd"] = round(agg["cost_usd"] + s.cost_usd, 6)
        agg["latency_s"] = round(agg["latency_s"] + s.latency_ms / 1000, 2)
    return {
        "id": case["id"], "config": config, "category": case["category"],
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"), "git_sha": git_sha,
        "status": state.status, "failed_stage": state.failed_stage, "error": state.error,
        "triage": {"service": state.triage.service, "severity": state.triage.severity} if state.triage else None,
        "injection_flags": state.alert.injection_flags,
        "qa": {"verdict": state.qa.verdict, "loops": state.qa_loops, "confidence": state.qa.confidence,
               "escalation_reasons": state.qa.escalation_reasons} if state.qa else None,
        "actions": actions,
        "latency_s": round(latency_s, 2), "pacing_wait_s": round(waited_s, 2),
        "tokens": sum(a["tokens"] for a in by_agent.values()),
        "cost_usd": round(sum(a["cost_usd"] for a in by_agent.values()), 6),
        "by_agent": by_agent,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Relay eval.")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--configs", nargs="+", default=list(CONFIGS), choices=list(CONFIGS))
    parser.add_argument("--limit", type=int, help="first N incidents only")
    parser.add_argument("--ids", nargs="+", help="only these incident ids")
    parser.add_argument("--tpm", type=int, default=0, help="per-model tokens/minute to pace to (0 = off)")
    parser.add_argument("--rpm", type=int, default=0, help="per-model requests/minute to pace to (0 = off)")
    parser.add_argument("--attempts", type=int, default=2, help="tries per incident on provider errors")
    args = parser.parse_args()

    cases = stratified(load_cases())
    if args.ids:
        cases = [c for c in cases if c["id"] in set(args.ids)]
    cases = cases[: args.limit] if args.limit else cases
    args.out.mkdir(parents=True, exist_ok=True)
    results = args.out / "results.jsonl"
    done = set()
    if results.exists():
        done = {(r["id"], r["config"]) for r in map(json.loads, results.read_text().splitlines()) if r}

    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    routers, settings = {}, {}
    for name in args.configs:
        settings[name] = Settings(llm_tpm=args.tpm, llm_rpm=args.rpm, **CONFIGS[name])
        routers[name] = ModelRouter(settings[name])
    meta_path = args.out / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dataset_sha256": hashlib.sha256(DATASET.read_bytes()).hexdigest()[:16],
        "max_output_tokens": next(iter(settings.values())).llm_max_output_tokens,
        "tpm": args.tpm, "rpm": args.rpm, "configs": {}, "git_shas": [],
    }
    for n in args.configs:
        meta["configs"].setdefault(n, {a: settings[n].model_for(a) for a in ("triage", "investigate", "plan", "qa")})
    if sha not in meta["git_shas"]:
        meta["git_shas"].append(sha)
    meta_path.write_text(json.dumps(meta, indent=2))
    docpilot = DocPilotClient.from_settings(next(iter(settings.values())))

    todo = [(c, n) for c in cases for n in args.configs if (c["id"], n) not in done]
    print(f"{len(todo)} runs to do ({len(done)} already recorded)", flush=True)
    skipped = 0
    for i, (case, name) in enumerate(todo, 1):
        router, s = routers[name], settings[name]
        retries = s.llm_max_retries
        pipeline = Pipeline(
            TriageAgent(router.for_agent("triage"), docpilot, retries),
            InvestigateAgent(router.for_agent("investigate"), docpilot, FixtureOps(case), retries),
            PlanAgent(router.for_agent("plan"), retries),
            QAAgent(router.for_agent("qa"), retries, s.qa_max_loops, s.qa_min_confidence),
        )
        start = datetime.fromisoformat(case["start"].replace("Z", "+00:00"))
        for attempt in range(1, args.attempts + 1):
            alert = Alert.from_raw(case["alert"], received_at=start + timedelta(minutes=5))
            waited0, t0 = router.pacing_wait_s, time.perf_counter()
            state = pipeline.run(alert)
            latency, waited = time.perf_counter() - t0, router.pacing_wait_s - waited0
            provider_error = state.error and state.error.split(":")[0] in PROVIDER_ERRORS
            if not provider_error:
                break
            print(f"  provider error on {case['id']}/{name} (attempt {attempt}): {state.error[:200]}", flush=True)
            if state.error.startswith("DailyLimitReached"):
                print("Daily quota reached; rerun the same command later to resume.", flush=True)
                return 2
            time.sleep(30)
        else:
            print(f"  skipping {case['id']}/{name} after {args.attempts} provider errors", flush=True)
            skipped += 1
            continue
        record = record_for(case, name, state, latency, waited, sha)
        with results.open("a") as fh:
            fh.write(json.dumps(record) + "\n")
        print(f"[{i}/{len(todo)}] {case['id']:<36} {name:<7} {state.status:<17} "
              f"{latency:6.1f}s (paced {waited:5.1f}s) ${record['cost_usd']:.5f}", flush=True)
    return 3 if skipped else 0


if __name__ == "__main__":
    sys.exit(main())
