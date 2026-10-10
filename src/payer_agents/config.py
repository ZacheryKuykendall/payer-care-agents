"""Runtime configuration.

The guiding rule: ``RuntimeMode.MOCK`` must never read a credential, open a socket to
Azure, or fail because an environment variable is missing. Every Azure setting is
optional and only validated when ``RuntimeMode.AZURE`` is selected.
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent.parent
DATA_ROOT = REPO_ROOT / "data"


class RuntimeMode(str, Enum):
    """Which set of adapters the composition root wires up."""

    MOCK = "mock"
    AZURE = "azure"


class MissingConfigError(RuntimeError):
    """Raised when Azure mode is requested without the settings it needs."""


class FoundrySettings(BaseModel):
    """Microsoft Foundry endpoints and deployment names.

    ``decision_deployment`` is the *deployment* name, which is what the decision API
    expects in the request ``model`` field -- not the catalog model name.
    """

    endpoint: str | None = None
    decision_deployment: str = "decision-router"
    chat_deployment: str = "gpt-4.1-mini"
    embedding_deployment: str = "text-embedding-3-small"
    api_key: str | None = Field(default=None, repr=False)
    timeout_seconds: float = 30.0


class SearchSettings(BaseModel):
    """Azure AI Search connection details."""

    endpoint: str | None = None
    api_key: str | None = Field(default=None, repr=False)
    benefits_index: str = "plan-benefits"
    policy_index: str = "medical-policy"
    top_k: int = 4


class GatewaySettings(BaseModel):
    """Where the MCP tool server lives and how we authenticate to it.

    In Azure this is the API Management MCP endpoint, not the FHIR server itself:
    agents never hold a FHIR credential.
    """

    mcp_endpoint: str | None = None
    subscription_key: str | None = Field(default=None, repr=False)
    agent_id: str = "payer-agents-local"
    rate_limit_calls: int = 30
    rate_limit_window_seconds: int = 60


class SafetySettings(BaseModel):
    """Human-in-the-loop and PHI-boundary knobs.

    Thresholds are deliberately per-intent: the cost of an unnecessary human review is
    a few minutes of a nurse's time, while the cost of a wrong prior-authorization
    answer lands on a member. See ``docs/EVALUATION.md``.
    """

    default_route_threshold: float = 0.70
    route_thresholds: dict[str, float] = Field(
        default_factory=lambda: {
            "benefits_coverage": 0.65,
            "clinical_policy": 0.75,
            "prior_auth_status": 0.80,
            "care_escalation": 0.50,
        }
    )
    injection_threshold: float = 0.50
    clinician_required_threshold: float = 0.50
    urgency_escalation_level: float = 2.0
    grounding_threshold: float = 1.5
    redact_before_model_call: bool = True
    egress_policy_path: Path = DATA_ROOT / "policy" / "egress-policy.json"


class Settings(BaseModel):
    """Top-level settings object handed to :func:`payer_agents.orchestrator.build`."""

    mode: RuntimeMode = RuntimeMode.MOCK
    foundry: FoundrySettings = Field(default_factory=FoundrySettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    gateway: GatewaySettings = Field(default_factory=GatewaySettings)
    safety: SafetySettings = Field(default_factory=SafetySettings)
    data_root: Path = DATA_ROOT
    audit_sink_path: Path | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        """Build settings from environment variables, defaulting to mock mode."""
        src = os.environ if env is None else env
        mode = RuntimeMode(src.get("PAYER_AGENTS_MODE", "mock").strip().lower())
        audit_path = src.get("PAYER_AGENTS_AUDIT_LOG")
        settings = cls(
            mode=mode,
            foundry=FoundrySettings(
                endpoint=_clean(src.get("FOUNDRY_PROJECT_ENDPOINT")),
                decision_deployment=src.get("FOUNDRY_DECISION_DEPLOYMENT", "decision-router"),
                chat_deployment=src.get("FOUNDRY_CHAT_DEPLOYMENT", "gpt-4.1-mini"),
                embedding_deployment=src.get(
                    "FOUNDRY_EMBEDDING_DEPLOYMENT", "text-embedding-3-small"
                ),
                api_key=_clean(src.get("FOUNDRY_API_KEY")),
            ),
            search=SearchSettings(
                endpoint=_clean(src.get("AZURE_SEARCH_ENDPOINT")),
                api_key=_clean(src.get("AZURE_SEARCH_API_KEY")),
                benefits_index=src.get("AZURE_SEARCH_BENEFITS_INDEX", "plan-benefits"),
                policy_index=src.get("AZURE_SEARCH_POLICY_INDEX", "medical-policy"),
            ),
            gateway=GatewaySettings(
                mcp_endpoint=_clean(src.get("MCP_GATEWAY_ENDPOINT")),
                subscription_key=_clean(src.get("MCP_GATEWAY_SUBSCRIPTION_KEY")),
                agent_id=src.get("MCP_AGENT_ID", "payer-agents-local"),
            ),
            audit_sink_path=Path(audit_path) if audit_path else None,
        )
        if mode is RuntimeMode.AZURE:
            settings.require_azure()
        return settings

    def require_azure(self) -> None:
        """Fail loudly and early rather than halfway through a member's request."""
        missing = [
            name
            for name, value in (
                ("FOUNDRY_PROJECT_ENDPOINT", self.foundry.endpoint),
                ("AZURE_SEARCH_ENDPOINT", self.search.endpoint),
                ("MCP_GATEWAY_ENDPOINT", self.gateway.mcp_endpoint),
            )
            if not value
        ]
        if missing:
            raise MissingConfigError(
                "Azure mode requires: " + ", ".join(missing) + ". Run in mock mode instead "
                "with PAYER_AGENTS_MODE=mock."
            )

    def threshold_for(self, intent: str) -> float:
        """Routing confidence required before the orchestrator acts without a human."""
        return self.safety.route_thresholds.get(intent, self.safety.default_route_threshold)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
