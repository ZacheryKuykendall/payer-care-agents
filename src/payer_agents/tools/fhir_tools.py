"""The FHIR tool set exposed over MCP.

Five tools, four read and one write. The write tool exists specifically so the
governance story has something to refuse: the member-facing assistant is issued an
identity that is not allowed to call ``submit_prior_authorization``, and the demo
shows the gateway rejecting it. A tool list that only contains safe tools proves
nothing about the controls around it.

Tool descriptions are part of the security surface. An MCP client decides when to
call a tool from its description, so the descriptions here state the limits of the
tool as well as its purpose.
"""

from __future__ import annotations

from typing import Any

from payer_agents.fhir.client import FhirClient
from payer_agents.tools.registry import RiskTier, ToolRegistry, ToolSpec

MEMBER_ID_SCHEMA = {
    "type": "string",
    "description": "Plan member identifier, for example NW-100002.",
    "pattern": "^[A-Za-z]{2,3}-?[0-9]{5,9}$",
}


def build_fhir_registry(client: FhirClient) -> ToolRegistry:
    """Bind the FHIR client into a registry of MCP tools."""

    def find_member_coverage(member_id: str) -> dict[str, Any]:
        summary = client.coverage_summary(member_id)
        if summary is None:
            return {"found": False, "member_id": member_id.upper()}
        return {"found": True, **summary}

    def get_prior_authorization_status(
        member_id: str, reference_number: str | None = None
    ) -> dict[str, Any]:
        authorizations = client.prior_authorizations(member_id, reference_number)
        return {
            "member_id": member_id.upper(),
            "count": len(authorizations),
            "authorizations": [a.model_dump(mode="json") for a in authorizations],
        }

    def list_member_conditions(member_id: str) -> dict[str, Any]:
        conditions = client.conditions(member_id)
        return {"member_id": member_id.upper(), "count": len(conditions),
                "conditions": conditions}

    def list_member_medications(member_id: str) -> dict[str, Any]:
        medications = client.medications(member_id)
        return {"member_id": member_id.upper(), "count": len(medications),
                "medications": medications}

    def submit_prior_authorization(
        member_id: str, service_code: str, clinical_summary: str
    ) -> dict[str, Any]:
        # Reached only if a caller is authorised for it, which the member-facing
        # assistant is not. Kept as a stub because this reference architecture does
        # not simulate an intake pipeline.
        raise PermissionError(
            "submit_prior_authorization is not available to this agent identity"
        )

    return ToolRegistry(
        [
            ToolSpec(
                name="find_member_coverage",
                title="Find member coverage",
                description=(
                    "Look up a member's active coverage: plan identifier, plan name, "
                    "group and effective dates. Use this before answering any "
                    "plan-specific benefit question so the answer is filtered to the "
                    "member's actual plan. Returns found=false for an unknown member; "
                    "it does not search by name."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"member_id": MEMBER_ID_SCHEMA},
                    "required": ["member_id"],
                    "additionalProperties": False,
                },
                handler=find_member_coverage,
                risk=RiskTier.MEMBER_READ,
            ),
            ToolSpec(
                name="get_prior_authorization_status",
                title="Get prior authorization status",
                description=(
                    "Return the status of a member's prior authorization requests, "
                    "newest first, optionally narrowed to one reference number such as "
                    "PA-20260207. Reports an existing determination only. It cannot "
                    "approve, deny, expedite or appeal a request."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "member_id": MEMBER_ID_SCHEMA,
                        "reference_number": {
                            "type": "string",
                            "description": "Authorization reference, for example PA-20260207.",
                        },
                    },
                    "required": ["member_id"],
                    "additionalProperties": False,
                },
                handler=get_prior_authorization_status,
                risk=RiskTier.CLINICAL_READ,
            ),
            ToolSpec(
                name="list_member_conditions",
                title="List member conditions",
                description=(
                    "Return the member's documented active conditions. Use only to "
                    "check whether a coverage criterion that depends on a diagnosis is "
                    "met. Never use it to discuss, interpret or infer a diagnosis with "
                    "the member."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"member_id": MEMBER_ID_SCHEMA},
                    "required": ["member_id"],
                    "additionalProperties": False,
                },
                handler=list_member_conditions,
                risk=RiskTier.CLINICAL_READ,
            ),
            ToolSpec(
                name="list_member_medications",
                title="List member medications",
                description=(
                    "Return the member's active medication orders. Use only to check a "
                    "formulary or step-therapy criterion. Never use it to give dosing "
                    "advice or to recommend starting or stopping a medication."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"member_id": MEMBER_ID_SCHEMA},
                    "required": ["member_id"],
                    "additionalProperties": False,
                },
                handler=list_member_medications,
                risk=RiskTier.CLINICAL_READ,
            ),
            ToolSpec(
                name="submit_prior_authorization",
                title="Submit a prior authorization request",
                description=(
                    "Create a new prior authorization request. Restricted to "
                    "provider-facing intake agents. The member-facing assistant is not "
                    "authorised for this tool and calls will be rejected at the "
                    "gateway."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "member_id": MEMBER_ID_SCHEMA,
                        "service_code": {"type": "string"},
                        "clinical_summary": {"type": "string"},
                    },
                    "required": ["member_id", "service_code", "clinical_summary"],
                    "additionalProperties": False,
                },
                handler=submit_prior_authorization,
                risk=RiskTier.CLINICAL_WRITE,
                writes=True,
            ),
        ]
    )
