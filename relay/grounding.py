"""Deterministic grounding rules for proposed actions, used by the QA gate and when a human edits one.

An action passes only if everything concrete in it can be found in what Relay actually retrieved:
  - every citation names a retrieved runbook section, in the right file,
  - every CLI command (argocd, patronictl, kubectl, ...) appears in runbook text,
  - every feature flag name, release version and replica count appears in the evidence,
  - a change action targets exactly one service that is in scope for this incident,
  - a rollback targets a service with a deploy in the evidence,
  - nothing asks for a mass action ("restart all services").
These rules are also the structural defence against prompt injection: obeying an injected instruction
still cannot yield an action that passes them.
"""

import re

from .schemas import DEPENDENCIES, RISKY_KINDS, ActionDraft, Investigation, ResponsePlan, TriageResult
from .untrusted import scan

_CLI = re.compile(
    r"\b(argocd|patronictl|kubectl|helm|psql|pg_ctl|terraform|aws|gcloud|az|ssh|systemctl|docker|curl|redis-cli)"
    r"((?:\s+[a-z][a-z0-9_-]*){0,2})",
    re.I,
)
_FLAG = re.compile(r"\b[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){2,}\b")
_VERSION = re.compile(r"\bv\d+\.\d+\.\d+\b")
_REPLICAS = re.compile(r"\b(\d+)\s+replicas?\b", re.I)


def in_scope_services(triage: TriageResult) -> set[str]:
    if triage.service == "unknown":
        return set()
    return {triage.service, *DEPENDENCIES.get(triage.service, ())}


def check_action(action: ActionDraft, label: str, triage: TriageResult, investigation: Investigation) -> list[str]:
    problems: list[str] = []
    citable = investigation.runbook_sections()
    runbooks = investigation.runbook_text().lower()
    evidence = investigation.all_text().lower()
    text = f"{action.title}\n{action.details}"

    for c in action.citations:
        if c.section not in citable:
            problems.append(f"{label} cites {c.section}, which was not retrieved")
        elif citable[c.section] != c.runbook:
            problems.append(f"{label} cites {c.section} in {c.runbook}, but it is in {citable[c.section]}")

    for m in _CLI.finditer(text):
        command = " ".join(m.group(0).lower().split())
        tool = m.group(1).lower()
        if tool not in runbooks:
            problems.append(f"{label} uses '{command}', but no retrieved runbook mentions {tool}")
        elif command not in " ".join(runbooks.split()):
            problems.append(f"{label} uses '{command}', which does not appear in the runbooks")

    for flag in set(_FLAG.findall(text.lower())):
        if flag not in evidence:
            problems.append(f"{label} names flag or setting '{flag}', which is not in the runbooks or evidence")
    for version in set(_VERSION.findall(text)):
        if version.lower() not in evidence:
            problems.append(f"{label} names version {version}, which is not in the evidence")
    for count in set(_REPLICAS.findall(text)):
        if not re.search(rf"\b{count}\s+replicas?\b|\bto {count}\b", evidence):
            problems.append(f"{label} uses a replica count of {count}, which no runbook or evidence gives")

    if action.kind in RISKY_KINDS:
        scope = in_scope_services(triage)
        if not scope:
            problems.append(f"{label} changes production, but the affected service is unknown")
        elif action.target not in scope:
            problems.append(
                f"{label} must target exactly one in-scope service ({', '.join(sorted(scope))}), "
                f"not {action.target!r}"
            )
        if "mass_action" in scan(text):
            problems.append(f"{label} asks for a change across many services; actions must be scoped to one")
        if action.kind == "rollback" and action.target and not any(
            e.kind == "deploy" and e.source.startswith(f"{action.target} ") for e in investigation.evidence
        ):
            problems.append(f"{label} rolls back {action.target}, but no deploy of it is in the evidence")
    return problems


def check_plan(plan: ResponsePlan, triage: TriageResult, investigation: Investigation) -> dict[str, list[str]]:
    """Problems per action id (only actions with problems are present)."""
    found = {a.id: check_action(a, a.id, triage, investigation) for a in plan.actions}
    return {k: v for k, v in found.items() if v}
