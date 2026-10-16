"""Deterministic grounding rules for proposed actions, used by the QA gate and when a human edits one.

An action passes only if everything concrete in it can be found in what Relay actually retrieved:
  - every citation names a retrieved runbook section, in the right file,
  - every CLI command (argocd, patronictl, kubectl, ...) appears in runbook text,
  - every feature flag name, release version and replica count appears in the evidence,
  - a change action targets exactly one service that is in scope for this incident,
  - a rollback targets a service with a deploy in the evidence,
  - nothing asks for a mass action ("restart all services"),
  - a read-only action (page_team, investigate_further) does not hide a production change,
  - a public status update names no internal systems.
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


# Wording that means an action changes production, which a read-only kind must not hide.
_MUTATION = re.compile(
    r"\b(roll(?:ing)?[ -]?back|revert|redeploy|deploy (?:the|a) fix|scale (?:up|out|down|to)|"
    r"restart|reboot|fail ?over|terminate|kill|"
    r"cancel(?:l?ing)? the|drop|delete|truncate|disable|enable|toggle|re-?publish|rotate|set the flag|"
    r"raise max_connections)\b",
    re.I,
)
_READ_ONLY_KINDS = {"page_team", "investigate_further"}
# A mention is not a command: "watch errors after the rollback", "confirm the failover worked".
_MENTION = re.compile(r"\b(after|before|following|once|until|since|whether|if|the|a|any|of|post)\s*-?\s*$", re.I)


def change_verb(text: str) -> str | None:
    """The first mutation verb used as an instruction (not merely mentioned), if any."""
    for m in _MUTATION.finditer(text):
        if not _MENTION.search(text[max(0, m.start() - 15) : m.start()]):
            return m.group(0)
    return None

# Internal names a public status update must not mention (COMM-1). "checkout" is not here: customers
# see a checkout too, and the runbook's own template talks about purchases.
_INTERNAL_NAMES = re.compile(
    r"\b(payments-api|auth-service|orders-db(?:-\d)?|patroni|argo(?: cd)?|pagerduty|jwks)\b", re.I
)


def performed_change(action: ActionDraft) -> str | None:
    """For a read-only kind (page_team, investigate_further), a production change hidden in its text."""
    return change_verb(f"{action.title}. {action.details}") if action.kind in _READ_ONLY_KINDS else None


def internal_names(action: ActionDraft) -> list[str]:
    """Internal system names in a status update's public text (policy COMM-1 forbids them)."""
    if action.kind != "status_update":
        return []
    return sorted({m.lower() for m in _INTERNAL_NAMES.findall(action.status_update_draft or "")})


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

    if verb := performed_change(action):
        problems.append(f"{label} is {action.kind} but would change production ({verb!r})")
    if leaked := internal_names(action):
        problems.append(f"{label} status update names internal systems {leaked}")

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
