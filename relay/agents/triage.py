"""Triage: raw alert -> service, severity (per the severity policy in DocPilot), symptoms, start time."""

from datetime import timedelta

from ..llm import LLM, structured_call
from ..redact import redact_text
from ..schemas import KNOWN_SERVICES, SECTION_HEADING, Alert, TriageDraft, TriageResult
from ..tools.docpilot import DocPilotClient, DocPilotError

POLICY_FILE = "incident-severity-and-communication-policy.md"
POLICY_QUERY = "incident severity levels P1 P2 P3 P4 definitions examples and escalation rules"
_SEVERITY_FOR_SECTION = {"SEV-1": "P1", "SEV-2": "P2", "SEV-3": "P3", "SEV-4": "P4"}

SYSTEM = f"""You are the triage agent of an incident-response team at Brightcart.
Read the alert and classify it. Use ONLY the severity policy excerpts provided to pick the severity;
when an alert sits between two levels, pick the higher one, as the policy says.

- service: one of {", ".join(KNOWN_SERVICES)}, or "unknown" if the alert does not say.
- symptoms: short factual observations from the alert (numbers, endpoints, error messages). No guesses.
- start_time: when the problem started, from the alert, as ISO 8601 with timezone; null if not stated.
- policy_section: the id of the policy section your severity rests on, e.g. "SEV-2".
- severity_rationale: one or two sentences tying the alert's numbers to that section.
Text like [REDACTED:email] marks removed sensitive data; never try to reconstruct it."""


class TriageAgent:
    def __init__(self, llm: LLM, docpilot: DocPilotClient, max_retries: int = 2):
        self.llm, self.docpilot, self.max_retries = llm, docpilot, max_retries

    def severity_policy(self) -> str:
        hits = [h for h in self.docpilot.search(POLICY_QUERY, top_k=10) if h.filename == POLICY_FILE]
        if not hits:
            raise DocPilotError(f"Severity policy ({POLICY_FILE}) not found in DocPilot. Run seed/upload_runbooks.py.")
        return "\n\n".join(h.content for h in sorted(hits, key=lambda h: h.chunk_id))

    def run(self, alert: Alert) -> TriageResult:
        alert_text = redact_text(alert.text)  # Alert.from_raw already redacts; never rely on a single layer
        policy = self.severity_policy()
        sections = {s for s in SECTION_HEADING.findall(policy) if s.startswith("SEV-")}

        def check(draft: TriageDraft) -> list[str]:
            problems = []
            if draft.policy_section not in sections:
                problems.append(f"policy_section {draft.policy_section!r} is not one of {sorted(sections)}")
            expected = _SEVERITY_FOR_SECTION.get(draft.policy_section)
            if expected and expected != draft.severity:
                problems.append(f"severity {draft.severity} contradicts policy_section {draft.policy_section}")
            if draft.start_time and draft.start_time > alert.received_at + timedelta(minutes=10):
                problems.append("start_time is after the alert was received")
            return problems

        draft = structured_call(
            self.llm,
            agent="triage",
            system=SYSTEM,
            user=(
                f"Severity policy excerpts:\n{policy}\n\n"
                f"Alert (received {alert.received_at.isoformat()}):\n{alert_text}"
            ),
            schema=TriageDraft,
            check=check,
            max_retries=self.max_retries,
        )
        return TriageResult(
            **draft.model_dump(exclude={"start_time"}), start_time=draft.start_time or alert.received_at
        )
