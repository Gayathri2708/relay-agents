"""Write the measured eval results into README.md between the eval-results markers.

    python -m evals.update_readme evals/results/<run>

Only numbers from <run>/summary.json (written by evals.report from results.jsonl) are used, and the
block always says how many incidents each configuration has completed.
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from .report import ROWS, fmt
from .run import load_cases

README = Path(__file__).parent.parent / "README.md"
START, END = "<!-- eval-results:start -->", "<!-- eval-results:end -->"
ORDER = ("routed", "single")


def render(run_dir: Path) -> str:
    data = json.loads((run_dir / "summary.json").read_text())
    summary, meta = data["summary"], data["meta"]
    total = len(load_cases())
    records = [json.loads(line) for line in (run_dir / "results.jsonl").read_text().splitlines() if line]
    last = max((r["recorded_at"] for r in records if r.get("recorded_at")), default="")
    configs = [c for c in ORDER if c in summary]
    done = {c: summary[c]["incidents"] for c in configs}
    complete = all(done.get(c) == total for c in ORDER)

    status = "Complete" if complete else "In progress"
    progress = "; ".join(
        f"`{c}`: **{done.get(c, 0)} of {total}** incidents scored" for c in ORDER
    )
    lines = [
        START,
        f"**{status}, on Groq's free tier.** {progress}. "
        f"Last result recorded {last.replace('T', ' ')} UTC; this section was regenerated "
        f"{datetime.now(UTC):%Y-%m-%d %H:%M} UTC by `evals/update_readme.py` from "
        f"[`{run_dir.as_posix()}`]({run_dir.as_posix()}/).",
        "",
    ]
    if not complete:
        lines += [
            "> **Partial results.** The free tier allows roughly 200k `gpt-oss-120b` tokens a day, about "
            "8–10 incidents, so the run continues daily (`evals/run_daily.sh`): all 49 incidents on `routed` "
            "first, then `single`. Percentages over a small sample move a lot as incidents are added; read "
            "them with the incident counts. Incidents run in an order that rotates through the four "
            "categories, so a partial run is a mixed sample.",
            "",
        ]
    lines += [
        "| Metric | " + " | ".join(f"{c} (n={done[c]})" for c in configs) + " |",
        "|---|" + "---|" * len(configs),
    ]
    lines += ["| " + label + " | " + " | ".join(fmt(summary[c][key], unit) for c in configs) + " |"
              for label, key, unit in ROWS if key != "incidents"]
    lines += ["", "By category (severity / service / first action / unsafe / escalated):", ""]
    lines += ["| Category | " + " | ".join(configs) + " |", "|---|" + "---|" * len(configs)]
    for cat in ("clear", "ambiguous", "multi_service", "malicious"):
        cells = []
        for c in configs:
            b = summary[c]["by_category"].get(cat)
            cells.append("not yet run" if not b else
                         f"{b['severity_accuracy_pct']}% / {b['service_accuracy_pct']}% / "
                         f"{b['first_action_accuracy_pct']}% / {b['unsafe_action_rate_pct']}% / "
                         f"{b['escalation_rate_pct']}% (n={b['incidents']})")
        lines.append(f"| {cat} | " + " | ".join(cells) + " |")
    for c in configs:
        unsafe = summary[c]["unsafe_actions"]
        lines += ["", f"Unsafe approvable actions (`{c}`): " + ("none." if not unsafe else "")]
        for cid, reasons in unsafe.items():
            lines.append(f"- `{cid}`: " + "; ".join(r for rs in reasons.values() for r in rs))
    lines += [
        "",
        "Notes on reading these numbers:",
        "- **Latency is dominated by free-tier pacing.** The client waits for room under 8,000 tokens per "
        "minute; the mean pacing wait is listed separately. Server-side 429 back-off is also inside latency.",
        "- **The configurations run on different days** (routed first, then single), so DocPilot and Groq "
        "conditions may differ between them. Within a configuration, every incident runs on the same code: "
        f"commit(s) {', '.join(f'`{s}`' for s in meta.get('git_shas', []))}.",
        "- **Cost** is Relay's own LLM cost at Groq list prices. On the free tier nothing is actually billed. "
        "DocPilot's `/ask` call uses its own LLM call and isn't included.",
        "- **The dataset is synthetic**, written from the same runbooks the agents read. Treat the results "
        "as evidence that the pipeline and guardrails behave as designed, not as production accuracy.",
        END,
    ]
    return "\n".join(lines)


def main(run_dir: Path) -> None:
    text = README.read_text()
    if START not in text or END not in text:
        raise SystemExit(f"README.md has no {START} … {END} block")
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    README.write_text(head + render(run_dir) + tail)
    print("README eval section updated")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
