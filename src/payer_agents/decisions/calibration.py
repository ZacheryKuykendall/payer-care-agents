"""Calibration measurement.

A routing threshold is a bet that "0.80 confidence" means "right about 80% of the
time". Microsoft publishes no calibration figures for ``Microsoft-Decision-1`` and
says to validate on your own labelled examples, so this module is the part of the
architecture that turns a threshold from a guess into a measurement.

Everything here is a pure function over ``(predicted_probability, was_correct)``
pairs, so it works the same against mock output, Foundry output, or a CSV someone
exported from a production trace.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from pydantic import BaseModel, Field


class ReliabilityBin(BaseModel):
    """One bucket of a reliability diagram."""

    lower: float
    upper: float
    count: int
    mean_confidence: float
    accuracy: float

    @property
    def gap(self) -> float:
        """Positive means overconfident in this bucket."""
        return self.mean_confidence - self.accuracy


class CalibrationReport(BaseModel):
    """Headline calibration numbers plus the bins they came from."""

    sample_size: int
    accuracy: float
    expected_calibration_error: float
    max_calibration_error: float
    brier_score: float
    mean_confidence: float
    bins: list[ReliabilityBin] = Field(default_factory=list)

    @property
    def overconfident(self) -> bool:
        return self.mean_confidence > self.accuracy

    def summary(self) -> str:
        direction = "overconfident" if self.overconfident else "underconfident"
        return (
            f"n={self.sample_size} accuracy={self.accuracy:.3f} "
            f"mean_confidence={self.mean_confidence:.3f} ({direction}) "
            f"ECE={self.expected_calibration_error:.3f} "
            f"MCE={self.max_calibration_error:.3f} Brier={self.brier_score:.3f}"
        )


def reliability_bins(
    outcomes: Sequence[tuple[float, bool]], bin_count: int = 10
) -> list[ReliabilityBin]:
    """Bucket ``(confidence, correct)`` pairs into equal-width confidence bins."""
    if bin_count < 1:
        raise ValueError("bin_count must be >= 1")
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bin_count)]
    for confidence, correct in outcomes:
        clamped = min(1.0, max(0.0, confidence))
        index = min(bin_count - 1, int(clamped * bin_count))
        buckets[index].append((clamped, correct))

    bins: list[ReliabilityBin] = []
    for index, bucket in enumerate(buckets):
        lower = index / bin_count
        upper = (index + 1) / bin_count
        if not bucket:
            bins.append(
                ReliabilityBin(
                    lower=lower, upper=upper, count=0, mean_confidence=0.0, accuracy=0.0
                )
            )
            continue
        bins.append(
            ReliabilityBin(
                lower=lower,
                upper=upper,
                count=len(bucket),
                mean_confidence=sum(c for c, _ in bucket) / len(bucket),
                accuracy=sum(1 for _, ok in bucket if ok) / len(bucket),
            )
        )
    return bins


def calibration_report(
    outcomes: Iterable[tuple[float, bool]], bin_count: int = 10
) -> CalibrationReport:
    """Expected/maximum calibration error and Brier score over labelled outcomes."""
    materialised = [(min(1.0, max(0.0, c)), bool(ok)) for c, ok in outcomes]
    total = len(materialised)
    if total == 0:
        return CalibrationReport(
            sample_size=0,
            accuracy=0.0,
            expected_calibration_error=0.0,
            max_calibration_error=0.0,
            brier_score=0.0,
            mean_confidence=0.0,
            bins=reliability_bins([], bin_count),
        )

    bins = reliability_bins(materialised, bin_count)
    ece = sum(b.count / total * abs(b.gap) for b in bins if b.count)
    mce = max((abs(b.gap) for b in bins if b.count), default=0.0)
    brier = sum((c - (1.0 if ok else 0.0)) ** 2 for c, ok in materialised) / total

    return CalibrationReport(
        sample_size=total,
        accuracy=sum(1 for _, ok in materialised if ok) / total,
        expected_calibration_error=ece,
        max_calibration_error=mce,
        brier_score=brier,
        mean_confidence=sum(c for c, _ in materialised) / total,
        bins=bins,
    )


class ThresholdPoint(BaseModel):
    """What happens to automation and error rates at one confidence threshold."""

    threshold: float
    automated: int
    deferred: int
    automated_errors: int

    @property
    def automation_rate(self) -> float:
        total = self.automated + self.deferred
        return self.automated / total if total else 0.0

    @property
    def automated_error_rate(self) -> float:
        return self.automated_errors / self.automated if self.automated else 0.0


def threshold_sweep(
    outcomes: Sequence[tuple[float, bool]], thresholds: Sequence[float] | None = None
) -> list[ThresholdPoint]:
    """Trade-off curve: how much automation each threshold buys and what it costs.

    This is the artefact to bring to a clinical governance committee. "Pick 0.8"
    is an opinion; "at 0.8 we automate 71% of traffic with a 2% error rate on the
    automated slice, and here is the curve" is a decision.
    """
    grid = thresholds if thresholds is not None else [i / 20 for i in range(21)]
    points: list[ThresholdPoint] = []
    for threshold in grid:
        automated = [(c, ok) for c, ok in outcomes if c >= threshold]
        deferred = len(outcomes) - len(automated)
        points.append(
            ThresholdPoint(
                threshold=threshold,
                automated=len(automated),
                deferred=deferred,
                automated_errors=sum(1 for _, ok in automated if not ok),
            )
        )
    return points
