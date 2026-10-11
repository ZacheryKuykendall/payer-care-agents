"""Human handoff.

Not a fallback. Escalation is a product surface with its own quality bar, and it is
the one every other path depends on, so it gets a real agent rather than a string
constant.

Two outputs, deliberately different:

* **Member-facing text** -- short, free of plan jargon, and for an emergent case it
  leads with the safety instruction before anything about the plan.
* **Handoff packet** -- the routing distribution, the gate that fired, what was
  already retrieved or looked up, and the audit correlation id, so the person
  picking it up starts where the agent stopped instead of from zero.
"""

from __future__ import annotations

from payer_agents.agents.base import AgentContext, SpecialistAgent
from payer_agents.domain import AgentAnswer, Handoff, HandoffReason

EMERGENCY_TEXT = (
    "If this is a medical emergency, call 911 or go to the nearest emergency room "
    "now. Emergency care never needs prior approval from your plan.\n\n"
    "I am also connecting you with a nurse line representative."
)

QUEUES: dict[HandoffReason, tuple[str, str]] = {
    HandoffReason.URGENT_CLINICAL: ("nurse-triage", "urgent"),
    HandoffReason.CLINICIAN_REQUIRED: ("nurse-triage", "high"),
    HandoffReason.SUSPECTED_INJECTION: ("security-review", "high"),
    HandoffReason.LOW_CONFIDENCE: ("member-services", "normal"),
    HandoffReason.ABSTAINED: ("member-services", "normal"),
    HandoffReason.UNGROUNDED_ANSWER: ("member-services", "normal"),
    HandoffReason.TOOL_FAILURE: ("member-services", "high"),
    HandoffReason.POLICY_BLOCKED: ("compliance-review", "high"),
}

MEMBER_TEXT: dict[HandoffReason, str] = {
    HandoffReason.CLINICIAN_REQUIRED: (
        "That is a question for a clinician rather than for me. I am connecting you "
        "with a nurse who can talk it through with you."
    ),
    HandoffReason.SUSPECTED_INJECTION: (
        "I am not able to help with that request. I have passed this conversation to "
        "a representative who can assist you."
    ),
    HandoffReason.LOW_CONFIDENCE: (
        "I want to make sure you get this right rather than guess, so I am connecting "
        "you with a member services representative."
    ),
    HandoffReason.ABSTAINED: (
        "I want to be sure I understand what you need before answering. A member "
        "services representative is joining to help."
    ),
    HandoffReason.UNGROUNDED_ANSWER: (
        "I could not confirm an answer against your plan documents, so I am handing "
        "this to a member services representative rather than giving you something I "
        "cannot back up."
    ),
    HandoffReason.TOOL_FAILURE: (
        "I could not reach the plan system I need for this. A representative is "
        "joining who can look it up directly."
    ),
    HandoffReason.POLICY_BLOCKED: (
        "I am not able to answer that here. A representative will follow up with you."
    ),
}


class EscalationAgent(SpecialistAgent):
    name = "escalation"
    uses_indexes = []
    uses_tools = []

    def handle(self, context: AgentContext) -> AgentAnswer:
        return AgentAnswer(
            agent=self.name,
            text=MEMBER_TEXT[HandoffReason.LOW_CONFIDENCE],
            grounded=True,
            notes=["escalation agent invoked without a specific reason"],
        )

    def build(
        self,
        context: AgentContext,
        reason: HandoffReason,
        explanation: str,
        *,
        partial: AgentAnswer | None = None,
        priority: str | None = None,
    ) -> Handoff:
        """Assemble the member-facing message and the packet for the human queue."""
        queue, default_priority = QUEUES.get(reason, ("member-services", "normal"))
        routing = context.routing
        is_emergency = (
            reason is HandoffReason.URGENT_CLINICAL
            and routing.urgency >= context.settings.safety.urgency_escalation_level
        )
        summary = (
            EMERGENCY_TEXT
            if is_emergency
            else MEMBER_TEXT.get(reason, MEMBER_TEXT[HandoffReason.LOW_CONFIDENCE])
        )

        packet = {
            "gate": reason.value,
            "gate_explanation": explanation,
            "channel": context.request.channel.value,
            "member_identified": context.member_id is not None,
            "redacted_member_message": context.redacted_text,
            "routing": {
                "intent": routing.intent.value,
                "confidence": round(routing.confidence, 4),
                "top_margin": round(routing.top_margin, 4),
                "probabilities": {k: round(v, 4) for k, v in routing.probabilities.items()},
                "urgency": round(routing.urgency, 3),
                "urgency_label": routing.urgency_label,
                "clinician_required_probability": round(routing.clinician_required_probability, 4),
                "injection_probability": round(routing.injection_probability, 4),
                "model": routing.model,
                "provider": routing.provider,
                "degraded": routing.degraded,
            },
            "work_already_done": {
                "agent": partial.agent if partial else None,
                "citations": [c.source_id for c in partial.citations] if partial else [],
                "tool_calls": [
                    {"tool": t.tool, "ok": t.ok, "correlation_id": t.correlation_id}
                    for t in (partial.tool_calls if partial else [])
                ],
            },
        }

        return Handoff(
            reason=reason,
            queue=queue,
            summary=summary,
            priority=priority or default_priority,
            context=packet,
        )
