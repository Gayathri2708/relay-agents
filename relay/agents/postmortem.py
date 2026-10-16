"""Postmortem: draft a blameless postmortem from a resolved run, its decisions and simulations."""

from ..llm import LLM, structured_call
from ..schemas import PostmortemDraft
from ..tracing import Span
from ..untrusted import wrap

SYSTEM = """You write blameless incident postmortems for Brightcart's SRE team.
Use only the facts provided: the triage, findings, evidence, the actions humans approved or rejected,
what was simulated, and the resolution note. Do not invent times, numbers or people. Name teams, not
individuals. Where a fact is missing, say so (e.g. "customer impact not measured").
Note that approved actions were simulated by Relay's mock executor, not run by Relay; humans performed
any real changes. Action items must be concrete and owned by a team."""


class PostmortemAgent:
    def __init__(self, llm: LLM, max_retries: int = 2):
        self.llm, self.max_retries = llm, max_retries

    def run(self, incident_json: str, span: Span | None = None) -> PostmortemDraft:
        return structured_call(
            self.llm,
            agent="postmortem",
            system=SYSTEM,
            user=f"Resolved incident record:\n{wrap('incident', incident_json)}",
            schema=PostmortemDraft,
            max_retries=self.max_retries,
            span=span,
        )
