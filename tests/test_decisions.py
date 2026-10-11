"""Decision layer: parsing, the offline scorer, resilience and calibration."""

from __future__ import annotations

import httpx
import pytest

from payer_agents.decisions.base import (
    ChoiceAnswer,
    ChoiceQuestion,
    DecisionError,
    DecisionRequest,
    NoulAnswer,
    NoulQuestion,
    ScoreAnswer,
    ScoreQuestion,
    normalise,
    parse_decision_payload,
    softmax,
)
from payer_agents.decisions.calibration import (
    calibration_report,
    reliability_bins,
    threshold_sweep,
)
from payer_agents.decisions.fallback import CircuitBreaker, ResilientDecisionClient
from payer_agents.decisions.foundry import FoundryDecisionClient
from payer_agents.decisions.mock import HeuristicDecisionClient, MockDecisionClient, stem, tokenize

CHOICE = ChoiceQuestion(
    instructions="Which team?",
    criteria={"billing": "Charges and refunds", "engineering": "Bugs and outages"},
)
SIMPLE_REQUEST = DecisionRequest(
    state="the api returns 500, bugs and outages everywhere", questions={"team": CHOICE}
)

#: Five options, like the real routing taxonomy. Needed wherever a test exercises a
#: confidence ceiling: with two options nothing can score below 0.5, so a 0.45 cap is
#: unreachable and the test would be asserting arithmetic rather than behaviour.
WIDE_CHOICE = ChoiceQuestion(
    instructions="Which team?",
    criteria={
        "billing": "Charges and refunds",
        "engineering": "Bugs and outages",
        "support": "How-to questions",
        "security": "Breaches and vulnerabilities",
        "cannot_tell": "Unclear",
    },
)
WIDE_REQUEST = DecisionRequest(
    state="the api returns 500, bugs and outages everywhere", questions={"team": WIDE_CHOICE}
)


# ---------------------------------------------------------------- wire format


def test_wire_body_uses_deployment_name_not_model_name():
    body = SIMPLE_REQUEST.wire("my-deployment")
    assert body["model"] == "my-deployment"
    assert body["questions"]["team"]["type"] == "choice"
    assert set(body["questions"]["team"]["criteria"]) == {"billing", "engineering"}


def test_noul_omits_optional_criteria():
    question = NoulQuestion(instructions="Is it true?")
    assert "criteria" not in question.wire()
    assert NoulQuestion(instructions="x", criteria={"true": "a"}).wire()["criteria"] == {
        "true": "a"
    }


def test_score_question_wire_preserves_level_order():
    question = ScoreQuestion(instructions="How bad?", criteria=["Low", "High"])
    assert question.wire()["criteria"] == ["Low", "High"]
    assert question.option_keys == ["Low", "High"]


# ------------------------------------------------------------------- parsing


def test_parse_choice_with_probabilities():
    payload = {
        "model": "microsoft-decision-1",
        "answers": {
            "team": {
                "type": "choice",
                "choice": "engineering",
                "probabilities": {"billing": 0.1, "engineering": 0.9},
            }
        },
        "usage": {"input_tokens": 12},
    }
    response = parse_decision_payload(payload, SIMPLE_REQUEST, provider="foundry")
    answer = response.choice("team")
    assert answer.choice == "engineering"
    assert answer.confidence == pytest.approx(0.9)
    assert response.usage["input_tokens"] == 12


def test_parse_tolerates_alternate_probability_key():
    """The published docs pin ``choice``; the probability key is parsed defensively."""
    payload = {
        "answers": {
            "team": {
                "type": "choice",
                "choice": "billing",
                "option_probabilities": {"billing": 2, "engineering": 2},
            }
        }
    }
    answer = parse_decision_payload(payload, SIMPLE_REQUEST).choice("team")
    assert answer.probabilities == {"billing": 0.5, "engineering": 0.5}


def test_parse_infers_choice_from_probabilities_when_absent():
    payload = {
        "answers": {
            "team": {"type": "choice", "probabilities": {"billing": 0.2, "engineering": 0.8}}
        }
    }
    assert parse_decision_payload(payload, SIMPLE_REQUEST).choice("team").choice == "engineering"


def test_parse_synthesises_probabilities_when_only_a_choice_is_returned():
    payload = {"answers": {"team": {"type": "choice", "choice": "billing"}}}
    answer = parse_decision_payload(payload, SIMPLE_REQUEST).choice("team")
    assert answer.probabilities == {"billing": 1.0, "engineering": 0.0}


@pytest.mark.parametrize(
    "payload, message",
    [
        ("not an object", "non-object"),
        ({"no_answers": {}}, "no answers object"),
        ({"answers": {"other": {}}}, "no answer for question"),
        ({"answers": {"team": {"type": "choice", "choice": "marketing"}}}, "unsupported option"),
    ],
)
def test_parse_raises_rather_than_degrading_silently(payload, message):
    with pytest.raises(DecisionError, match=message):
        parse_decision_payload(payload, SIMPLE_REQUEST)


def test_parse_noul_accepts_probability_or_mapping():
    request = DecisionRequest(state="x", questions={"gate": NoulQuestion(instructions="?")})
    assert parse_decision_payload(
        {"answers": {"gate": {"type": "noul", "probability": 0.7}}}, request
    ).noul("gate") == pytest.approx(0.7)
    assert parse_decision_payload(
        {"answers": {"gate": {"type": "noul", "probabilities": {"true": 0.4}}}}, request
    ).noul("gate") == pytest.approx(0.4)
    with pytest.raises(DecisionError, match="no probability"):
        parse_decision_payload({"answers": {"gate": {"type": "noul"}}}, request)


def test_parse_score_derives_score_from_distribution():
    request = DecisionRequest(
        state="x",
        questions={"sev": ScoreQuestion(instructions="?", criteria=["a", "b", "c"])},
    )
    answer = parse_decision_payload(
        {"answers": {"sev": {"type": "score", "probabilities": [0.0, 0.0, 1.0]}}}, request
    ).score("sev")
    assert answer is not None
    assert answer.score == pytest.approx(2.0)
    assert answer.nearest_level == "c"
    with pytest.raises(DecisionError, match="no score"):
        parse_decision_payload({"answers": {"sev": {"type": "score"}}}, request)


def test_response_accessors_reject_type_mismatch():
    response = parse_decision_payload(
        {"answers": {"team": {"type": "choice", "choice": "billing"}}}, SIMPLE_REQUEST
    )
    with pytest.raises(DecisionError, match="did not return a choice"):
        response.choice("missing")
    assert response.noul("missing", default=0.25) == 0.25
    assert response.score("team") is None


# ------------------------------------------------------------------ helpers


def test_normalise_handles_all_zero_and_negative_inputs():
    assert normalise({"a": 0.0, "b": 0.0}) == {"a": 0.5, "b": 0.5}
    assert normalise({"a": -1.0, "b": 3.0}) == {"a": 0.0, "b": 1.0}


def test_softmax_is_stable_and_sums_to_one():
    result = softmax({"a": 1000.0, "b": 999.0})
    assert sum(result.values()) == pytest.approx(1.0)
    assert result["a"] > result["b"]
    assert softmax({}) == {}


@pytest.mark.parametrize(
    "word, expected",
    [
        ("specialists", "specialist"),
        ("policies", "policy"),
        ("member's", "member"),
        ("status", "status"),
        ("class", "class"),
        ("mri", "mri"),
    ],
)
def test_stemmer_strips_plurals_without_mangling_short_or_ss_words(word, expected):
    assert stem(word) == expected


def test_tokenize_drops_stopwords_and_short_tokens():
    tokens = tokenize("What is my copay for a specialist visit?")
    assert "copay" in tokens and "specialist" in tokens
    assert "what" not in tokens and "is" not in tokens


# ------------------------------------------------------------- mock scoring


def test_mock_client_is_deterministic():
    client = MockDecisionClient()
    first = client.decide(SIMPLE_REQUEST).choice("team").probabilities
    second = MockDecisionClient().decide(SIMPLE_REQUEST).choice("team").probabilities
    assert first == second


def test_mock_client_answers_every_question_type():
    request = DecisionRequest(
        state={"message": "chest pain and shortness of breath"},
        questions={
            "team": CHOICE,
            "severity": ScoreQuestion(instructions="?", criteria=["Low", "Med", "High"]),
            "urgent": NoulQuestion(instructions="Is this urgent?"),
        },
    )
    response = MockDecisionClient().decide(request)
    assert isinstance(response.answers["team"], ChoiceAnswer)
    assert isinstance(response.answers["severity"], ScoreAnswer)
    assert isinstance(response.answers["urgent"], NoulAnswer)
    assert response.usage["output_tokens"] == 0


def test_mock_probabilities_form_a_distribution():
    probabilities = MockDecisionClient().decide(SIMPLE_REQUEST).choice("team").probabilities
    assert sum(probabilities.values()) == pytest.approx(1.0)
    assert all(0.0 <= p <= 1.0 for p in probabilities.values())


def test_confidence_ceiling_shrinks_toward_uniform_without_changing_the_winner():
    plain = MockDecisionClient().decide(WIDE_REQUEST).choice("team")
    capped = MockDecisionClient(confidence_ceiling=0.30).decide(WIDE_REQUEST).choice("team")
    assert capped.choice == plain.choice
    assert capped.confidence == pytest.approx(0.30, abs=1e-6)
    assert capped.confidence < plain.confidence
    assert sum(capped.probabilities.values()) == pytest.approx(1.0)


def test_confidence_ceiling_below_uniform_collapses_to_uniform_not_negative_mass():
    capped = MockDecisionClient(confidence_ceiling=0.1).decide(SIMPLE_REQUEST).choice("team")
    assert all(p >= 0.0 for p in capped.probabilities.values())
    assert capped.confidence == pytest.approx(0.5)


def test_heuristic_fallback_marks_itself_degraded_and_stays_under_thresholds():
    response = HeuristicDecisionClient().decide(WIDE_REQUEST)
    assert response.degraded is True
    assert "fallback" in (response.degraded_reason or "")
    # Below the lowest routing threshold in SafetySettings, so every turn defers.
    assert response.choice("team").confidence <= 0.45


def test_mock_abstains_when_nothing_matches():
    """The whole point of an abstention option: no cue, no confident answer."""
    request = DecisionRequest(
        state="qqqq zzzz",
        questions={
            "team": ChoiceQuestion(
                instructions="?",
                criteria={"a": "alpha things", "b": "beta things", "cannot_tell": "unclear"},
            )
        },
    )
    answer = MockDecisionClient(priors={"team.cannot_tell": 1.7}).decide(request).choice("team")
    assert answer.choice == "cannot_tell"


# ----------------------------------------------------------- foundry client


def _transport(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_foundry_client_posts_to_the_decision_path_with_api_key():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["api_key"] = request.headers.get("api-key")
        return httpx.Response(
            200,
            json={
                "model": "microsoft-decision-1",
                "answers": {
                    "team": {
                        "type": "choice",
                        "choice": "billing",
                        "probabilities": {"billing": 0.8, "engineering": 0.2},
                    }
                },
            },
        )

    client = FoundryDecisionClient(
        "https://x.services.ai.azure.com/", "dep", api_key="k", client=_transport(handler)
    )
    response = client.decide(SIMPLE_REQUEST)
    assert seen["url"].endswith("/providers/microsoft/v1/systemone")
    assert seen["api_key"] == "k"
    assert response.provider == "foundry"
    assert response.choice("team").choice == "billing"


def test_foundry_client_uses_bearer_token_when_no_key():
    class FakeCredential:
        def get_token(self, *scopes):
            assert scopes == ("https://cognitiveservices.azure.com/.default",)
            return type("T", (), {"token": "tok"})()

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200, json={"answers": {"team": {"type": "choice", "choice": "billing"}}}
        )

    FoundryDecisionClient(
        "https://x", "dep", credential=FakeCredential(), client=_transport(handler)
    ).decide(SIMPLE_REQUEST)
    assert seen["auth"] == "Bearer tok"


def test_foundry_client_retries_429_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, text="slow down")
        return httpx.Response(
            200, json={"answers": {"team": {"type": "choice", "choice": "billing"}}}
        )

    client = FoundryDecisionClient(
        "https://x", "dep", api_key="k", client=_transport(handler), sleep=lambda _: None
    )
    assert client.decide(SIMPLE_REQUEST).choice("team").choice == "billing"
    assert calls["n"] == 3


def test_foundry_client_does_not_retry_a_400():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad question shape")

    client = FoundryDecisionClient(
        "https://x", "dep", api_key="k", client=_transport(handler), sleep=lambda _: None
    )
    with pytest.raises(DecisionError, match="HTTP 400"):
        client.decide(SIMPLE_REQUEST)
    assert calls["n"] == 1


def test_foundry_client_wraps_transport_and_json_errors():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with pytest.raises(DecisionError, match="unreachable"):
        FoundryDecisionClient(
            "https://x", "dep", api_key="k", client=_transport(boom), sleep=lambda _: None
        ).decide(SIMPLE_REQUEST)

    with pytest.raises(DecisionError, match="invalid JSON"):
        FoundryDecisionClient(
            "https://x",
            "dep",
            api_key="k",
            client=_transport(lambda r: httpx.Response(200, text="<html>")),
        ).decide(SIMPLE_REQUEST)


def test_foundry_client_requires_an_endpoint():
    with pytest.raises(ValueError, match="endpoint is required"):
        FoundryDecisionClient("", "dep")


# -------------------------------------------------------------- resilience


class _AlwaysFails(MockDecisionClient):
    def decide(self, request):
        raise DecisionError("primary down")


def test_circuit_breaker_opens_after_threshold_and_half_opens_after_cooldown():
    now = {"t": 0.0}
    breaker = CircuitBreaker(threshold=2, cooldown_seconds=10, clock=lambda: now["t"])
    breaker.record_failure()
    assert not breaker.is_open
    breaker.record_failure()
    assert breaker.is_open
    now["t"] = 11.0
    assert not breaker.is_open  # half-open probe allowed
    breaker.record_success()
    assert breaker.failures == 0


def test_resilient_client_falls_back_and_flags_degradation():
    degradations = []
    resilient = ResilientDecisionClient(
        _AlwaysFails(),
        HeuristicDecisionClient(),
        CircuitBreaker(threshold=1),
        on_degrade=degradations.append,
    )
    response = resilient.decide(SIMPLE_REQUEST)
    assert response.degraded is True
    assert degradations == ["primary down"]

    # Breaker is now open: the next call skips the primary entirely.
    second = resilient.decide(SIMPLE_REQUEST)
    assert second.degraded_reason == "circuit breaker open"


def test_resilient_client_passes_through_when_primary_is_healthy():
    resilient = ResilientDecisionClient(MockDecisionClient(), HeuristicDecisionClient())
    assert resilient.decide(SIMPLE_REQUEST).degraded is False
    resilient.close()


# ------------------------------------------------------------- calibration


def test_calibration_report_on_a_perfectly_calibrated_sample():
    outcomes = [(0.9, True)] * 9 + [(0.9, False)]
    report = calibration_report(outcomes, bin_count=10)
    assert report.sample_size == 10
    assert report.accuracy == pytest.approx(0.9)
    assert report.expected_calibration_error == pytest.approx(0.0, abs=1e-9)
    assert not report.overconfident
    assert "ECE=" in report.summary()


def test_calibration_detects_overconfidence():
    report = calibration_report([(0.95, False)] * 10)
    assert report.overconfident
    assert report.expected_calibration_error == pytest.approx(0.95)
    assert report.brier_score == pytest.approx(0.9025)


def test_calibration_report_handles_an_empty_sample():
    report = calibration_report([])
    assert report.sample_size == 0
    assert report.expected_calibration_error == 0.0
    assert len(report.bins) == 10


def test_reliability_bins_place_values_and_reject_bad_bin_counts():
    bins = reliability_bins([(0.05, True), (0.95, False)], bin_count=2)
    assert bins[0].count == 1 and bins[1].count == 1
    assert bins[1].gap == pytest.approx(0.95)
    with pytest.raises(ValueError):
        reliability_bins([], bin_count=0)


def test_threshold_sweep_trades_automation_against_error_rate():
    outcomes = [(0.95, True), (0.95, True), (0.4, False), (0.4, True)]
    points = {p.threshold: p for p in threshold_sweep(outcomes, [0.0, 0.5, 1.0])}
    assert points[0.0].automated == 4
    assert points[0.0].automated_error_rate == pytest.approx(0.25)
    assert points[0.5].automated == 2
    assert points[0.5].automated_error_rate == 0.0
    assert points[1.0].automation_rate == 0.0
    assert points[1.0].automated_error_rate == 0.0
