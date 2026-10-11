"""Route a member turn, then decide whether the routing is trustworthy enough to act on.

The two halves are deliberately separate. :meth:`Router.classify` produces a
:class:`~payer_agents.domain.RoutingDecision` -- a pure record of what the model said.
:meth:`Router.gate` turns that record into an allow/defer verdict using thresholds
that a governance committee can change without touching model code.
"""

from __future__ import annotations

from payer_agents.config import Settings
from payer_agents.decisions.base import DecisionClient, DecisionError
from payer_agents.domain import HandoffReason, Intent, MemberRequest, RoutingDecision
from payer_agents.routing.taxonomy import (
    CLINICIAN_QUESTION,
    INJECTION_QUESTION,
    ROUTE_QUESTION,
    URGENCY_LEVELS,
    URGENCY_QUESTION,
    build_routing_request,
)


class GateOutcome:
    """Verdict from the safety gates, with the reason spelled out for the audit log."""

    def __init__(
        self,
        allow: bool,
        reason: HandoffReason | None = None,
        explanation: str = "",
        priority: str = "normal",
    ) -> None:
        self.allow = allow
        self.reason = reason
        self.explanation = explanation
        self.priority = priority

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"GateOutcome(allow={self.allow}, reason={self.reason}, {self.explanation!r})"


class Router:
    """Thin orchestration around one decision call."""

    def __init__(self, client: DecisionClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    def classify(self, request: MemberRequest) -> RoutingDecision:
        """Ask the decision model; never raise into the caller's happy path.

        A decision-model failure becomes a maximally uncertain decision rather than an
        exception, because the gate below already knows what to do with uncertainty:
        hand the turn to a person.
        """
        decision_request = build_routing_request(request)
        try:
            response = self.client.decide(decision_request)
        except DecisionError as exc:
            return self._unknown(str(exc))

        route = response.choice(ROUTE_QUESTION)
        urgency = response.score(URGENCY_QUESTION)
        return RoutingDecision(
            intent=Intent(route.choice),
            confidence=route.confidence,
            probabilities=dict(route.probabilities),
            urgency=urgency.score if urgency else 0.0,
            urgency_label=urgency.nearest_level if urgency else URGENCY_LEVELS[0],
            clinician_required_probability=response.noul(CLINICIAN_QUESTION),
            injection_probability=response.noul(INJECTION_QUESTION),
            provider=response.provider,
            model=response.model,
            latency_ms=response.latency_ms,
            degraded=response.degraded,
            degraded_reason=response.degraded_reason,
        )

    def gate(self, decision: RoutingDecision) -> GateOutcome:
        """Apply safety gates in cost order: cheapest and most severe first."""
        safety = self.settings.safety

        if decision.injection_probability >= safety.injection_threshold:
            return GateOutcome(
                False,
                HandoffReason.SUSPECTED_INJECTION,
                f"prompt-injection probability {decision.injection_probability:.2f} "
                f">= {safety.injection_threshold:.2f}",
                priority="security",
            )

        if decision.urgency >= safety.urgency_escalation_level:
            return GateOutcome(
                False,
                HandoffReason.URGENT_CLINICAL,
                f"urgency score {decision.urgency:.2f} ({decision.urgency_label}) "
                f">= {safety.urgency_escalation_level:.2f}",
                priority="urgent",
            )

        if decision.clinician_required_probability >= safety.clinician_required_threshold:
            return GateOutcome(
                False,
                HandoffReason.CLINICIAN_REQUIRED,
                f"clinician-required probability "
                f"{decision.clinician_required_probability:.2f} >= "
                f"{safety.clinician_required_threshold:.2f}",
                priority="clinical",
            )

        if decision.intent is Intent.CANNOT_TELL:
            return GateOutcome(
                False,
                HandoffReason.ABSTAINED,
                "decision model abstained rather than forcing a classification",
            )

        if decision.intent is Intent.CARE_ESCALATION:
            return GateOutcome(
                False,
                HandoffReason.URGENT_CLINICAL
                if decision.urgency >= 1.0
                else HandoffReason.CLINICIAN_REQUIRED,
                "routed to the human escalation queue by design",
                priority="high" if decision.urgency >= 1.0 else "normal",
            )

        threshold = self.settings.threshold_for(decision.intent.value)
        if decision.confidence < threshold:
            return GateOutcome(
                False,
                HandoffReason.LOW_CONFIDENCE,
                f"routing confidence {decision.confidence:.2f} < {threshold:.2f} "
                f"required for {decision.intent.value}",
            )

        return GateOutcome(
            True,
            explanation=f"confidence {decision.confidence:.2f} >= {threshold:.2f}",
        )

    def _unknown(self, reason: str) -> RoutingDecision:
        options = [intent.value for intent in Intent]
        uniform = 1.0 / len(options)
        return RoutingDecision(
            intent=Intent.CANNOT_TELL,
            confidence=uniform,
            probabilities=dict.fromkeys(options, uniform),
            provider="unavailable",
            model="unavailable",
            degraded=True,
            degraded_reason=reason,
        )
