"""Tool registry, gateway policy chain, MCP client and MCP server.

The gateway tests double as the parity check between
``src/payer_agents/gateway/apim_sim.py`` and ``infra/apim/policies/mcp-server.xml``.
If the chain order changes in one, these tests should fail until it changes in both.
"""

from __future__ import annotations

import json

import pytest

from payer_agents.fhir.store import FhirStore, FhirStoreError, derive_status
from payer_agents.gateway.apim_sim import (
    ApiManagementSimulator,
    CallContext,
    GatewayPolicy,
    RateLimiter,
)
from payer_agents.tools.mcp_client import McpToolError
from payer_agents.tools.mcp_server import context_from_headers, handle_rpc
from payer_agents.tools.registry import (
    RiskTier,
    ToolNotFound,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)

AUTHORISED = CallContext(agent_id="a1", subscription_key="k", correlation_id="c1")


def _spec(name="echo", **overrides) -> ToolSpec:
    defaults = dict(
        name=name,
        title="Echo",
        description="Echo a value back.",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}, "count": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        handler=lambda value, count=1: {"value": value, "count": count},
    )
    defaults.update(overrides)
    return ToolSpec(**defaults)


# ---------------------------------------------------------------- registry


def test_registry_exposes_governance_metadata_to_mcp_clients():
    spec = _spec(risk=RiskTier.CLINICAL_READ)
    described = spec.to_mcp()
    assert described["annotations"]["riskTier"] == "clinical_read"
    assert described["annotations"]["readOnlyHint"] is True
    assert described["annotations"]["touchesPhi"] is True
    assert _spec(writes=True).to_mcp()["annotations"]["readOnlyHint"] is False


def test_registry_rejects_duplicate_names_and_reports_unknown_ones():
    registry = ToolRegistry([_spec()])
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_spec())
    with pytest.raises(ToolNotFound):
        registry.get("missing")
    assert "echo" in registry
    assert len(registry) == 1
    assert registry.describe()[0]["name"] == "echo"


@pytest.mark.parametrize(
    "arguments, error",
    [
        ({}, "missing required argument: value"),
        ({"value": "   "}, "missing required argument: value"),
        ({"value": "x", "extra": 1}, "unexpected arguments: extra"),
        ({"value": 42}, "must be of type string"),
        ({"value": "x", "count": True}, "must be of type integer"),
    ],
)
def test_argument_validation_rejects_malformed_calls(arguments, error):
    result = ToolRegistry([_spec()]).invoke("echo", arguments)
    assert result.ok is False
    assert error in (result.error or "")


def test_invoke_returns_a_result_and_times_it():
    result = ToolRegistry([_spec()]).invoke("echo", {"value": "hi"})
    assert result.ok is True
    assert result.content == {"value": "hi", "count": 1}
    assert result.duration_ms >= 0


def test_handler_exceptions_become_failed_results_not_crashes():
    def boom(value):
        raise PermissionError("not allowed")

    result = ToolRegistry([_spec(handler=boom)]).invoke("echo", {"value": "x"})
    assert result.ok is False
    assert "PermissionError: not allowed" in (result.error or "")


def test_mcp_result_marks_errors_in_band():
    ok = ToolResult(tool="t", ok=True, content={"a": 1}).to_mcp()
    assert ok["isError"] is False
    assert json.loads(ok["content"][0]["text"]) == {"a": 1}
    bad = ToolResult(tool="t", ok=False, error="nope").to_mcp()
    assert bad["isError"] is True
    assert "nope" in bad["content"][0]["text"]


# ----------------------------------------------------------------- gateway


def test_chain_order_matches_the_apim_policy():
    """Credential, then correlation id, then tool authorisation, then rate limit.
    Reordering these changes which failure a caller sees first."""
    gateway = ApiManagementSimulator(GatewayPolicy(rate_limit_calls=1))

    no_credential = gateway.authorize("find_member_coverage", CallContext())
    assert (no_credential.status_code, no_credential.policy) == (401, "validate-azure-ad-token")

    no_correlation = gateway.authorize("find_member_coverage", CallContext(subscription_key="k"))
    assert (no_correlation.status_code, no_correlation.policy) == (400, "check-header")

    blocked = gateway.authorize("submit_prior_authorization", AUTHORISED)
    assert (blocked.status_code, blocked.policy) == (403, "tool-allow-list")

    assert gateway.authorize("find_member_coverage", AUTHORISED).allowed is True
    throttled = gateway.authorize("find_member_coverage", AUTHORISED)
    assert (throttled.status_code, throttled.policy) == (429, "rate-limit-by-key")


def test_a_bearer_token_is_an_acceptable_credential():
    gateway = ApiManagementSimulator()
    context = CallContext(bearer_token="jwt", correlation_id="c")
    assert gateway.authorize("find_member_coverage", context).allowed is True


def test_allow_list_restricts_to_named_tools():
    policy = GatewayPolicy(allowed_tools=["find_member_coverage"], denied_tools=[])
    assert policy.permits("find_member_coverage") is True
    assert policy.permits("list_member_conditions") is False
    assert GatewayPolicy(denied_tools=[]).permits("anything") is True


def test_rate_limit_is_per_agent_not_global():
    limiter = RateLimiter(calls=1, window_seconds=60, clock=lambda: 0.0)
    assert limiter.check("agent-a") == (True, 0)
    assert limiter.check("agent-b")[0] is True  # different budget
    assert limiter.check("agent-a")[0] is False


def test_rate_limit_window_slides():
    now = {"t": 0.0}
    limiter = RateLimiter(calls=1, window_seconds=10, clock=lambda: now["t"])
    assert limiter.check("a")[0] is True
    assert limiter.check("a")[0] is False
    now["t"] = 11.0
    assert limiter.check("a")[0] is True
    limiter.reset("a")
    assert limiter.check("a")[0] is True
    limiter.reset()


def test_gateway_logs_rejections_as_well_as_successes(audit):
    gateway = ApiManagementSimulator(GatewayPolicy(), audit=audit)
    decision = gateway.authorize("submit_prior_authorization", AUTHORISED)
    gateway.log("submit_prior_authorization", AUTHORISED, decision, outcome="rejected")
    event = audit.events[-1]
    assert event.category == "tool_gateway"
    assert event.outcome == "rejected"
    assert event.detail["gateway_status"] == 403
    assert event.detail["purpose_of_use"] == "TREAT"


def test_gateway_without_an_audit_log_is_a_no_op():
    gateway = ApiManagementSimulator(GatewayPolicy())
    gateway.log("x", AUTHORISED, gateway.authorize("find_member_coverage", AUTHORISED))


def test_decision_summary_is_readable():
    gateway = ApiManagementSimulator()
    assert gateway.authorize("find_member_coverage", AUTHORISED).summary == "allow"
    assert "403" in gateway.authorize("submit_prior_authorization", AUTHORISED).summary


# ------------------------------------------------------------- mcp client


def test_in_process_client_hides_tools_the_identity_cannot_call(mcp):
    names = [tool["name"] for tool in mcp.list_tools()]
    assert "find_member_coverage" in names
    assert "submit_prior_authorization" not in names


def test_tool_call_returns_a_structured_payload_not_a_truncated_string(mcp):
    call = mcp.call_tool("get_prior_authorization_status", {"member_id": "NW-100002"})
    assert call.ok is True
    assert isinstance(call.payload, dict)
    assert call.payload["count"] == 2
    # The long disposition text must survive; summarising for display truncates.
    assert len(call.result_summary) <= 600


def test_member_identifiers_are_redacted_from_the_recorded_arguments(mcp):
    call = mcp.call_tool("find_member_coverage", {"member_id": "NW-100001"})
    assert call.arguments == {"member_id": "<redacted>"}
    assert "NW-100001" not in json.dumps(call.model_dump(mode="json"))


def test_tool_payload_is_excluded_from_serialisation(mcp):
    call = mcp.call_tool("find_member_coverage", {"member_id": "NW-100001"})
    assert "payload" not in call.model_dump(mode="json")


def test_blocked_tool_is_refused_before_the_handler_runs(mcp):
    call = mcp.call_tool(
        "submit_prior_authorization",
        {"member_id": "NW-100002", "service_code": "72148", "clinical_summary": "x"},
    )
    assert call.ok is False
    assert "tool-allow-list" in (call.error or "")
    assert call.gateway_decision.startswith("tool-allow-list")


def test_invoke_raw_raises_for_a_blocked_tool(mcp):
    with pytest.raises(McpToolError) as excinfo:
        mcp.invoke_raw("submit_prior_authorization", {"member_id": "NW-100002"})
    assert excinfo.value.status_code == 403


def test_invoke_raw_returns_the_payload_for_an_allowed_tool(mcp):
    result = mcp.invoke_raw("find_member_coverage", {"member_id": "NW-100001"})
    assert result.ok is True
    assert result.content["plan_id"] == "NW-MA-HMO-2026"


def test_every_tool_call_writes_an_audit_record(mcp, audit):
    before = len(audit.events)
    mcp.call_tool("find_member_coverage", {"member_id": "NW-100001"})
    mcp.call_tool("submit_prior_authorization", {"member_id": "NW-100001"})
    categories = [e.category for e in audit.events[before:]]
    assert categories == ["tool_gateway", "tool_gateway"]


def test_unknown_tool_is_reported_not_raised(registry, gateway):
    from payer_agents.tools.mcp_client import InProcessMcpClient

    client = InProcessMcpClient(registry, gateway, AUTHORISED)
    call = client.call_tool("no_such_tool", {})
    assert call.ok is False
    assert "unknown tool" in (call.error or "")


# ------------------------------------------------------------- mcp server


def _rpc(method, registry, gateway, params=None, request_id=1, context=AUTHORISED):
    return handle_rpc(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
        registry,
        gateway,
        context,
    )


def test_initialize_advertises_tools_only(registry, gateway):
    result = _rpc("initialize", registry, gateway)["result"]
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert "resources" not in result["capabilities"]
    assert result["serverInfo"]["name"] == "payer-agents-fhir-tools"


def test_notifications_get_no_response(registry, gateway):
    assert _rpc("notifications/initialized", registry, gateway) is None


def test_ping_and_tools_list(registry, gateway):
    assert _rpc("ping", registry, gateway)["result"] == {}
    tools = _rpc("tools/list", registry, gateway)["result"]["tools"]
    assert "submit_prior_authorization" not in [t["name"] for t in tools]


def test_tools_call_round_trip(registry, gateway):
    response = _rpc(
        "tools/call",
        registry,
        gateway,
        {"name": "find_member_coverage", "arguments": {"member_id": "NW-100002"}},
    )
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["plan_name"] == "Northwind Advantage Choice (PPO)"
    assert response["result"]["isError"] is False


def test_tools_call_is_refused_for_a_blocked_tool(registry, gateway):
    response = _rpc(
        "tools/call",
        registry,
        gateway,
        {"name": "submit_prior_authorization", "arguments": {"member_id": "NW-100002"}},
    )
    assert response["error"]["data"]["status"] == 403


@pytest.mark.parametrize(
    "message, code",
    [
        ({"jsonrpc": "1.0", "id": 1, "method": "ping"}, -32600),
        ({"jsonrpc": "2.0", "id": 1, "method": "nope"}, -32601),
    ],
)
def test_protocol_errors(registry, gateway, message, code):
    assert handle_rpc(message, registry, gateway, AUTHORISED)["error"]["code"] == code


def test_tools_call_rejects_malformed_params(registry, gateway):
    response = _rpc("tools/call", registry, gateway, {"name": 123})
    assert response["error"]["code"] == -32602


def test_tools_call_reports_an_unknown_tool(registry, gateway):
    response = _rpc("tools/call", registry, gateway, {"name": "nope", "arguments": {}})
    assert response["error"]["code"] == -32601


def test_context_is_built_from_the_headers_apim_populates():
    context = context_from_headers(
        {
            "x-agent-id": "member-assistant",
            "authorization": "Bearer abc",
            "x-correlation-id": "corr-1",
            "x-purpose-of-use": "HPAYMT",
            "ocp-apim-subscription-key": "sub",
        }
    )
    assert context.agent_id == "member-assistant"
    assert context.bearer_token == "abc"
    assert context.purpose_of_use == "HPAYMT"
    default = context_from_headers({})
    assert default.agent_id == "mcp-http-client"
    assert default.purpose_of_use == "TREAT"
    assert default.bearer_token is None


# -------------------------------------------------------------- fhir store


def test_store_refuses_a_bundle_that_is_not_tagged_synthetic():
    store = FhirStore()
    with pytest.raises(FhirStoreError, match="synthetic"):
        store.load_bundle({"resourceType": "Bundle", "entry": []}, source="real.json")
    with pytest.raises(FhirStoreError, match="expected a Bundle"):
        store.load_bundle({"resourceType": "Patient"})


def test_store_rejects_a_resource_without_an_id():
    with pytest.raises(FhirStoreError, match="resourceType or id"):
        FhirStore([{"resourceType": "Patient"}])


def test_store_missing_directory(tmp_path):
    with pytest.raises(FhirStoreError, match="not found"):
        FhirStore.from_directory(tmp_path / "nope")


@pytest.mark.parametrize(
    "outcome, disposition, expected",
    [
        ("complete", "Approved.", "approved"),
        ("partial", "Some units approved.", "partially approved"),
        ("error", "Denied.", "denied"),
        ("queued", "In clinical review.", "in clinical review"),
        ("queued", "Pending information: records requested.", "pending information"),
        ("queued", "Withdrawn by provider.", "withdrawn"),
        ("unmapped", "", "received"),
    ],
)
def test_claim_outcome_maps_to_a_status_a_member_recognises(outcome, disposition, expected):
    assert derive_status(outcome, disposition) == expected


def test_prior_authorisations_flatten_claim_and_response(fhir_store):
    patient = fhir_store.patient_by_member_id("nw-100002")
    authorizations = fhir_store.prior_authorizations(patient["id"])
    assert [a.reference_number for a in authorizations] == ["PA-20260264", "PA-20260207"]
    approved = authorizations[1]
    assert approved.status == "approved"
    assert approved.is_final is True
    assert approved.valid_through == "2026-04-25"


def test_prior_authorisations_filter_by_reference(fhir_store):
    patient = fhir_store.patient_by_member_id("NW-100002")
    only = fhir_store.prior_authorizations(patient["id"], "pa-20260207")
    assert len(only) == 1
    assert fhir_store.prior_authorizations(patient["id"], "PA-00000000") == []


def test_unknown_member_returns_nothing_rather_than_guessing(fhir_client):
    assert fhir_client.find_patient("NW-999999") is None
    assert fhir_client.coverage_summary("NW-999999") is None
    assert fhir_client.prior_authorizations("NW-999999") == []
    assert fhir_client.conditions("NW-999999") == []
    assert fhir_client.medications("NW-999999") == []
    assert fhir_client.plan_id("NW-999999") is None
    assert fhir_client.find_patient("") is None


def test_every_registered_tool_answers_for_a_known_member(registry):
    for name in (
        "find_member_coverage",
        "get_prior_authorization_status",
        "list_member_conditions",
        "list_member_medications",
    ):
        result = registry.invoke(name, {"member_id": "NW-100003"})
        assert result.ok is True, name
        assert result.content["member_id"] == "NW-100003"


def test_read_tools_report_an_unknown_member_without_raising(registry):
    coverage = registry.invoke("find_member_coverage", {"member_id": "NW-999999"})
    assert coverage.content == {"found": False, "member_id": "NW-999999"}
    assert (
        registry.invoke("list_member_conditions", {"member_id": "NW-999999"}).content["count"] == 0
    )


def test_the_write_tool_refuses_even_if_the_gateway_is_bypassed(registry):
    """Belt and braces: the handler itself declines, so a misconfigured gateway
    cannot turn a read-only agent into a writing one."""
    result = registry.invoke(
        "submit_prior_authorization",
        {"member_id": "NW-100002", "service_code": "72148", "clinical_summary": "x"},
    )
    assert result.ok is False
    assert "not available to this agent identity" in (result.error or "")


def test_tool_descriptions_state_their_limits_not_just_their_purpose(registry):
    prior_auth = registry.get("get_prior_authorization_status")
    assert "cannot approve, deny, expedite or appeal" in prior_auth.description
    assert (
        "Never use it to give dosing advice" in registry.get("list_member_medications").description
    )


def test_client_projects_coverage_conditions_and_medications(fhir_client):
    coverage = fhir_client.coverage_summary("NW-100003")
    assert coverage["plan_id"] == "NW-COM-PPO-2026"
    assert coverage["member_name"].startswith("Darrel268")
    assert fhir_client.plan_id("NW-100003") == "NW-COM-PPO-2026"
    assert any("diabetes" in c["display"].lower() for c in fhir_client.conditions("NW-100003"))
    assert len(fhir_client.medications("NW-100003")) == 2
