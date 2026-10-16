"""Investigate: gather evidence with tools, then have the LLM state findings that cite that evidence.

The tool calls are deterministic (driven by the triage result), not chosen by the model. That keeps
an investigation reproducible and auditable, and the model cannot skip a source of evidence.
"""

import logging
from datetime import datetime

from ..llm import LLM, structured_call
from ..redact import redact_text
from ..schemas import (
    DEPENDENCIES,
    SECTION_HEADING,
    SECTION_ID,
    Evidence,
    EvidenceKind,
    Investigation,
    InvestigationDraft,
    TriageResult,
)
from ..tools.docpilot import DocPilotClient, DocPilotError
from ..tools.ops import OpsData

log = logging.getLogger(__name__)

SYSTEM = """You are the investigation agent of an incident-response team at Brightcart.
You get a triaged incident and numbered evidence gathered from runbooks, deploy history, metrics,
logs and past incidents. Work out what is most likely happening.

- Every finding must cite the evidence ids (e.g. "E3") that support it. Cite only ids you were given.
- Prefer concrete observations: timings relative to deploys, metric changes, error messages.
- Say "low" confidence when evidence is indirect. Do not invent facts not in the evidence.
- likely_cause: the single most likely cause, or "unknown" with what is missing.
Text like [REDACTED:email] marks removed sensitive data; never try to reconstruct it."""

MAX_REFERENCE_LOOKUPS = 6


class _Evidence:
    def __init__(self) -> None:
        self.items: list[Evidence] = []
        self._chunks: set[int] = set()

    def add(self, kind: EvidenceKind, source: str, content: str) -> None:
        self.items.append(
            Evidence(id=f"E{len(self.items) + 1}", kind=kind, source=source, content=redact_text(content))
        )

    def add_runbook_chunk(self, chunk_id: int, filename: str, content: str) -> None:
        if chunk_id not in self._chunks:
            self._chunks.add(chunk_id)
            self.add("runbook", filename, content)


def _offset(ts: datetime, start: datetime) -> str:
    minutes = round((ts - start).total_seconds() / 60)
    return f"{ts:%Y-%m-%d %H:%M}Z ({minutes:+d} min vs incident start)"


class InvestigateAgent:
    def __init__(self, llm: LLM, docpilot: DocPilotClient, ops: OpsData, max_retries: int = 2):
        self.llm, self.docpilot, self.ops, self.max_retries = llm, docpilot, ops, max_retries

    # --- evidence gathering ---------------------------------------------------------------------

    def _runbooks(self, ev: _Evidence, triage: TriageResult) -> None:
        service, symptoms = triage.service, "; ".join(triage.symptoms)
        queries = [
            f"{service}: {symptoms}",
            f"{service} triage checklist",
            f"{service} who to page and escalation contacts",
            "status page update template for customers",
        ]
        for query in queries:
            for hit in self.docpilot.search(query, top_k=4):
                ev.add_runbook_chunk(hit.chunk_id, hit.filename, hit.content)

        # Runbooks cross-reference each other ("follow [DEP-2]"). Fetch referenced sections we lack,
        # so the plan can cite the procedure itself rather than a mention of it.
        defined = {sid for e in ev.items for sid in e.defined_sections}
        referenced = {sid for e in ev.items if e.kind == "runbook" for sid in SECTION_ID.findall(e.content)}
        for sid in sorted(referenced - defined)[:MAX_REFERENCE_LOOKUPS]:
            for hit in self.docpilot.search(f"[{sid}]", top_k=3, mode="hybrid"):
                if sid in SECTION_HEADING.findall(hit.content):
                    ev.add_runbook_chunk(hit.chunk_id, hit.filename, hit.content)

        try:
            answer = self.docpilot.ask(
                f"According to the runbooks, what should the on-call engineer check and do when "
                f"{service} shows: {symptoms}?"
            )
            if answer.answered:
                ev.add("runbook_answer", ", ".join(answer.sources) or "DocPilot", answer.answer)
        except DocPilotError as exc:  # /ask needs DocPilot's LLM; retrieval above is what matters
            log.warning("DocPilot /ask failed: %s", exc)
            ev.add("tool_error", "docpilot /ask", f"Runbook Q&A unavailable: {exc}")

    def _ops(self, ev: _Evidence, triage: TriageResult) -> None:
        start = triage.start_time
        services = [triage.service, *DEPENDENCIES.get(triage.service, ())] if triage.service != "unknown" else []

        try:
            deploys = self.ops.recent_deploys(start)
            for d in deploys:
                ev.add(
                    "deploy",
                    f"{d.service} {d.version}",
                    f"{d.service} deployed {d.version} (previous {d.previous_version}) by {d.author}, "
                    f"commit {d.commit_sha}, finished {_offset(d.finished_at, start)}. "
                    f"Change: {d.summary}. Includes DB migration: {'yes' if d.has_migration else 'no'}.",
                )
            if not deploys:
                ev.add("deploy", "deploys", "No deploys to any service in the 6 hours before the incident.")
        except Exception as exc:
            log.exception("deploy lookup failed")
            ev.add("tool_error", "deploys", f"Deploy lookup failed: {exc}")

        for service in services:
            try:
                if snaps := self.ops.metrics(service, start):
                    lines = [
                        f"{_offset(s.captured_at, start)}: "
                        + ", ".join(f"{k}={v:g}" for k, v in sorted(s.metrics.items()))
                        for s in snaps
                    ]
                    ev.add("metrics", f"{service} metrics", "\n".join(lines))
                if logs := self.ops.logs(service, start):
                    lines = [f"{_offset(line.captured_at, start)} {line.level} {line.message}" for line in logs]
                    ev.add("logs", f"{service} logs", "\n".join(lines))
            except Exception as exc:
                log.exception("metrics/log lookup failed for %s", service)
                ev.add("tool_error", f"{service} telemetry", f"Metrics/log lookup failed: {exc}")

        try:
            text = f"{triage.service} " + " ".join(triage.symptoms)
            for inc in self.ops.similar_incidents(triage.service, text):
                ev.add(
                    "past_incident",
                    inc.id,
                    f"{inc.id} ({inc.service}, {inc.severity}, {inc.started_at:%Y-%m-%d}): {inc.title}. "
                    f"{inc.summary} Root cause: {inc.root_cause} Resolution: {inc.resolution}",
                )
        except Exception as exc:
            log.exception("similar incident lookup failed")
            ev.add("tool_error", "past incidents", f"Similar incident lookup failed: {exc}")

    def gather(self, triage: TriageResult) -> list[Evidence]:
        ev = _Evidence()
        self._runbooks(ev, triage)  # DocPilot search failing is fatal: a plan needs runbooks to cite
        self._ops(ev, triage)  # ops lookups degrade to tool_error evidence instead
        return ev.items

    # --- reasoning ------------------------------------------------------------------------------

    def run(self, triage: TriageResult) -> Investigation:
        evidence = self.gather(triage)
        ids = {e.id for e in evidence}

        def check(draft: InvestigationDraft) -> list[str]:
            problems = []
            for i, finding in enumerate(draft.findings):
                if unknown := sorted(set(finding.evidence_ids) - ids):
                    problems.append(f"findings[{i}] cites unknown evidence ids {unknown}")
            return problems

        listing = "\n\n".join(f"[{e.id}] ({e.kind}) {e.source}\n{e.content}" for e in evidence)
        draft = structured_call(
            self.llm,
            agent="investigate",
            system=SYSTEM,
            user=f"Triaged incident:\n{triage.model_dump_json(indent=2)}\n\nEvidence:\n{listing}",
            schema=InvestigationDraft,
            check=check,
            max_retries=self.max_retries,
        )
        return Investigation(**draft.model_dump(), evidence=evidence)
