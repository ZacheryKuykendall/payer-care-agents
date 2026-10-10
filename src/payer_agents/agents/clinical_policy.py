"""Medical policy and coverage criteria agent.

This agent answers "what does the plan require?" and never "does this member
qualify?". The distinction is load-bearing: under the fictional UM-001 program rules
in the corpus -- which mirror how real utilization management works -- only a
licensed clinical reviewer may issue a medical necessity denial. So the agent reads
criteria out of policy documents and stops there.
"""

from __future__ import annotations

from payer_agents.agents.base import (
    AgentContext,
    RetrievalMixin,
    SpecialistAgent,
    unanswered,
)
from payer_agents.domain import AgentAnswer

CLOSING = (
    "These are the plan's published criteria. Whether a specific request meets them "
    "is decided by a clinical reviewer, not by this assistant."
)


class ClinicalPolicyAgent(SpecialistAgent, RetrievalMixin):
    name = "clinical_policy"
    uses_indexes = ["policy"]
    uses_tools = []

    def handle(self, context: AgentContext) -> AgentAnswer:
        query = context.redacted_text
        hits = self.retrieve(context, "policy", query)
        if not hits:
            return unanswered(
                self.name,
                "I could not find a medical policy that covers this question.",
            )

        policy_titles = sorted({hit.chunk.title for hit in hits[:3]})
        lead_in = (
            "Here are the published criteria from the plan's medical policy"
            + (f" ({policy_titles[0]})" if len(policy_titles) == 1 else "")
            + ":"
        )
        composed = context.composer.compose(
            lead_in=lead_in, query=query, hits=hits, closing=CLOSING
        )
        return AgentAnswer(
            agent=self.name,
            text=composed.text,
            citations=composed.citations,
            notes=[f"policies consulted: {', '.join(policy_titles)}"],
        )
