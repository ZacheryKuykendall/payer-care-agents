"""Domain model shared by every layer.

These types are the contract between the router, the specialist agents, the tool
gateway and the audit log. Keeping them in one module makes the data that crosses the
PHI boundary easy to review in one sitting -- which is the question a security reviewer
actually asks.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Intent(str, Enum):
    """The fixed option set handed to the decision model.

    ``CANNOT_TELL`` is the abstention option. Microsoft's guidance for
    ``Microsoft-Decision-1`` is to offer one explicitly rather than force a choice; a
    forced choice on an ambiguous member message is how a benefits bot ends up
    answering a chest-pain question.
    """

    BENEFITS_COVERAGE = "benefits_coverage"
    CLINICAL_POLICY = "clinical_policy"
    PRIOR_AUTH_STATUS = "prior_auth_status"
    CARE_ESCALATION = "care_escalation"
    CANNOT_TELL = "cannot_tell"


class Channel(str, Enum):
    MEMBER_PORTAL = "member_portal"
    CALL_CENTER = "call_center"
    PROVIDER_PORTAL = "provider_portal"


class MemberRequest(BaseModel):
    """One inbound turn. ``member_id`` is a synthetic identifier only."""

    text: str
    member_id: str | None = None
    channel: Channel = Channel.MEMBER_PORTAL
    conversation_id: str = Field(default_factory=lambda: new_id("conv"))
    request_id: str = Field(default_factory=lambda: new_id("req"))
    received_at: datetime = Field(default_factory=utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RoutingDecision(BaseModel):
    """Everything the cheap decision step produced, kept verbatim for audit.

    ``probabilities`` is the full distribution, not just the winner, because the
    distribution is what you need later to measure calibration.
    """

    intent: Intent
    confidence: float
    probabilities: dict[str, float]
    urgency: float = 0.0
    urgency_label: str = "routine"
    clinician_required_probability: float = 0.0
    injection_probability: float = 0.0
    provider: str = "unknown"
    model: str = "unknown"
    latency_ms: float = 0.0
    degraded: bool = False
    degraded_reason: str | None = None

    @property
    def top_margin(self) -> float:
        """Gap between the best and second-best option.

        A high top probability with a near-tie behind it is a different kind of
        uncertainty than a high top probability with nothing close.
        """
        ranked = sorted(self.probabilities.values(), reverse=True)
        if len(ranked) < 2:
            return ranked[0] if ranked else 0.0
        return ranked[0] - ranked[1]


class HandoffReason(str, Enum):
    """Why a turn stopped being fully automated. One enum, one audit dimension."""

    LOW_CONFIDENCE = "low_confidence"
    ABSTAINED = "abstained"
    URGENT_CLINICAL = "urgent_clinical"
    CLINICIAN_REQUIRED = "clinician_required"
    SUSPECTED_INJECTION = "suspected_injection"
    UNGROUNDED_ANSWER = "ungrounded_answer"
    TOOL_FAILURE = "tool_failure"
    POLICY_BLOCKED = "policy_blocked"


class Citation(BaseModel):
    """A pointer a human reviewer can actually open."""

    source_id: str
    title: str
    snippet: str
    score: float = 0.0
    section: str | None = None
    url: str | None = None


class ToolCall(BaseModel):
    """One MCP tool invocation, recorded whether it succeeded or not.

    ``payload`` carries the structured result for the calling agent and is excluded
    from serialisation, because it is the one field here that can contain member
    data. Everything the audit log and the API need -- what was called, by whom,
    whether the gateway allowed it, how long it took -- survives ``model_dump``.
    """

    tool: str
    arguments: dict[str, Any]
    ok: bool
    duration_ms: float = 0.0
    result_summary: str = ""
    payload: Any = Field(default=None, exclude=True)
    error: str | None = None
    gateway_decision: str = "allow"
    correlation_id: str = Field(default_factory=lambda: new_id("tool"))


class AgentAnswer(BaseModel):
    """What a specialist produced before the orchestrator decided to show it.

    ``citations`` and ``facts`` are two kinds of provenance and the grounding judge
    needs both. A copay comes from a document and is citable. A determination comes
    from the system of record through an audited tool call and is attested by that
    call. Grading an answer against documents alone would mark every correct
    prior-authorization answer as unsupported.
    """

    agent: str
    text: str
    citations: list[Citation] = Field(default_factory=list)
    facts: list[str] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    grounded: bool = True
    grounding_score: float = 3.0
    notes: list[str] = Field(default_factory=list)


class Handoff(BaseModel):
    """The packet a human receives. Deliberately richer than the member-facing text."""

    reason: HandoffReason
    queue: str
    summary: str
    priority: str = "normal"
    context: dict[str, Any] = Field(default_factory=dict)


class TurnResult(BaseModel):
    """The orchestrator's complete output for one member turn."""

    request_id: str
    conversation_id: str
    redacted_text: str
    routing: RoutingDecision
    answer: AgentAnswer | None = None
    handoff: Handoff | None = None
    audit_events: list[dict[str, Any]] = Field(default_factory=list)
    total_latency_ms: float = 0.0

    @property
    def automated(self) -> bool:
        return self.handoff is None

    @property
    def display_text(self) -> str:
        if self.answer is not None:
            return self.answer.text
        if self.handoff is not None:
            return self.handoff.summary
        return ""
