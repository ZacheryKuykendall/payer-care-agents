"""Prior authorization status agent.

Structured facts come from FHIR through the governed tool layer; narrative context
(timeframes, what a status means, what happens next) comes from retrieval over the
UM policy documents. The split matters: a determination is a fact about a member and
must come from the system of record, while "what does 'pending information' mean"
is published policy and should be cited, not paraphrased from memory.

The agent reports a determination. It never makes, predicts, or appeals one.
"""

from __future__ import annotations

from payer_agents.agents.base import (
    AgentContext,
    RetrievalMixin,
    SpecialistAgent,
    unanswered,
)
from payer_agents.domain import AgentAnswer, ToolCall

NEEDS_MEMBER_ID = (
    "To look up an authorization I need the member ID from your plan card. I have "
    "connected you to a member services representative who can verify your identity "
    "and check it with you."
)

CLOSING = (
    "This is the status recorded in the plan's system. If you disagree with a "
    "determination, a representative can walk you through your appeal rights."
)


class PriorAuthAgent(SpecialistAgent, RetrievalMixin):
    name = "prior_auth"
    uses_indexes = ["policy"]
    uses_tools = ["find_member_coverage", "get_prior_authorization_status"]

    def handle(self, context: AgentContext) -> AgentAnswer:
        if not context.member_id or context.mcp is None:
            return unanswered(self.name, NEEDS_MEMBER_ID)

        tool_calls: list[ToolCall] = []
        call = context.mcp.call_tool(
            "get_prior_authorization_status", {"member_id": context.member_id}
        )
        tool_calls.append(call)

        if not call.ok:
            return unanswered(
                self.name,
                "I could not reach the authorization system just now, so I am "
                "connecting you with a representative who can check it directly.",
                tool_calls,
            )

        payload = call.payload if isinstance(call.payload, dict) else {}
        authorizations = payload.get("authorizations") or []
        if not authorizations:
            return AgentAnswer(
                agent=self.name,
                text=(
                    "I do not see any prior authorization requests on file for your "
                    "membership. If your provider submitted one recently, it can take "
                    "up to one business day to appear."
                ),
                tool_calls=tool_calls,
                grounded=True,
                notes=["no authorizations returned for member"],
            )

        facts = [_describe(auth) for auth in authorizations[:3]]
        statuses = sorted({a.get("status", "") for a in authorizations[:3]})

        # Retrieve on the statuses rather than the member's words. "What does 'pending
        # information' mean and what happens next" is the question behind every status
        # enquiry, and the member rarely phrases it that way. Filtering to the UM
        # documents keeps clinical policy out of a process answer.
        hits = self.retrieve(
            context,
            "policy",
            f"prior authorization status {' '.join(statuses)} what happens next "
            f"determination timeframe",
            {"doc_type": "utilization_management"},
        )

        composed = context.composer.compose(
            lead_in=(
                f"I found {len(authorizations)} authorization "
                f"{'request' if len(authorizations) == 1 else 'requests'} on your "
                f"membership:"
            ),
            query=f"{context.redacted_text} {' '.join(statuses)}",
            hits=hits,
            facts=facts,
            closing=CLOSING,
        )
        return AgentAnswer(
            agent=self.name,
            text=composed.text,
            citations=composed.citations,
            facts=facts,
            tool_calls=tool_calls,
            notes=[f"authorizations returned: {len(authorizations)}"],
        )


def _describe(auth: dict) -> str:
    """One line a member can read, built only from fields the system of record returned."""
    status = str(auth.get("status", "unknown"))
    parts = [
        f"{auth.get('reference_number', 'unknown reference')} - "
        f"{auth.get('service', 'requested service')}: {status}"
    ]
    if auth.get("valid_from") and auth.get("valid_through"):
        parts.append(f"valid {auth['valid_from']} through {auth['valid_through']}")
    disposition = str(auth.get("disposition", "")).strip()
    # The disposition usually opens by restating the status; repeating it reads as a
    # bug to the member even though both fields are correct.
    if disposition and not disposition.lower().startswith(status.lower()):
        parts.append(disposition)
    elif disposition:
        trimmed = disposition[len(status) :].lstrip(" .")
        if trimmed:
            parts.append(trimmed)
    return ". ".join(parts)
