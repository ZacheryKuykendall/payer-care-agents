"""A local simulation of the API Management policy chain in front of the MCP server.

The authoritative policies are the XML in ``infra/apim/policies``. This module
re-implements the same chain in Python for one reason: so the controls are *executed*
in the demo and *asserted* in the test suite, rather than existing only as XML nobody
runs. Each check below names the APIM policy element it stands in for, and the
tests in ``tests/test_gateway.py`` are the parity check.

Chain order matches the XML, because order is the control:

1. ``validate-azure-ad-token`` - identify the calling agent
2. ``check-header`` - require a correlation identifier for the audit trail
3. tool allow-list - which tools this identity may call at all
4. ``rate-limit-by-key`` - per-agent quota, not per-IP, so one noisy agent cannot
   consume another's budget
5. ``trace`` / ``log-to-eventhub`` - emit the audit record either way
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from payer_agents.phi.audit import AuditLog


class CallContext(BaseModel):
    """Who is calling and under what authority."""

    agent_id: str = "payer-agents-local"
    subscription_key: str | None = None
    bearer_token: str | None = None
    correlation_id: str | None = None
    purpose_of_use: str = "TREAT"
    member_scope: str | None = None


class GatewayPolicy(BaseModel):
    """The knobs the APIM policy XML exposes, expressed as data."""

    name: str = "mcp-fhir-tools"
    #: Empty means "every registered tool". Tool-level allow/block mirrors the
    #: granular control available for MCP servers registered in an AI Gateway tier.
    allowed_tools: list[str] = Field(default_factory=list)
    denied_tools: list[str] = Field(default_factory=lambda: ["submit_prior_authorization"])
    require_credential: bool = True
    require_correlation_id: bool = True
    rate_limit_calls: int = 30
    rate_limit_window_seconds: int = 60
    max_records_per_response: int = 50

    def permits(self, tool_name: str) -> bool:
        if tool_name in self.denied_tools:
            return False
        return not self.allowed_tools or tool_name in self.allowed_tools


class GatewayDecision(BaseModel):
    """Allow or reject, with the HTTP status API Management would have returned."""

    allowed: bool
    status_code: int = 200
    policy: str = "allow"
    reason: str = ""
    remaining_calls: int | None = None

    @property
    def summary(self) -> str:
        return "allow" if self.allowed else f"{self.policy} ({self.status_code})"


class RateLimiter:
    """Sliding-window counter keyed per agent, mirroring ``rate-limit-by-key``."""

    def __init__(
        self, calls: int, window_seconds: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.calls = calls
        self.window_seconds = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def check(self, key: str) -> tuple[bool, int]:
        """Record an attempt. Returns ``(allowed, remaining)``."""
        now = self._clock()
        bucket = self._hits.setdefault(key, deque())
        while bucket and now - bucket[0] >= self.window_seconds:
            bucket.popleft()
        if len(bucket) >= self.calls:
            return False, 0
        bucket.append(now)
        return True, self.calls - len(bucket)

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._hits.clear()
        else:
            self._hits.pop(key, None)


class ApiManagementSimulator:
    """Runs the policy chain and writes the audit record for every tool call."""

    def __init__(
        self,
        policy: GatewayPolicy | None = None,
        audit: AuditLog | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.policy = policy or GatewayPolicy()
        self.audit = audit
        self.limiter = RateLimiter(
            self.policy.rate_limit_calls, self.policy.rate_limit_window_seconds, clock
        )

    def authorize(self, tool_name: str, context: CallContext) -> GatewayDecision:
        """Evaluate the chain. Nothing here touches tool arguments or PHI."""
        if self.policy.require_credential and not (
            context.subscription_key or context.bearer_token
        ):
            return GatewayDecision(
                allowed=False,
                status_code=401,
                policy="validate-azure-ad-token",
                reason="no subscription key or bearer token presented",
            )

        if self.policy.require_correlation_id and not context.correlation_id:
            return GatewayDecision(
                allowed=False,
                status_code=400,
                policy="check-header",
                reason="x-correlation-id header is required for auditability",
            )

        if not self.policy.permits(tool_name):
            return GatewayDecision(
                allowed=False,
                status_code=403,
                policy="tool-allow-list",
                reason=f"agent {context.agent_id!r} is not authorised for tool {tool_name!r}",
            )

        allowed, remaining = self.limiter.check(context.agent_id)
        if not allowed:
            return GatewayDecision(
                allowed=False,
                status_code=429,
                policy="rate-limit-by-key",
                reason=f"exceeded {self.policy.rate_limit_calls} calls per "
                f"{self.policy.rate_limit_window_seconds}s for "
                f"{context.agent_id!r}",
            )

        return GatewayDecision(
            allowed=True, policy="allow", reason="policy chain passed", remaining_calls=remaining
        )

    def log(
        self,
        tool_name: str,
        context: CallContext,
        decision: GatewayDecision,
        *,
        outcome: str = "ok",
        request_id: str | None = None,
        **detail: Any,
    ) -> None:
        """Emit the per-tool-call audit record the APIM policy would send onward."""
        if self.audit is None:
            return
        self.audit.record(
            "tool_gateway",
            tool_name,
            outcome=outcome,
            request_id=request_id,
            actor=context.agent_id,
            subject_token=context.member_scope,
            gateway_policy=decision.policy,
            gateway_status=decision.status_code,
            gateway_reason=decision.reason,
            purpose_of_use=context.purpose_of_use,
            correlation_id=context.correlation_id,
            **detail,
        )
