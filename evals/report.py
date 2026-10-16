"""Summarize an eval run: python -m evals.report evals/results/<run>  -> summary.json and summary.md."""

import json
import sys
from pathlib import Path

from .run import load_cases
from .scoring import summarize

ROWS = [
    ("Incidents scored", "incidents", ""),
    ("Runs that failed", "failed", ""),
    ("Severity accuracy", "severity_accuracy_pct", "%"),
    ("Service identified", "service_accuracy_pct", "%"),
    ("Correct first action", "first_action_accuracy_pct", "%"),
    ("**Unsafe-action rate**", "unsafe_action_rate_pct", "%"),
    ("Grounded steps", "grounded_step_rate_pct", "%"),
    ("Escalation rate (all)", "escalation_rate_pct", "%"),
    ("Malicious incidents escalated", "malicious_escalated_pct", "%"),
    ("Escalated when not needed", "unneeded_escalation_pct", "%"),
    ("Latency p50", "latency_p50_s", " s"),
    ("Latency p95", "latency_p95_s", " s"),
    ("Mean client-side pacing wait", "pacing_wait_mean_s", " s"),
    ("Cost per incident (mean)", "cost_per_incident_usd", " USD"),
    ("Tokens per incident (mean)", "tokens_per_incident", ""),
]


def fmt(value, unit: str) -> str:
    if value is None:
        return "n/a"
    if unit == " USD":
        return f"${value:.4f}"
    return f"{value:,}{unit}" if isinstance(value, int) else f"{value}{unit}"


def main(run_dir: Path) -> None:
    cases = {c["id"]: c for c in load_cases()}
    records = [json.loads(line) for line in (run_dir / "results.jsonl").read_text().splitlines() if line]
    meta = json.loads((run_dir / "meta.json").read_text())
    configs = [c for c in ("single", "routed") if any(r["config"] == c for r in records)]
    summary = {c: summarize([r for r in records if r["config"] == c], cases) for c in configs}
    (run_dir / "summary.json").write_text(json.dumps({"meta": meta, "summary": summary}, indent=2))

    lines = ["| Metric | " + " | ".join(configs) + " |", "|---|" + "---|" * len(configs)]
    lines += ["| " + label + " | " + " | ".join(fmt(summary[c][key], unit) for c in configs) + " |"
              for label, key, unit in ROWS]
    lines += ["", "| Category | " + " | ".join(f"{c}: severity / service / first action / unsafe / escalated"
                                              for c in configs) + " |", "|---|" + "---|" * len(configs)]
    cats = sorted({k for c in configs for k in summary[c]["by_category"]})
    for cat in cats:
        cells = []
        for c in configs:
            b = summary[c]["by_category"].get(cat)
            cells.append("n/a" if not b else
                         f"{b['severity_accuracy_pct']}% / {b['service_accuracy_pct']}% / "
                         f"{b['first_action_accuracy_pct']}% / {b['unsafe_action_rate_pct']}% / "
                         f"{b['escalation_rate_pct']}% (n={b['incidents']})")
        lines.append(f"| {cat} | " + " | ".join(cells) + " |")
    for c in configs:
        if summary[c]["unsafe_actions"]:
            lines += ["", f"Unsafe actions ({c}):"]
            lines += [f"- {cid}: {reasons}" for cid, reasons in summary[c]["unsafe_actions"].items()]
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
