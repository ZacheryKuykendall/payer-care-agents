"""Request/response contract for a decision model.

Modelled on the ``Microsoft-Decision-1`` decision API in Microsoft Foundry
(``POST {endpoint}/providers/microsoft/v1/systemone``), which takes a ``state`` plus a
named set of typed questions and returns an ``answers`` object keyed by question name.

Three question types exist upstream:

===========  ========================================  ==========================
``noul``     "Is this true?"                           probability in [0, 1]
``choice``   "Which option is it?"                     selected option + per-option
                                                       probabilities
``score``    "How much?"                               probability-weighted position
                                                       on an ordered scale
===========  ========================================  ==========================

Response parsing is deliberately tolerant. The published docs show the request bodies
and the ``answers[name].choice`` accessor verbatim, but not the exact spelling of the
probability field for every type, so :func:`parse_decision_payload` accepts the
plausible spellings and normalises them. ``tests/test_decisions_contract.py`` pins the
shapes we rely on; re-run it against a live deployment before trusting Azure mode.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class DecisionError(RuntimeError):
    """The decision model could not produce a usable answer."""


class QuestionType(str, Enum):
    NOUL = "noul"
    CHOICE = "choice"
    SCORE = "score"


class NoulQuestion(BaseModel):
    """A yes/no gate. ``criteria`` is optional but sharpens borderline cases."""

    type: Literal[QuestionType.NOUL] = QuestionType.NOUL
    instructions: str
    criteria: dict[str, str] | None = None

    def wire(self) -> dict[str, Any]:
        body: dict[str, Any] = {"type": "noul", "instructions": self.instructions}
        if self.criteria:
            body["criteria"] = dict(self.criteria)
        return body

    @property
    def option_keys(self) -> list[str]:
        return ["true", "false"]


class ChoiceQuestion(BaseModel):
    """Exactly one label from a fixed set. ``criteria`` maps option -> description."""

    type: Literal[QuestionType.CHOICE] = QuestionType.CHOICE
    instructions: str
    criteria: dict[str, str]

    def wire(self) -> dict[str, Any]:
        return {
            "type": "choice",
            "instructions": self.instructions,
            "criteria": dict(self.criteria),
        }

    @property
    def option_keys(self) -> list[str]:
        return list(self.criteria)


class ScoreQuestion(BaseModel):
    """An ordered scale, listed lowest to highest."""

    type: Literal[QuestionType.SCORE] = QuestionType.SCORE
    instructions: str
    criteria: list[str]

    def wire(self) -> dict[str, Any]:
        return {
            "type": "score",
            "instructions": self.instructions,
            "criteria": list(self.criteria),
        }

    @property
    def option_keys(self) -> list[str]:
        return list(self.criteria)


Question = NoulQuestion | ChoiceQuestion | ScoreQuestion


class DecisionRequest(BaseModel):
    """One state, many questions.

    Bundling related questions into a single request is the documented pattern: it
    halves the round trips and guarantees every answer saw identical context, which
    matters when one answer gates another.
    """

    state: str | dict[str, Any]
    questions: dict[str, Question]

    def wire(self, model: str) -> dict[str, Any]:
        return {
            "model": model,
            "state": self.state,
            "questions": {name: q.wire() for name, q in self.questions.items()},
        }


class NoulAnswer(BaseModel):
    type: Literal["noul"] = "noul"
    probability: float

    @property
    def is_true(self) -> bool:
        return self.probability >= 0.5


class ChoiceAnswer(BaseModel):
    type: Literal["choice"] = "choice"
    choice: str
    probabilities: dict[str, float] = Field(default_factory=dict)

    @property
    def confidence(self) -> float:
        if self.choice in self.probabilities:
            return self.probabilities[self.choice]
        return max(self.probabilities.values(), default=0.0)


class ScoreAnswer(BaseModel):
    type: Literal["score"] = "score"
    score: float
    probabilities: list[float] = Field(default_factory=list)
    levels: list[str] = Field(default_factory=list)

    @property
    def nearest_level(self) -> str:
        if not self.levels:
            return ""
        index = min(len(self.levels) - 1, max(0, round(self.score)))
        return self.levels[index]


Answer = NoulAnswer | ChoiceAnswer | ScoreAnswer


class DecisionResponse(BaseModel):
    """Parsed answers plus the provenance an auditor will ask for."""

    answers: dict[str, Answer]
    model: str = "unknown"
    provider: str = "unknown"
    usage: dict[str, Any] = Field(default_factory=dict)
    latency_ms: float = 0.0
    degraded: bool = False
    degraded_reason: str | None = None

    def choice(self, name: str) -> ChoiceAnswer:
        answer = self.answers.get(name)
        if not isinstance(answer, ChoiceAnswer):
            raise DecisionError(f"question {name!r} did not return a choice answer")
        return answer

    def noul(self, name: str, default: float = 0.0) -> float:
        answer = self.answers.get(name)
        if isinstance(answer, NoulAnswer):
            return answer.probability
        return default

    def score(self, name: str) -> ScoreAnswer | None:
        answer = self.answers.get(name)
        return answer if isinstance(answer, ScoreAnswer) else None


class DecisionClient(ABC):
    """Anything that can turn a :class:`DecisionRequest` into a response."""

    name: str = "decision-client"

    @abstractmethod
    def decide(self, request: DecisionRequest) -> DecisionResponse:
        """Score every question against the state."""

    def close(self) -> None:  # noqa: B027 - optional hook, not a required override
        """Release transport resources. Networked clients override this."""


_PROBABILITY_KEYS = ("probabilities", "probability", "option_probabilities", "scores")


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def _extract_mapping(raw: dict[str, Any]) -> dict[str, float]:
    for key in _PROBABILITY_KEYS:
        candidate = raw.get(key)
        if isinstance(candidate, dict):
            parsed = {k: _as_float(v) for k, v in candidate.items()}
            return {k: v for k, v in parsed.items() if v is not None}
    return {}


def _extract_sequence(raw: dict[str, Any]) -> list[float]:
    for key in _PROBABILITY_KEYS:
        candidate = raw.get(key)
        if isinstance(candidate, list):
            parsed = [_as_float(v) for v in candidate]
            if all(v is not None for v in parsed):
                return [v for v in parsed if v is not None]
    return []


def parse_decision_payload(
    payload: Any,
    request: DecisionRequest,
    *,
    provider: str = "unknown",
    latency_ms: float = 0.0,
) -> DecisionResponse:
    """Turn a raw decision-API body into a :class:`DecisionResponse`.

    Raises :class:`DecisionError` rather than returning a half-populated object: a
    router that silently treats a malformed response as "low confidence" would route
    every request to a human during an outage without anyone noticing why.
    """
    if not isinstance(payload, dict):
        raise DecisionError("decision API returned a non-object body")
    raw_answers = payload.get("answers")
    if not isinstance(raw_answers, dict):
        raise DecisionError("decision API returned no answers object")

    answers: dict[str, Answer] = {}
    for name, question in request.questions.items():
        raw = raw_answers.get(name)
        if not isinstance(raw, dict):
            raise DecisionError(f"decision API returned no answer for question {name!r}")
        answers[name] = _parse_one(name, question, raw)

    return DecisionResponse(
        answers=answers,
        model=str(payload.get("model", "unknown")),
        provider=provider,
        usage=payload.get("usage") if isinstance(payload.get("usage"), dict) else {},
        latency_ms=latency_ms,
    )


def _parse_one(name: str, question: Question, raw: dict[str, Any]) -> Answer:
    if isinstance(question, ChoiceQuestion):
        probabilities = _extract_mapping(raw)
        choice = raw.get("choice") or raw.get("selected") or raw.get("answer")
        if not isinstance(choice, str) and probabilities:
            choice = max(probabilities, key=lambda k: probabilities[k])
        if not isinstance(choice, str) or choice not in question.criteria:
            raise DecisionError(
                f"question {name!r} returned unsupported option {choice!r}; "
                f"expected one of {sorted(question.criteria)}"
            )
        if not probabilities:
            probabilities = {opt: (1.0 if opt == choice else 0.0) for opt in question.criteria}
        return ChoiceAnswer(choice=choice, probabilities=normalise(probabilities))

    if isinstance(question, NoulQuestion):
        value = _as_float(raw.get("probability"))
        if value is None:
            value = _as_float(raw.get("value"))
        if value is None:
            mapping = _extract_mapping(raw)
            value = mapping.get("true")
        if value is None:
            raise DecisionError(f"question {name!r} returned no probability")
        return NoulAnswer(probability=min(1.0, max(0.0, value)))

    probabilities = _extract_sequence(raw)
    score = _as_float(raw.get("score"))
    if score is None and probabilities:
        score = sum(index * p for index, p in enumerate(probabilities))
    if score is None:
        raise DecisionError(f"question {name!r} returned no score")
    return ScoreAnswer(score=score, probabilities=probabilities, levels=list(question.criteria))


def normalise(values: dict[str, float]) -> dict[str, float]:
    """Clamp to non-negative and rescale to sum to one."""
    clamped = {k: max(0.0, v) for k, v in values.items()}
    total = sum(clamped.values())
    if total <= 0:
        count = len(clamped) or 1
        return dict.fromkeys(clamped, 1.0 / count)
    return {k: v / total for k, v in clamped.items()}


def softmax(logits: dict[str, float], temperature: float = 1.0) -> dict[str, float]:
    """Numerically stable softmax over a named set of logits."""
    if not logits:
        return {}
    temperature = max(1e-6, temperature)
    peak = max(logits.values())
    exponentials = {k: math.exp((v - peak) / temperature) for k, v in logits.items()}
    total = sum(exponentials.values())
    return {k: v / total for k, v in exponentials.items()}
