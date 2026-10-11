"""Evaluation harness.

Three questions, answered from labelled synthetic cases:

1. **Does routing pick the right specialist?** Accuracy and a confusion matrix.
2. **Is the confidence trustworthy?** Expected calibration error, Brier score, and a
   threshold sweep showing what each operating point buys and costs.
3. **Are the answers grounded?** The judge's score distribution over cases the system
   chose to answer.

What this harness deliberately does not produce is a benchmark number for
``Microsoft-Decision-1``. In mock mode it measures a bag-of-phrases classifier
against cases written alongside its own lexicon, which is close to circular. Its
value is that the *method* is in the repository and runs on every commit, so pointing
it at a real deployment is a configuration change rather than a project.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from payer_agents.config import DATA_ROOT
from payer_agents.decisions.calibration import (
    CalibrationReport,
    ThresholdPoint,
    calibration_report,
    threshold_sweep,
)
from payer_agents.domain import Channel, Intent, MemberRequest


class EvalCase(BaseModel):
    """One labelled example."""

    id: str
    text: str
    expected_intent: Intent
    channel: Channel = Channel.MEMBER_PORTAL
    member_id: str | None = None
    #: ``True`` when a correct system defers to a human regardless of routing.
    expect_human: bool = False
    note: str = ""


class CaseOutcome(BaseModel):
    id: str
    expected: str
    predicted: str
    confidence: float
    correct: bool
    automated: bool
    expect_human: bool
    human_decision_correct: bool
    grounding_score: float | None = None
    handoff_reason: str | None = None


class EvalReport(BaseModel):
    """Everything the harness measured, ready to print or render."""

    sample_size: int
    routing_accuracy: float
    abstention_rate: float
    automation_rate: float
    human_routing_accuracy: float
    mean_grounding_score: float | None = None
    calibration: CalibrationReport
    threshold_points: list[ThresholdPoint] = Field(default_factory=list)
    confusion: dict[str, dict[str, int]] = Field(default_factory=dict)
    outcomes: list[CaseOutcome] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def summary_lines(self) -> list[str]:
        lines = [
            f"cases                 {self.sample_size}",
            f"routing accuracy      {self.routing_accuracy:.1%}",
            f"abstention rate       {self.abstention_rate:.1%}",
            f"automation rate       {self.automation_rate:.1%}",
            f"human-referral recall {self.human_routing_accuracy:.1%}",
        ]
        if self.mean_grounding_score is not None:
            lines.append(f"mean grounding score  {self.mean_grounding_score:.2f} / 3.00")
        lines.append(f"calibration           {self.calibration.summary()}")
        return lines


def load_cases(path: Path | None = None) -> list[EvalCase]:
    """Read the labelled case file."""
    source = Path(path) if path is not None else DATA_ROOT / "evals" / "cases.yaml"
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    return [EvalCase.model_validate(item) for item in raw.get("cases", [])]


def run_suite(orchestrator: Any, cases: list[EvalCase] | None = None) -> EvalReport:
    """Run every case end to end and summarise."""
    cases = cases if cases is not None else load_cases()
    outcomes: list[CaseOutcome] = []
    confusion: dict[str, dict[str, int]] = {}
    grounding_scores: list[float] = []

    for case in cases:
        result = orchestrator.handle(
            MemberRequest(text=case.text, channel=case.channel, member_id=case.member_id)
        )
        predicted = result.routing.intent
        correct = predicted is case.expected_intent
        confusion.setdefault(case.expected_intent.value, Counter())[predicted.value] += 1

        if result.answer is not None:
            grounding_scores.append(result.answer.grounding_score)

        outcomes.append(
            CaseOutcome(
                id=case.id,
                expected=case.expected_intent.value,
                predicted=predicted.value,
                confidence=result.routing.confidence,
                correct=correct,
                automated=result.automated,
                expect_human=case.expect_human,
                human_decision_correct=(not result.automated) == case.expect_human,
                grounding_score=(
                    result.answer.grounding_score if result.answer is not None else None
                ),
                handoff_reason=(
                    result.handoff.reason.value if result.handoff is not None else None
                ),
            )
        )

    total = len(outcomes) or 1
    pairs = [(o.confidence, o.correct) for o in outcomes]

    return EvalReport(
        sample_size=len(outcomes),
        routing_accuracy=sum(1 for o in outcomes if o.correct) / total,
        abstention_rate=sum(1 for o in outcomes if o.predicted == Intent.CANNOT_TELL.value) / total,
        automation_rate=sum(1 for o in outcomes if o.automated) / total,
        human_routing_accuracy=sum(1 for o in outcomes if o.human_decision_correct) / total,
        mean_grounding_score=(
            sum(grounding_scores) / len(grounding_scores) if grounding_scores else None
        ),
        calibration=calibration_report(pairs),
        threshold_points=threshold_sweep(pairs, [i / 10 for i in range(11)]),
        confusion={k: dict(v) for k, v in confusion.items()},
        outcomes=outcomes,
        notes=[
            "Synthetic cases scored against the mode the orchestrator was built in.",
            "In mock mode these numbers describe a lexical classifier, not "
            "Microsoft-Decision-1. They are a harness check, not a benchmark.",
            "Re-run with PAYER_AGENTS_MODE=azure against a real deployment before "
            "using any threshold in production.",
        ],
    )
