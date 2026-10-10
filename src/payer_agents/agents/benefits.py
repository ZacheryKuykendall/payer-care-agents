"""Benefits and cost-share agent.

The job this agent does that a plain RAG pipeline does not: it resolves the member's
plan through a tool call *before* retrieving, and filters retrieval to that plan.
Quoting another plan's copay back to a member is the most common way a payer
assistant produces a confidently wrong answer, and a metadata filter is a cheaper fix
than a smarter model.
"""

from __future__ import annotations

from payer_agents.agents.base import (
    NO_MEDICAL_ADVICE,
    AgentContext,
    RetrievalMixin,
    SpecialistAgent,
    unanswered,
)
from payer_agents.domain import AgentAnswer, ToolCall


class BenefitsAgent(SpecialistAgent, RetrievalMixin):
    name = "benefits"
    uses_indexes = ["benefits"]
    uses_tools = ["find_member_coverage"]

    def handle(self, context: AgentContext) -> AgentAnswer:
        tool_calls: list[ToolCall] = []
        plan_id: str | None = None
        plan_name: str | None = None
        notes: list[str] = []

        if context.member_id and context.mcp is not None:
            call = context.mcp.call_tool(
                "find_member_coverage", {"member_id": context.member_id}
            )
            tool_calls.append(call)
            if call.ok:
                coverage = call.payload if isinstance(call.payload, dict) else {}
                if coverage.get("found"):
                    plan_id = coverage.get("plan_id")
                    plan_name = coverage.get("plan_name")
                else:
                    notes.append("coverage lookup returned no active plan for this member")
            else:
                notes.append(f"coverage lookup failed: {call.error}")

        query = context.redacted_text
        filters = {"plan_id": plan_id} if plan_id else None
        hits = self.retrieve(context, "benefits", query, filters)

        if not hits:
            return unanswered(
                self.name,
                "I could not find plan language that answers this question.",
                tool_calls,
            )

        if plan_name:
            lead_in = (
                f"Here is what your plan documents say for {plan_name} "
                f"({plan_id}):"
            )
        else:
            lead_in = (
                "I do not have your plan on file for this conversation, so this is "
                "general plan language rather than your specific cost share:"
            )
            notes.append("answered without a verified plan filter")

        composed = context.composer.compose(
            lead_in=lead_in, query=query, hits=hits, closing=NO_MEDICAL_ADVICE
        )
        return AgentAnswer(
            agent=self.name,
            text=composed.text,
            citations=composed.citations,
            tool_calls=tool_calls,
            notes=notes,
        )
