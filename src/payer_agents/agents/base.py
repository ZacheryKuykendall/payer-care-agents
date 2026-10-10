"""Shared agent scaffolding.

An agent gets a context and returns an answer. It does not decide whether the answer
is shown, whether a human is involved, or what is logged -- the orchestrator owns all
three. Keeping that authority out of the agents is what makes "add a sixth agent"
a small change instead of a governance review.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field

from payer_agents.agents.generation import Composer
from payer_agents.config import Settings
from payer_agents.domain import AgentAnswer, MemberRequest, RoutingDecision
from payer_agents.retrieval.types import SearchIndex
from payer_agents.tools.mcp_client import McpClient

NO_MEDICAL_ADVICE = (
    "This is plan coverage information, not medical advice. For questions about your "
    "care, talk with your provider."
)


class AgentContext(BaseModel):
    """Everything an agent is allowed to see."""

    request: MemberRequest
    redacted_text: str
    routing: RoutingDecision
    indexes: dict[str, SearchIndex]
    mcp: McpClient | None = None
    composer: Composer
    settings: Settings
    member_id: str | None = None

    model_config = {"arbitrary_types_allowed": True}

    def index(self, name: str) -> SearchIndex | None:
        return self.indexes.get(name)


class SpecialistAgent(ABC):
    """One intent, one agent."""

    name: str = "agent"
    #: Indexes and tools this agent is permitted to touch. Declared, not discovered,
    #: so a reviewer can see an agent's blast radius without reading its body.
    uses_indexes: list[str] = []
    uses_tools: list[str] = []

    @abstractmethod
    def handle(self, context: AgentContext) -> AgentAnswer: ...

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "indexes": list(self.uses_indexes),
            "tools": list(self.uses_tools),
        }


class RetrievalMixin:
    """Search helper shared by the retrieval-backed agents."""

    def retrieve(
        self,
        context: AgentContext,
        index_name: str,
        query: str,
        filters: dict[str, Any] | None = None,
    ) -> list:
        index = context.index(index_name)
        if index is None:
            return []
        return index.search(query, top_k=context.settings.search.top_k, filters=filters)


class ToolFailure(BaseModel):
    """A tool call that did not return usable data, carried back to the orchestrator."""

    tool: str
    error: str
    notes: list[str] = Field(default_factory=list)


def unanswered(agent: str, message: str, tool_calls: list | None = None) -> AgentAnswer:
    """Uniform "I could not answer this" result that trips the handoff path."""
    return AgentAnswer(
        agent=agent,
        text=message,
        grounded=False,
        grounding_score=0.0,
        tool_calls=tool_calls or [],
        notes=["agent could not produce a grounded answer"],
    )
