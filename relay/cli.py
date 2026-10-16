"""Run the pipeline on an alert file and print the result.

    python -m relay.cli seed/alerts/payments-5xx-after-deploy.txt
    python -m relay.cli alert.txt --json --save
"""

import argparse
import sys
from pathlib import Path

from .config import get_settings
from .db import apply_schema, make_pool
from .graph import IncidentState, Pipeline
from .schemas import Alert
from .store import PostgresRunStore
from .tools.ops import PostgresOps


def render(state: IncidentState) -> str:
    out = [f"status: {state.status}"]
    if state.alert.redactions:
        out.append(f"redacted from alert: {state.alert.redactions}")
    if t := state.triage:
        started = f"{t.start_time:%Y-%m-%d %H:%M}Z"
        out += ["", f"TRIAGE  {t.service}  {t.severity}  (per {t.policy_section}, started {started})",
                f"  {t.severity_rationale}", *[f"  - {s}" for s in t.symptoms]]
    if i := state.investigation:
        out += ["", f"INVESTIGATION  ({len(i.evidence)} pieces of evidence)", f"  {i.summary}",
                f"  likely cause: {i.likely_cause}"]
        out += [f"  - [{f.confidence}] {f.statement} ({', '.join(f.evidence_ids)})" for f in i.findings]
    if p := state.plan:
        out += ["", "PROPOSED ACTIONS  (nothing has been executed)", f"  {p.summary}"]
        for a in p.actions:
            flag = "  [needs approval]" if a.requires_approval else ""
            if a.risk == "high":
                flag = "  [NEEDS APPROVAL, high risk]"
            cites = ", ".join(f"{c.section} ({c.runbook})" for c in a.citations)
            out += [f"  {a.id}. {a.kind}: {a.title}{flag}", f"      {a.details}", f"      cites: {cites}"]
            if a.status_update_draft:
                out.append(f"      draft: \"{a.status_update_draft}\"")
    if state.error:
        out += ["", f"FAILED at {state.failed_stage}: {state.error}"]
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Relay on an alert file.")
    parser.add_argument("alert_file", type=Path)
    parser.add_argument("--json", action="store_true", help="print the full state as JSON")
    parser.add_argument("--save", action="store_true", help="store the run so it can be approved via the API")
    args = parser.parse_args()

    settings = get_settings()
    pool = make_pool(settings.database_url)
    apply_schema(pool)
    try:
        state = Pipeline.from_settings(settings, PostgresOps(pool)).run(Alert.from_raw(args.alert_file.read_text()))
        print(state.model_dump_json(indent=2) if args.json else render(state))
        if args.save:
            print(f"\nsaved run {PostgresRunStore(pool).save(state).id}")
    finally:
        pool.close()
    return 0 if state.status == "awaiting_approval" else 1


if __name__ == "__main__":
    sys.exit(main())
