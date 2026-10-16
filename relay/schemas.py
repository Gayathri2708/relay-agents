"""Typed contracts for every handoff in the pipeline.

    Alert --triage--> TriageResult --investigate--> Investigation --plan--> ResponsePlan

The *Draft models are what an LLM is asked to return. Agents validate a draft, check it against
the evidence they hold, then build the full contract model that is handed to the next stage.
"""

import re
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator

from .redact import redact

Severity = Literal["P1", "P2", "P3", "P4"]
Service = Literal["payments-api", "checkout", "auth-service", "orders-db", "unknown"]
KNOWN_SERVICES: tuple[str, ...] = ("payments-api", "checkout", "auth-service", "orders-db")

# What each service calls; investigation looks at these too, since symptoms often come from a dependency.
DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "checkout": ("payments-api", "auth-service", "orders-db"),
    "payments-api": ("orders-db",),
    "auth-service": (),
    "orders-db": (),
}

SECTION_ID = re.compile(r"\b([A-Z]{2,5}-\d{1,2})\b")
SECTION_HEADING = re.compile(r"#{1,6}\s*\[([A-Z]{2,5}-\d{1,2})\]")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(_utc)]  # naive times are taken as UTC


# --- Alert -------------------------------------------------------------------------------------


class Alert(BaseModel):
    """An incoming alert. The text is redacted on construction, so raw secrets never travel further."""

    text: str = Field(min_length=1, max_length=20_000)
    received_at: UtcDatetime = Field(default_factory=lambda: datetime.now(UTC))
    redactions: dict[str, int] = Field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw_text: str, received_at: datetime | None = None) -> "Alert":
        result = redact(raw_text)
        return cls(text=result.text, received_at=received_at or datetime.now(UTC), redactions=result.counts)


# --- Triage ------------------------------------------------------------------------------------


class TriageDraft(BaseModel):
    service: Service
    severity: Severity
    symptoms: list[str] = Field(min_length=1, max_length=8)
    start_time: UtcDatetime | None = Field(
        description="When the problem began, from the alert; null if not stated"
    )
    severity_rationale: str = Field(min_length=10)
    policy_section: str = Field(description="Severity policy section the decision rests on, e.g. SEV-2")

    @field_validator("symptoms")
    @classmethod
    def _non_blank(cls, v: list[str]) -> list[str]:
        if any(not s.strip() for s in v):
            raise ValueError("symptoms must not be blank")
        return [s.strip() for s in v]


class TriageResult(BaseModel):
    service: Service
    severity: Severity
    symptoms: list[str] = Field(min_length=1)
    start_time: UtcDatetime
    severity_rationale: str
    policy_section: str


# --- Investigation -----------------------------------------------------------------------------

EvidenceKind = Literal[
    "runbook", "runbook_answer", "deploy", "metrics", "logs", "past_incident", "tool_error"
]


class Evidence(BaseModel):
    id: str = Field(pattern=r"^E\d+$")
    kind: EvidenceKind
    source: str  # runbook filename, deploy version, incident id, ...
    content: str

    @property
    def defined_sections(self) -> set[str]:
        """Runbook section ids whose heading appears in this evidence (citable)."""
        return set(SECTION_HEADING.findall(self.content)) if self.kind == "runbook" else set()


class Finding(BaseModel):
    statement: str = Field(min_length=5)
    confidence: Literal["low", "medium", "high"]
    evidence_ids: list[str] = Field(min_length=1)


class InvestigationDraft(BaseModel):
    summary: str = Field(min_length=10)
    likely_cause: str = Field(min_length=5)
    findings: list[Finding] = Field(min_length=1, max_length=10)


class Investigation(InvestigationDraft):
    evidence: list[Evidence]

    def runbook_sections(self) -> dict[str, str]:
        """Citable section id -> runbook filename, from the runbook evidence that was retrieved."""
        sections: dict[str, str] = {}
        for ev in self.evidence:
            for sid in ev.defined_sections:
                sections.setdefault(sid, ev.source)
        return sections


# --- Plan --------------------------------------------------------------------------------------

ActionKind = Literal[
    "rollback",
    "scale_up",
    "feature_flag",
    "failover",
    "restart",
    "config_change",
    "terminate_query",
    "page_team",
    "status_update",
    "investigate_further",
]
# Changes to production. These are always high risk and always need explicit human approval.
RISKY_KINDS: frozenset[str] = frozenset(
    {"rollback", "scale_up", "feature_flag", "failover", "restart", "config_change", "terminate_query"}
)
# Publishing a status update needs incident commander sign-off (policy COMM-1), though it is not risky.
APPROVAL_KINDS: frozenset[str] = RISKY_KINDS | {"status_update"}


class RunbookCitation(BaseModel):
    runbook: str = Field(description="Runbook filename, e.g. payments-api.md")
    section: str = Field(pattern=r"^[A-Z]{2,5}-\d{1,2}$", description="Section id, e.g. PAY-2")


class ActionDraft(BaseModel):
    kind: ActionKind
    title: str = Field(min_length=5, max_length=200)
    details: str = Field(min_length=10)
    target: str | None = Field(default=None, description="Service, team or flag the action applies to")
    citations: list[RunbookCitation] = Field(min_length=1)
    status_update_draft: str | None = Field(default=None, description="Required when kind is status_update")

    @model_validator(mode="after")
    def _status_update_has_text(self) -> "ActionDraft":
        if self.kind == "status_update" and not (self.status_update_draft or "").strip():
            raise ValueError("status_update actions must include status_update_draft")
        return self


class PlanDraft(BaseModel):
    summary: str = Field(min_length=10)
    actions: list[ActionDraft] = Field(min_length=1, max_length=8)


class ProposedAction(ActionDraft):
    id: str = Field(pattern=r"^A\d+$")
    risk: Literal["low", "high"] = "low"
    requires_approval: bool = False

    @model_validator(mode="after")
    def _enforce_safety(self) -> "ProposedAction":
        # Enforced here, not left to the model: whatever an LLM says, risky kinds need approval.
        if self.kind in RISKY_KINDS:
            self.risk = "high"
        if self.kind in APPROVAL_KINDS:
            self.requires_approval = True
        return self


class ResponsePlan(BaseModel):
    summary: str
    actions: list[ProposedAction] = Field(min_length=1)
    # Relay never executes anything. This is part of the contract so every consumer sees it.
    executed: Literal[False] = False
