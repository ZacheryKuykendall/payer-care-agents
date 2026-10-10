"""The turn pipeline and the composition root.

One function, :meth:`Orchestrator.handle`, owns the order of operations for every
member turn, and it is short on purpose: the whole control flow of the system should
fit on one screen during a design review.

::

    redact -> classify -> gate -> dispatch -> judge -> respond or hand off

Every step writes an audit record. No agent decides whether it is trusted; the
orchestrator does, using thresholds from configuration.

:func:`build` is the only place that knows whether we are in mock or Azure mode.
Nothing downstream imports an Azure SDK.
"""

from __future__ import annotations

import time
from typing import Any

from payer_agents.agents.base import AgentContext, SpecialistAgent
from payer_agents.agents.benefits import BenefitsAgent
from payer_agents.agents.clinical_policy import ClinicalPolicyAgent
from payer_agents.agents.escalation import EscalationAgent
from payer_agents.agents.generation import Composer, ExtractiveComposer, FoundryChatComposer
from payer_agents.agents.prior_auth import PriorAuthAgent
from payer_agents.config import RuntimeMode, Settings
from payer_agents.decisions.base import DecisionClient
from payer_agents.decisions.fallback import ResilientDecisionClient
from payer_agents.decisions.foundry import FoundryDecisionClient
from payer_agents.decisions.mock import HeuristicDecisionClient
from payer_agents.domain import (
    AgentAnswer,
    HandoffReason,
    Intent,
    MemberRequest,
    TurnResult,
)
from payer_agents.evals.judge import DecisionModelJudge, Judge, LexicalJudgeClient
from payer_agents.fhir.client import FhirClient, LocalFhirClient
from payer_agents.fhir.store import FhirStore
from payer_agents.gateway.apim_sim import ApiManagementSimulator, CallContext, GatewayPolicy
from payer_agents.phi.audit import AuditLog, JsonlAuditSink, MemoryAuditSink
from payer_agents.phi.egress import EgressPolicy, load_egress_policy
from payer_agents.phi.redaction import PhiCategory, Redactor
from payer_agents.retrieval.corpus import build_indexes
from payer_agents.retrieval.embeddings import HashingEmbedder
from payer_agents.retrieval.types import SearchIndex
from payer_agents.routing.router import Router
from payer_agents.routing.taxonomy import TAXONOMY_VERSION
from payer_agents.tools.fhir_tools import build_fhir_registry
from payer_agents.tools.mcp_client import InProcessMcpClient, McpClient
from payer_agents.tools.registry import ToolRegistry

AGENT_FOR_INTENT: dict[Intent, str] = {
    Intent.BENEFITS_COVERAGE: "benefits",
    Intent.CLINICAL_POLICY: "clinical_policy",
    Intent.PRIOR_AUTH_STATUS: "prior_auth",
}


class Orchestrator:
    """Runs one member turn end to end."""

    def __init__(
        self,
        settings: Settings,
        router: Router,
        agents: dict[str, SpecialistAgent],
        escalation: EscalationAgent,
        indexes: dict[str, SearchIndex],
        composer: Composer,
        judge: Judge,
        audit: AuditLog,
        mcp: McpClient | None = None,
        registry: ToolRegistry | None = None,
        egress_policy: EgressPolicy | None = None,
    ) -> None:
        self.settings = settings
        self.router = router
        self.agents = agents
        self.escalation = escalation
        self.indexes = indexes
        self.composer = composer
        self.judge = judge
        self.audit = audit
        self.mcp = mcp
        self.registry = registry
        self.egress_policy = egress_policy

    # -- pipeline --------------------------------------------------------------

    def handle(self, request: MemberRequest) -> TurnResult:
        started = time.perf_counter()
        first_event = len(self.audit.events)

        redactor = Redactor()
        redaction = redactor.redact(request.text)
        member_id = request.member_id or redactor.first_value(PhiCategory.MEMBER_ID)

        self.audit.record(
            "request",
            "received",
            request_id=request.request_id,
            conversation_id=request.conversation_id,
            channel=request.channel.value,
            characters=redaction.original_length,
            phi_redacted=redaction.counts,
            member_identified=member_id is not None,
        )

        routing = self.router.classify(request)
        self.audit.record(
            "routing",
            "classified",
            request_id=request.request_id,
            conversation_id=request.conversation_id,
            outcome="degraded" if routing.degraded else "ok",
            intent=routing.intent.value,
            confidence=round(routing.confidence, 4),
            top_margin=round(routing.top_margin, 4),
            probabilities={k: round(v, 4) for k, v in routing.probabilities.items()},
            urgency=round(routing.urgency, 3),
            urgency_label=routing.urgency_label,
            clinician_required_probability=round(routing.clinician_required_probability, 4),
            injection_probability=round(routing.injection_probability, 4),
            model=routing.model,
            provider=routing.provider,
            taxonomy_version=TAXONOMY_VERSION,
            degraded_reason=routing.degraded_reason,
            latency_ms=round(routing.latency_ms, 2),
        )

        context = AgentContext(
            request=request,
            redacted_text=redaction.text,
            routing=routing,
            indexes=self.indexes,
            mcp=self.mcp,
            composer=self.composer,
            settings=self.settings,
            member_id=member_id,
        )

        gate = self.router.gate(routing)
        self.audit.record(
            "gate",
            "evaluated",
            request_id=request.request_id,
            conversation_id=request.conversation_id,
            outcome="allow" if gate.allow else "defer",
            reason=gate.reason.value if gate.reason else None,
            explanation=gate.explanation,
            threshold=self.settings.threshold_for(routing.intent.value),
        )

        if not gate.allow:
            return self._handoff(
                context,
                gate.reason or HandoffReason.LOW_CONFIDENCE,
                gate.explanation,
                started,
                first_event,
                priority=gate.priority,
            )

        agent = self.agents.get(AGENT_FOR_INTENT.get(routing.intent, ""))
        if agent is None:
            return self._handoff(
                context,
                HandoffReason.POLICY_BLOCKED,
                f"no agent registered for intent {routing.intent.value}",
                started,
                first_event,
            )

        answer = agent.handle(context)
        self.audit.record(
            "agent",
            agent.name,
            request_id=request.request_id,
            conversation_id=request.conversation_id,
            outcome="ok" if answer.grounded else "no_answer",
            citations=[c.source_id for c in answer.citations],
            tool_calls=[
                {"tool": t.tool, "ok": t.ok, "gateway": t.gateway_decision}
                for t in answer.tool_calls
            ],
            notes=answer.notes,
        )

        if not answer.grounded:
            reason = (
                HandoffReason.TOOL_FAILURE
                if any(not t.ok for t in answer.tool_calls)
                else HandoffReason.UNGROUNDED_ANSWER
            )
            return self._handoff(
                context, reason, "agent returned no grounded answer", started, first_event,
                partial=answer
            )

        grade = self.judge.grade(answer.text, answer.citations, answer.facts)
        answer.grounding_score = grade.score
        answer.grounded = grade.passes(self.settings.safety.grounding_threshold)
        self.audit.record(
            "grounding",
            "judged",
            request_id=request.request_id,
            conversation_id=request.conversation_id,
            outcome="pass" if answer.grounded else "fail",
            score=round(grade.score, 3),
            label=grade.label,
            unsafe_probability=round(grade.unsafe_probability, 4),
            threshold=self.settings.safety.grounding_threshold,
            judge_provider=grade.provider,
            judge_model=grade.model,
            judge_error=grade.error,
        )

        unsafe = grade.unsafe_probability >= self.settings.safety.injection_threshold
        if not answer.grounded or unsafe:
            explanation = (
                f"grounding score {grade.score:.2f} ({grade.label}) below "
                f"{self.settings.safety.grounding_threshold:.2f}"
                if not answer.grounded
                else f"judge flagged unsafe content at {grade.unsafe_probability:.2f}"
            )
            return self._handoff(
                context,
                HandoffReason.UNGROUNDED_ANSWER,
                explanation,
                started,
                first_event,
                partial=answer,
            )

        self.audit.record(
            "response",
            "delivered",
            request_id=request.request_id,
            conversation_id=request.conversation_id,
            agent=answer.agent,
            citation_count=len(answer.citations),
            automated=True,
        )
        return TurnResult(
            request_id=request.request_id,
            conversation_id=request.conversation_id,
            redacted_text=redaction.text,
            routing=routing,
            answer=answer,
            audit_events=self.audit.as_dicts()[first_event:],
            total_latency_ms=(time.perf_counter() - started) * 1000,
        )

    # -- handoff ---------------------------------------------------------------

    def _handoff(
        self,
        context: AgentContext,
        reason: HandoffReason,
        explanation: str,
        started: float,
        first_event: int,
        *,
        partial: AgentAnswer | None = None,
        priority: str | None = None,
    ) -> TurnResult:
        handoff = self.escalation.build(
            context, reason, explanation, partial=partial, priority=priority
        )
        self.audit.record(
            "handoff",
            reason.value,
            request_id=context.request.request_id,
            conversation_id=context.request.conversation_id,
            outcome="queued",
            queue=handoff.queue,
            priority=handoff.priority,
            explanation=explanation,
            automated=False,
        )
        return TurnResult(
            request_id=context.request.request_id,
            conversation_id=context.request.conversation_id,
            redacted_text=context.redacted_text,
            routing=context.routing,
            handoff=handoff,
            audit_events=self.audit.as_dicts()[first_event:],
            total_latency_ms=(time.perf_counter() - started) * 1000,
        )

    # -- introspection ---------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        """Self-description used by the UI and the CLI banner."""
        return {
            "mode": self.settings.mode.value,
            "taxonomy_version": TAXONOMY_VERSION,
            "decision_client": getattr(self.router.client, "name", "unknown"),
            "judge": self.judge.name,
            "composer": self.composer.name,
            "agents": [agent.describe() for agent in self.agents.values()],
            "indexes": {name: len(index) for name, index in self.indexes.items()},
            "tools": self.registry.describe() if self.registry else [],
            "egress_policy": (
                {
                    "name": self.egress_policy.name,
                    "mode": self.egress_policy.mode.value,
                    "default_action": self.egress_policy.default_action.value,
                    "rules": [r.model_dump(mode="json") for r in self.egress_policy.rules],
                }
                if self.egress_policy
                else None
            ),
            "thresholds": {
                "route": self.settings.safety.route_thresholds,
                "default_route": self.settings.safety.default_route_threshold,
                "grounding": self.settings.safety.grounding_threshold,
                "injection": self.settings.safety.injection_threshold,
                "clinician_required": self.settings.safety.clinician_required_threshold,
                "urgency": self.settings.safety.urgency_escalation_level,
            },
        }


def build(settings: Settings | None = None) -> Orchestrator:
    """Composition root. The only function that branches on runtime mode."""
    settings = settings or Settings.from_env()

    sinks = [MemoryAuditSink()]
    if settings.audit_sink_path is not None:
        sinks.append(JsonlAuditSink(settings.audit_sink_path))
    audit = AuditLog(sinks)

    egress_policy = load_egress_policy(settings.safety.egress_policy_path)
    indexes = build_indexes(embedder=HashingEmbedder(), root=settings.data_root / "corpus")

    fhir_client: FhirClient = LocalFhirClient(
        FhirStore.from_directory(settings.data_root / "fhir" / "bundles")
    )
    registry = build_fhir_registry(fhir_client)
    gateway = ApiManagementSimulator(
        GatewayPolicy(
            rate_limit_calls=settings.gateway.rate_limit_calls,
            rate_limit_window_seconds=settings.gateway.rate_limit_window_seconds,
        ),
        audit=audit,
    )
    mcp = InProcessMcpClient(
        registry,
        gateway,
        CallContext(
            agent_id=settings.gateway.agent_id,
            subscription_key=settings.gateway.subscription_key or "local-demo-key",
            purpose_of_use="TREAT",
        ),
        audit=audit,
    )

    decision_client, judge_client, composer = _build_model_clients(settings)

    return Orchestrator(
        settings=settings,
        router=Router(decision_client, settings),
        agents={
            "benefits": BenefitsAgent(),
            "clinical_policy": ClinicalPolicyAgent(),
            "prior_auth": PriorAuthAgent(),
        },
        escalation=EscalationAgent(),
        indexes=indexes,
        composer=composer,
        judge=DecisionModelJudge(judge_client),
        audit=audit,
        mcp=mcp,
        registry=registry,
        egress_policy=egress_policy,
    )


def _build_model_clients(
    settings: Settings,
) -> tuple[DecisionClient, DecisionClient, Composer]:
    """Pick the decision, judge and generation clients for the active mode."""
    if settings.mode is RuntimeMode.MOCK:
        shared = LexicalJudgeClient()
        return shared, shared, ExtractiveComposer()

    settings.require_azure()
    assert settings.foundry.endpoint is not None
    primary = FoundryDecisionClient(
        settings.foundry.endpoint,
        settings.foundry.decision_deployment,
        api_key=settings.foundry.api_key,
        timeout=settings.foundry.timeout_seconds,
    )
    # A hosted-model outage must degrade, not fail. The heuristic keeps classifying
    # and caps its own confidence so every turn lands in front of a human.
    resilient = ResilientDecisionClient(primary, HeuristicDecisionClient())
    composer = FoundryChatComposer(
        settings.foundry.endpoint,
        settings.foundry.chat_deployment,
        api_key=settings.foundry.api_key,
    )
    return resilient, resilient, composer
