"""Plan: propose next actions, each citing a runbook section that was actually retrieved.

Nothing here executes anything. Actions are proposals; risky ones are flagged for approval by the
ProposedAction contract itself, regardless of what the model returns.
"""

import re

from ..llm import LLM, AgentOutputError, structured_call
from ..schemas import (
    Investigation,
    PlanDraft,
    ProposedAction,
    ResponsePlan,
    TriageResult,
)

SYSTEM = """You are the planning agent of an incident-response team at Brightcart.
Propose the next actions for the on-call engineer. A human reviews and approves every action;
you never execute anything. Base every action on the runbooks provided.

Rules:
- Each action cites at least one runbook section as {"runbook": <filename>, "section": <id>}, using ONLY
  the citable sections listed. The cited section must actually support the action.
- Action kinds: rollback, scale_up, feature_flag, failover, restart, page_team, status_update,
  investigate_further. Order actions by priority.
- Always include a page_team action naming the owning team for P1 and P2 incidents.
- For P1 and P2, include a status_update action whose status_update_draft follows the status page
  template. It must not name internal services, hosts or people.
- Do not propose actions a runbook forbids (e.g. failing over for slow queries, rolling back during a
  processor outage)."""

# Internal names a public status update must not mention (COMM-1). "checkout" is not here: customers
# see a checkout too, and the runbook's own template talks about purchases.
_INTERNAL_NAMES = re.compile(
    r"\b(payments-api|auth-service|orders-db(?:-\d)?|patroni|argo(?: cd)?|pagerduty|jwks)\b", re.I
)


class PlanAgent:
    def __init__(self, llm: LLM, max_retries: int = 2):
        self.llm, self.max_retries = llm, max_retries

    def run(self, triage: TriageResult, investigation: Investigation) -> ResponsePlan:
        citable = investigation.runbook_sections()
        if not citable:
            raise AgentOutputError("plan", ["no runbook sections were retrieved, so no action can be cited"], 0)
        urgent = triage.severity in ("P1", "P2")

        def check(draft: PlanDraft) -> list[str]:
            problems = []
            for i, action in enumerate(draft.actions):
                for c in action.citations:
                    if c.section not in citable:
                        problems.append(f"actions[{i}] cites {c.section}, which is not a citable section")
                    elif citable[c.section] != c.runbook:
                        problems.append(
                            f"actions[{i}] cites {c.section} in {c.runbook}, but it is in {citable[c.section]}"
                        )
                leaked = sorted({m.lower() for m in _INTERNAL_NAMES.findall(action.status_update_draft or "")})
                if action.kind == "status_update" and leaked:
                    problems.append(f"actions[{i}] status update names internal systems {leaked}")
            kinds = {a.kind for a in draft.actions}
            if urgent and "page_team" not in kinds:
                problems.append(f"{triage.severity} incidents need a page_team action")
            if urgent and "status_update" not in kinds:
                problems.append(f"{triage.severity} incidents need a status_update action")
            return problems

        sections = "\n".join(f"- {sid} in {fname}" for sid, fname in sorted(citable.items()))
        runbooks = "\n\n".join(
            f"[{e.source}]\n{e.content}" for e in investigation.evidence if e.kind == "runbook"
        )
        findings = investigation.model_dump_json(include={"summary", "likely_cause", "findings"}, indent=2)
        draft = structured_call(
            self.llm,
            agent="plan",
            system=SYSTEM,
            user=(
                f"Triage:\n{triage.model_dump_json(indent=2)}\n\nInvestigation:\n{findings}\n\n"
                f"Citable runbook sections:\n{sections}\n\nRunbook excerpts:\n{runbooks}"
            ),
            schema=PlanDraft,
            check=check,
            max_retries=self.max_retries,
        )
        actions = [ProposedAction(id=f"A{i}", **a.model_dump()) for i, a in enumerate(draft.actions, 1)]
        return ResponsePlan(summary=draft.summary, actions=actions)
