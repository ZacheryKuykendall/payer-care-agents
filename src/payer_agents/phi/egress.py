"""Outbound network allow/deny controls for agent traffic.

The policy document in ``data/policy/egress-policy.json`` is the single source of
truth for two enforcement points:

* **Locally** -- :class:`GuardedTransport` evaluates it before any HTTP request
  leaves the process, so the demo and the tests exercise the same rules.
* **In Azure** -- :meth:`EgressPolicy.to_rai_properties` emits the
  ``properties.egressPolicy`` block of a Cognitive Services Responsible AI policy,
  which is how Microsoft Foundry applies network egress controls to a hosted agent.

The structure deliberately mirrors the Azure resource: ordered rules, first match
wins, ``Fqdn`` matching on host with wildcard support, a default action, and an
``Audit``/``Enforced`` mode switch. Deviating from that shape would mean the thing
tested locally is not the thing deployed.

PREVIEW: network egress controls for Foundry hosted agents are in public preview and
Microsoft states they are not intended for production and carry no preview SLA. See
``docs/PREVIEW-STATUS.md``.
"""

from __future__ import annotations

import fnmatch
import json
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

#: Azure caps an egress policy at 480 rules across all actions.
MAX_RULES = 480


class EgressMode(str, Enum):
    AUDIT = "Audit"
    ENFORCED = "Enforced"


class EgressAction(str, Enum):
    ALLOW = "Allow"
    DENY = "Deny"
    #: Transform and Rewrite exist upstream but run in Audit only during preview, so
    #: this simulator treats them as "allowed and recorded" and says so in the trace.
    TRANSFORM = "Transform"
    REWRITE = "Rewrite"


class EgressRule(BaseModel):
    name: str
    host: str
    action: EgressAction = EgressAction.ALLOW
    rule_type: str = "Fqdn"
    description: str | None = None

    def matches(self, host: str) -> bool:
        """Exact host or wildcard (``*.contoso.com``) match, case-insensitive."""
        return fnmatch.fnmatch(host.lower(), self.host.lower())

    def to_arm(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ruleType": self.rule_type,
            "match": {"host": self.host},
            "action": {"actionType": self.action.value},
        }


class EgressDecision(BaseModel):
    """One evaluation, shaped for the audit log."""

    url: str
    host: str
    action: EgressAction
    rule_name: str
    mode: EgressMode
    allowed: bool
    reason: str = ""

    @property
    def would_deny(self) -> bool:
        """Audit mode records what *would* have been blocked without blocking it."""
        return self.action is EgressAction.DENY


class EgressBlocked(RuntimeError):
    """Raised when an enforced policy denies an outbound request."""

    def __init__(self, decision: EgressDecision) -> None:
        super().__init__(
            f"egress denied for {decision.host} by rule {decision.rule_name!r} "
            f"({decision.reason})"
        )
        self.decision = decision


class EgressPolicy(BaseModel):
    """Ordered FQDN rules with a default action."""

    name: str = "payer-agents-egress"
    mode: EgressMode = EgressMode.ENFORCED
    default_action: EgressAction = EgressAction.DENY
    rules: list[EgressRule] = Field(default_factory=list)
    base_policy_name: str = "Microsoft.DefaultV2"
    content_safety_mode: str = "Blocking"
    notes: list[str] = Field(default_factory=list)

    @field_validator("rules")
    @classmethod
    def _within_limit(cls, rules: list[EgressRule]) -> list[EgressRule]:
        if len(rules) > MAX_RULES:
            raise ValueError(f"an egress policy supports at most {MAX_RULES} rules")
        return rules

    def evaluate(self, url: str) -> EgressDecision:
        """First match wins. Anything unparsable is denied -- evaluation fails closed."""
        host = _host_of(url)
        if not host:
            return EgressDecision(
                url=url,
                host="",
                action=EgressAction.DENY,
                rule_name="<unparsable>",
                mode=self.mode,
                allowed=False,
                reason="destination host could not be parsed; policy fails closed",
            )

        for rule in self.rules:
            if rule.matches(host):
                denied = rule.action is EgressAction.DENY
                return EgressDecision(
                    url=url,
                    host=host,
                    action=rule.action,
                    rule_name=rule.name,
                    mode=self.mode,
                    allowed=not (denied and self.mode is EgressMode.ENFORCED),
                    reason=rule.description or f"matched rule {rule.name}",
                )

        denied = self.default_action is EgressAction.DENY
        return EgressDecision(
            url=url,
            host=host,
            action=self.default_action,
            rule_name="<default>",
            mode=self.mode,
            allowed=not (denied and self.mode is EgressMode.ENFORCED),
            reason=f"no rule matched; default action is {self.default_action.value}",
        )

    def to_arm_properties(self) -> dict[str, Any]:
        """The ``properties`` block of a Cognitive Services RAI policy.

        ``properties.mode`` is the content-safety setting; ``properties.egressPolicy.mode``
        is the network one. They are two different switches on one resource and
        conflating them is the most common way to deploy an egress policy that does
        nothing.
        """
        return {
            "basePolicyName": self.base_policy_name,
            "mode": self.content_safety_mode,
            "egressPolicy": {
                "mode": self.mode.value,
                "defaultAction": self.default_action.value,
                "rules": [rule.to_arm() for rule in self.rules],
            },
        }

    @classmethod
    def from_file(cls, path: Path) -> EgressPolicy:
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def load_egress_policy(path: Path | None = None) -> EgressPolicy:
    """Load the policy, falling back to a deny-all policy if the file is missing.

    Failing closed on a missing policy file is the only safe default: a deployment
    that forgot to ship the policy should lose outbound access, not gain it.
    """
    if path is None or not Path(path).exists():
        return EgressPolicy(
            name="fallback-deny-all",
            mode=EgressMode.ENFORCED,
            default_action=EgressAction.DENY,
            rules=[],
            notes=["policy file not found; failing closed with a deny-all policy"],
        )
    return EgressPolicy.from_file(Path(path))


class GuardedTransport:
    """Wraps an HTTP client so every request is evaluated against the policy first.

    ``inner`` is anything with ``get(url, params=..., headers=...) -> response``. The
    FHIR client only needs GET, which keeps the blast radius of this shim small.
    """

    def __init__(
        self,
        base_url: str,
        policy: EgressPolicy,
        inner: Any | None = None,
        *,
        headers: dict[str, str] | None = None,
        on_decision: Any | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.policy = policy
        self.headers = headers or {}
        self._on_decision = on_decision
        self._decisions: list[EgressDecision] = []
        self._owns_client = inner is None
        if inner is None:
            import httpx

            inner = httpx.Client(timeout=15.0)
        self._inner = inner

    @property
    def decisions(self) -> list[EgressDecision]:
        """Every evaluation this transport made, in order. Audit mode needs the
        would-deny records just as much as enforced mode needs the blocks."""
        return list(self._decisions)

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        decision = self.policy.evaluate(url)
        self._decisions.append(decision)
        if self._on_decision is not None:
            self._on_decision(decision)
        if not decision.allowed:
            raise EgressBlocked(decision)
        response = self._inner.get(url, params=params or {}, headers=self.headers)
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        if self._owns_client and hasattr(self._inner, "close"):
            self._inner.close()


def _host_of(url: str) -> str:
    parsed = urlparse(url if "//" in url else f"//{url}")
    return (parsed.hostname or "").lower()
