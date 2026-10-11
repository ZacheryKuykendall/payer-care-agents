"""Shared fixtures.

Everything is built in mock mode, which means the whole suite runs with no
credentials, no network and no Azure SDK installed. That is a property worth
protecting: a test suite that needs a subscription is a test suite nobody runs.
"""

from __future__ import annotations

import pytest

from payer_agents.config import DATA_ROOT, Settings
from payer_agents.decisions.mock import MockDecisionClient
from payer_agents.evals.judge import LexicalJudgeClient
from payer_agents.fhir.client import LocalFhirClient
from payer_agents.fhir.store import FhirStore
from payer_agents.gateway.apim_sim import ApiManagementSimulator, CallContext, GatewayPolicy
from payer_agents.orchestrator import build
from payer_agents.phi.audit import AuditLog, MemoryAuditSink
from payer_agents.retrieval.corpus import build_indexes, load_corpus
from payer_agents.tools.fhir_tools import build_fhir_registry
from payer_agents.tools.mcp_client import InProcessMcpClient


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings()


@pytest.fixture(scope="session")
def documents():
    return load_corpus()


@pytest.fixture(scope="session")
def indexes(documents):
    return build_indexes(documents)


@pytest.fixture(scope="session")
def fhir_store() -> FhirStore:
    return FhirStore.from_directory(DATA_ROOT / "fhir" / "bundles")


@pytest.fixture
def fhir_client(fhir_store) -> LocalFhirClient:
    return LocalFhirClient(fhir_store)


@pytest.fixture
def registry(fhir_client):
    return build_fhir_registry(fhir_client)


@pytest.fixture
def audit() -> AuditLog:
    return AuditLog([MemoryAuditSink()])


@pytest.fixture
def gateway(audit) -> ApiManagementSimulator:
    return ApiManagementSimulator(GatewayPolicy(), audit=audit)


@pytest.fixture
def mcp(registry, gateway, audit) -> InProcessMcpClient:
    return InProcessMcpClient(
        registry,
        gateway,
        CallContext(agent_id="test-agent", subscription_key="test-key"),
        audit=audit,
    )


@pytest.fixture
def decision_client() -> MockDecisionClient:
    return MockDecisionClient()


@pytest.fixture
def judge_client() -> LexicalJudgeClient:
    return LexicalJudgeClient()


@pytest.fixture(scope="session")
def orchestrator():
    """One orchestrator for the whole session.

    Shared deliberately: the audit log accumulates across tests, which is what lets
    ``test_audit.py`` assert that a long-running chain stays verifiable.
    """
    return build(Settings())


@pytest.fixture
def fresh_orchestrator():
    return build(Settings())
