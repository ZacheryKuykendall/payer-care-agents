"""Offline decision model.

Mock mode needs a scorer that is deterministic (so tests assert on exact numbers),
fast (so the demo feels live) and *honest about uncertainty* (so the human-in-the-loop
path actually gets exercised during a walkthrough instead of being dead code).

The scorer combines two signals per option:

1. weighted phrase cues from :mod:`payer_agents.decisions.lexicon`
2. content-word overlap between the state and the option's criteria text, which is
   what keeps the mock useful when someone adds a new option and forgets the lexicon

Those become logits, and a softmax turns them into a distribution. It is a bag-of-
phrases classifier wearing a decision-model costume, and the README says so.
"""

from __future__ import annotations

import math
import re
import time

from payer_agents.decisions.base import (
    Answer,
    ChoiceAnswer,
    ChoiceQuestion,
    DecisionClient,
    DecisionRequest,
    DecisionResponse,
    NoulAnswer,
    NoulQuestion,
    Question,
    ScoreAnswer,
    ScoreQuestion,
    normalise,
    softmax,
)
from payer_agents.decisions.lexicon import CUE_LEXICON, PRIORS, CueMap

_WORD = re.compile(r"[a-z0-9][a-z0-9\-']*")
_STOPWORDS = frozenset(
    """a an and are as at be but by for from has have how i if in is it its my of on or
    that the their there they this to was what when where which who will with you your
    about do does did can could should would me we us our""".split()
)

#: Evidence is capped before it becomes a logit. Without a cap, a message that happens
#: to repeat four cue phrases would report 0.999 confidence, which is exactly the kind
#: of fake certainty the human-review gate exists to catch.
MAX_EVIDENCE = 1.5
LOGIT_GAIN = 3.2
CRITERIA_WEIGHT = 0.55
NOUL_GAIN = 4.0
NOUL_BIAS = -2.3


def stem(word: str) -> str:
    """Crude plural stripping.

    Not linguistics -- just enough that "specialist" matches "specialists" and
    "criterion" does not silently miss "criteria". Applied to both sides of every
    comparison, so consistency matters more than correctness here.
    """
    if word.endswith("'s") or word.endswith("s'"):
        word = word[:-2]
    if len(word) > 4:
        if word.endswith("ies"):
            return word[:-3] + "y"
        if word.endswith("sses") or word.endswith("shes") or word.endswith("ches"):
            return word[:-2]
        if word.endswith("s") and not word.endswith("ss") and not word.endswith("us"):
            return word[:-1]
    return word


def tokenize(text: str) -> set[str]:
    """Content words, lowercased, stopwords dropped, crudely stemmed."""
    return {
        stem(w)
        for w in _WORD.findall(text.lower())
        if w not in _STOPWORDS and len(w) > 2
    }


def _flatten_state(state: object) -> str:
    if isinstance(state, str):
        return state
    if isinstance(state, dict):
        return " ".join(f"{k} {_flatten_state(v)}" for k, v in state.items())
    if isinstance(state, (list, tuple)):
        return " ".join(_flatten_state(v) for v in state)
    return str(state)


class MockDecisionClient(DecisionClient):
    """Deterministic, dependency-free stand-in for ``Microsoft-Decision-1``."""

    name = "mock-decision"
    provider = "mock"
    model_name = "mock-decision-1"

    def __init__(
        self,
        lexicon: CueMap | None = None,
        priors: dict[str, float] | None = None,
        *,
        confidence_ceiling: float | None = None,
    ) -> None:
        self.lexicon = CUE_LEXICON if lexicon is None else lexicon
        self.priors = PRIORS if priors is None else priors
        self.confidence_ceiling = confidence_ceiling
        self.call_count = 0

    def decide(self, request: DecisionRequest) -> DecisionResponse:
        started = time.perf_counter()
        self.call_count += 1
        text = _flatten_state(request.state)
        lowered = text.lower()
        tokens = tokenize(text)

        answers: dict[str, Answer] = {
            name: self._answer(name, question, lowered, tokens)
            for name, question in request.questions.items()
        }
        return DecisionResponse(
            answers=answers,
            model=self.model_name,
            provider=self.provider,
            usage={"input_tokens": len(tokens), "output_tokens": 0},
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    # -- scoring ---------------------------------------------------------------

    def _cue_score(self, key: str, lowered: str) -> float:
        cues = self.lexicon.get(key, {})
        return sum(weight for phrase, weight in cues.items() if phrase in lowered)

    def _prior(self, key: str, option: str) -> float:
        if key in self.priors:
            return self.priors[key]
        return self.priors.get(option, 0.0)

    def _evidence(self, question_name: str, option: str, criteria: str, lowered: str,
                  tokens: set[str]) -> float:
        key = f"{question_name}.{option}"
        cue = self._cue_score(key, lowered) or self._cue_score(option, lowered)
        overlap = 0.0
        criteria_tokens = tokenize(criteria)
        if criteria_tokens:
            overlap = len(criteria_tokens & tokens) / len(criteria_tokens)
        return min(MAX_EVIDENCE, cue + CRITERIA_WEIGHT * overlap)

    def _answer(
        self, name: str, question: Question, lowered: str, tokens: set[str]
    ) -> Answer:
        if isinstance(question, ChoiceQuestion):
            logits = {
                option: self._prior(f"{name}.{option}", option)
                + LOGIT_GAIN * self._evidence(name, option, criteria, lowered, tokens)
                for option, criteria in question.criteria.items()
            }
            probabilities = self._apply_ceiling(softmax(logits))
            choice = max(probabilities, key=lambda k: (probabilities[k], k))
            return ChoiceAnswer(choice=choice, probabilities=probabilities)

        if isinstance(question, NoulQuestion):
            criteria = question.criteria or {}
            true_evidence = self._evidence(
                name, "true", criteria.get("true", question.instructions), lowered, tokens
            )
            false_evidence = self._evidence(name, "false", criteria.get("false", ""), lowered,
                                            tokens)
            logit = NOUL_GAIN * (true_evidence - false_evidence) + NOUL_BIAS
            return NoulAnswer(probability=1.0 / (1.0 + math.exp(-logit)))

        assert isinstance(question, ScoreQuestion)
        logits = {
            level: self._prior(f"{name}.{level}", level)
            + LOGIT_GAIN * self._evidence(name, level, level, lowered, tokens)
            for level in question.criteria
        }
        distribution = softmax(logits)
        ordered = [distribution[level] for level in question.criteria]
        score = sum(index * p for index, p in enumerate(ordered))
        return ScoreAnswer(score=score, probabilities=ordered, levels=list(question.criteria))

    def _apply_ceiling(self, probabilities: dict[str, float]) -> dict[str, float]:
        """Shrink a distribution toward uniform so its peak never exceeds the ceiling."""
        ceiling = self.confidence_ceiling
        if ceiling is None or not probabilities:
            return probabilities
        count = len(probabilities)
        uniform = 1.0 / count
        peak = max(probabilities.values())
        if peak <= ceiling or peak <= uniform:
            return probabilities
        blend = (ceiling - uniform) / (peak - uniform)
        return normalise({k: blend * v + (1 - blend) * uniform for k, v in probabilities.items()})


class HeuristicDecisionClient(MockDecisionClient):
    """Last-resort fallback for when the hosted decision model is unavailable.

    Same scorer, but every distribution is squashed below the lowest routing threshold.
    The effect is deliberate: during a model outage the system keeps classifying well
    enough to pick the right queue, and keeps sending everything to a human. Silently
    continuing to auto-answer on a degraded classifier is the failure mode worth
    designing against.
    """

    name = "heuristic-fallback"
    provider = "heuristic"
    model_name = "lexical-heuristic-v1"

    def __init__(self, confidence_ceiling: float = 0.45, **kwargs: object) -> None:
        super().__init__(confidence_ceiling=confidence_ceiling, **kwargs)  # type: ignore[arg-type]

    def decide(self, request: DecisionRequest) -> DecisionResponse:
        response = super().decide(request)
        response.degraded = True
        response.degraded_reason = "primary decision model unavailable; lexical fallback in use"
        return response
