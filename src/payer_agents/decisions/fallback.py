"""Keep routing alive when the decision model is not.

A classifier in front of every member request is a single point of failure. The
mitigation is not "retry harder" -- it is to degrade to a worse classifier that is
honest about being worse, so the human-review gate catches what it now misses.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from payer_agents.decisions.base import (
    DecisionClient,
    DecisionError,
    DecisionRequest,
    DecisionResponse,
)


class CircuitBreaker:
    """Open after ``threshold`` consecutive failures; probe again after ``cooldown``."""

    def __init__(
        self,
        threshold: int = 3,
        cooldown_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.threshold = max(1, threshold)
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if self._clock() - self.opened_at >= self.cooldown_seconds:
            # Half-open: let exactly one request through to test the water.
            self.opened_at = None
            self.failures = self.threshold - 1
            return False
        return True

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = self._clock()


class ResilientDecisionClient(DecisionClient):
    """Primary client with a circuit breaker and a pluggable fallback."""

    name = "resilient-decision"

    def __init__(
        self,
        primary: DecisionClient,
        fallback: DecisionClient,
        breaker: CircuitBreaker | None = None,
        on_degrade: Callable[[str], None] | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.breaker = breaker or CircuitBreaker()
        self._on_degrade = on_degrade

    def decide(self, request: DecisionRequest) -> DecisionResponse:
        if self.breaker.is_open:
            return self._degrade(request, "circuit breaker open")
        try:
            response = self.primary.decide(request)
        except DecisionError as exc:
            self.breaker.record_failure()
            return self._degrade(request, str(exc))
        self.breaker.record_success()
        return response

    def _degrade(self, request: DecisionRequest, reason: str) -> DecisionResponse:
        if self._on_degrade is not None:
            self._on_degrade(reason)
        response = self.fallback.decide(request)
        response.degraded = True
        response.degraded_reason = reason
        return response

    def close(self) -> None:
        self.primary.close()
        self.fallback.close()
