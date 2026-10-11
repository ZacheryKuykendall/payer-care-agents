"""Routing taxonomy and the safety gates around it.

The gate tests are the ones that matter. A router that classifies well and gates
badly is more dangerous than one that classifies badly, because the bad classifier
at least produces low confidence.
"""

from __future__ import annotations

import pytest

from payer_agents.config import Settings
from payer_agents.decisions.base import DecisionClient, DecisionError
from payer_agents.decisions.mock import MockDecisionClient
from payer_agents.domain import Channel, HandoffReason, Intent, MemberRequest, RoutingDecision
from payer_agents.routing.router import Router
from payer_agents.routing.taxonomy import (
    TAXONOMY_VERSION,
    URGENCY_LEVELS,
    build_routing_request,
    build_state,
    intent_criteria,
)


@pytest.fixture
def router(settings) -> Router:
    return Router(MockDecisionClient(), settings)


# ------------------------------------------------------------------ taxonomy


def test_every_intent_has_criteria_including_the_abstention():
    criteria = intent_criteria()
    assert set(criteria) == {intent.value for intent in Intent}
    assert "cannot_tell" in criteria
    assert all(len(text) > 40 for text in criteria.values())


def test_routing_request_asks_four_questions_against_one_state():
    request = build_routing_request(MemberRequest(text="hello"))
    assert set(request.questions) == {"route", "urgency", "clinician_required", "prompt_injection"}
    wire = request.wire("dep")
    assert wire["questions"]["urgency"]["criteria"] == URGENCY_LEVELS
    assert wire["state"]["member_message"] == "hello"


def test_state_excludes_taxonomy_version():
    """Provenance belongs in the audit record, not in the model input."""
    state = build_state(MemberRequest(text="hello"))
    assert "taxonomy_version" not in state
    assert TAXONOMY_VERSION  # still available to the orchestrator for auditing


def test_state_marks_whether_the_member_is_identified():
    assert build_state(MemberRequest(text="x"))["member_identified"] is False
    assert build_state(MemberRequest(text="x", member_id="NW-100001"))["member_identified"] is True
    assert (
        build_state(MemberRequest(text="x", channel=Channel.CALL_CENTER))["channel"]
        == "call_center"
    )


# ----------------------------------------------------------------- classify


@pytest.mark.parametrize(
    "text, expected",
    [
        ("What is my copay for a specialist visit?", Intent.BENEFITS_COVERAGE),
        (
            "What are the coverage criteria for a continuous glucose monitor?",
            Intent.CLINICAL_POLICY,
        ),
        ("What is the status of my prior authorization? NW-100002", Intent.PRIOR_AUTH_STATUS),
        ("I have chest pain and cannot breathe", Intent.CARE_ESCALATION),
        ("hi", Intent.CANNOT_TELL),
    ],
)
def test_classify_routes_representative_messages(router, text, expected):
    assert router.classify(MemberRequest(text=text)).intent is expected


def test_classify_records_the_whole_distribution_not_just_the_winner():
    decision = Router(MockDecisionClient(), Settings()).classify(
        MemberRequest(text="What is my copay for a specialist visit?")
    )
    assert set(decision.probabilities) == {intent.value for intent in Intent}
    assert sum(decision.probabilities.values()) == pytest.approx(1.0)
    assert decision.top_margin > 0


class _BrokenClient(DecisionClient):
    name = "broken"

    def decide(self, request):
        raise DecisionError("model unavailable")


def test_classify_turns_a_model_outage_into_maximum_uncertainty(settings):
    decision = Router(_BrokenClient(), settings).classify(MemberRequest(text="anything"))
    assert decision.intent is Intent.CANNOT_TELL
    assert decision.degraded is True
    assert decision.confidence == pytest.approx(0.2)
    assert decision.provider == "unavailable"


def test_top_margin_handles_degenerate_distributions():
    assert (
        RoutingDecision(
            intent=Intent.CANNOT_TELL, confidence=1.0, probabilities={"a": 1.0}
        ).top_margin
        == 1.0
    )
    assert (
        RoutingDecision(intent=Intent.CANNOT_TELL, confidence=0.0, probabilities={}).top_margin
        == 0.0
    )


# --------------------------------------------------------------------- gate


def _decision(**overrides) -> RoutingDecision:
    base = dict(
        intent=Intent.BENEFITS_COVERAGE,
        confidence=0.95,
        probabilities={"benefits_coverage": 0.95, "cannot_tell": 0.05},
    )
    base.update(overrides)
    return RoutingDecision(**base)


def test_gate_allows_a_confident_routable_intent(router):
    outcome = router.gate(_decision())
    assert outcome.allow is True
    assert outcome.reason is None


def test_injection_gate_fires_before_everything_else(router):
    """Order matters: a manipulated request must never reach a tool, even a
    confident, non-urgent one."""
    outcome = router.gate(
        _decision(injection_probability=0.9, urgency=3.0, clinician_required_probability=0.9)
    )
    assert outcome.allow is False
    assert outcome.reason is HandoffReason.SUSPECTED_INJECTION
    assert outcome.priority == "security"


def test_urgency_gate_outranks_confidence(router):
    outcome = router.gate(_decision(urgency=2.5, urgency_label="Emergent"))
    assert outcome.reason is HandoffReason.URGENT_CLINICAL
    assert outcome.priority == "urgent"


def test_clinician_gate_fires_on_a_confident_benefits_question(router):
    outcome = router.gate(_decision(clinician_required_probability=0.7))
    assert outcome.reason is HandoffReason.CLINICIAN_REQUIRED
    assert outcome.priority == "clinical"


def test_abstention_defers_even_at_high_confidence(router):
    outcome = router.gate(_decision(intent=Intent.CANNOT_TELL, confidence=0.99))
    assert outcome.reason is HandoffReason.ABSTAINED


def test_escalation_intent_always_goes_to_a_human(router):
    calm = router.gate(_decision(intent=Intent.CARE_ESCALATION, confidence=0.99, urgency=0.1))
    assert calm.allow is False
    assert calm.reason is HandoffReason.CLINICIAN_REQUIRED

    worried = router.gate(_decision(intent=Intent.CARE_ESCALATION, confidence=0.99, urgency=1.5))
    assert worried.reason is HandoffReason.URGENT_CLINICAL
    assert worried.priority == "high"


def test_thresholds_are_per_intent(router, settings):
    """Prior authorization is consequential, so it costs more confidence to automate."""
    assert settings.threshold_for("prior_auth_status") > settings.threshold_for("benefits_coverage")
    assert router.gate(_decision(intent=Intent.PRIOR_AUTH_STATUS, confidence=0.70)).allow is False
    assert router.gate(_decision(intent=Intent.BENEFITS_COVERAGE, confidence=0.70)).allow is True


def test_unknown_intent_falls_back_to_the_default_threshold(settings):
    assert settings.threshold_for("something_new") == settings.safety.default_route_threshold


def test_gate_outcome_is_readable_in_a_log(router):
    assert "GateOutcome" in repr(router.gate(_decision()))
