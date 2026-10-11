"""HTTP surface and the command line.

The HTTP tests cover the paths the demo console calls, because a console that
404s during an interview is worse than no console.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from payer_agents.api.app import create_app
from payer_agents.cli import build_parser, main
from payer_agents.config import (
    MissingConfigError,
    RuntimeMode,
    Settings,
    _clean,
)
from payer_agents.demo_scenarios import SCENARIOS, SCENARIOS_BY_KEY


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app(settings=Settings()))


# -------------------------------------------------------------------- api


def test_health_and_system_describe_the_running_configuration(client):
    assert client.get("/healthz").json() == {"status": "ok", "mode": "mock"}
    system = client.get("/api/system").json()
    assert system["composer"] == "extractive"
    assert len(system["tools"]) == 5
    assert system["egress_policy"]["rules"]


def test_scenarios_endpoint_feeds_the_console(client):
    scenarios = client.get("/api/scenarios").json()["scenarios"]
    assert len(scenarios) == len(SCENARIOS)
    assert {s["key"] for s in scenarios} == set(SCENARIOS_BY_KEY)
    assert all(s["teaches"] and s["expectation"] for s in scenarios)


def test_chat_returns_an_automated_answer_with_citations(client):
    response = client.post(
        "/api/chat",
        json={"text": "What is my specialist copay? My member ID is NW-100001."},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["automated"] is True
    assert body["result"]["answer"]["citations"]
    assert "$35" in body["display_text"]


def test_chat_returns_a_handoff_for_an_escalation(client):
    body = client.post(
        "/api/chat",
        json={"text": "I have chest pain and cannot catch my breath", "channel": "call_center"},
    ).json()
    assert body["automated"] is False
    assert body["result"]["handoff"]["queue"] == "nurse-triage"
    assert body["result"]["answer"] is None


def test_chat_response_never_carries_a_raw_tool_payload(client):
    body = client.post("/api/chat", json={"text": "What is my specialist copay? NW-100001"}).json()
    assert "payload" not in json.dumps(body["result"]["answer"]["tool_calls"])


def test_chat_honours_a_supplied_conversation_id(client):
    body = client.post("/api/chat", json={"text": "hello", "conversation_id": "conv_fixed"}).json()
    assert body["result"]["conversation_id"] == "conv_fixed"


@pytest.mark.parametrize("payload", [{"text": ""}, {}, {"text": "x" * 5000}])
def test_chat_validates_its_input(client, payload):
    assert client.post("/api/chat", json=payload).status_code == 422


def test_audit_endpoint_reports_a_verified_chain(client):
    client.post("/api/chat", json={"text": "hello"})
    body = client.get("/api/audit?limit=5").json()
    assert body["chain_valid"] is True
    assert len(body["events"]) <= 5
    assert body["total"] >= len(body["events"])


@pytest.mark.parametrize(
    "url, allowed",
    [
        ("https://apim-payer-agents.azure-api.net/mcp", True),
        ("https://api.openai.com/v1/chat/completions", False),
        ("https://anything-unlisted.example.net/x", False),
    ],
)
def test_egress_endpoint_evaluates_the_shipped_policy(client, url, allowed):
    decision = client.post("/api/egress/evaluate", json={"url": url}).json()
    assert decision["allowed"] is allowed


def test_evals_endpoint_runs_the_labelled_suite(client):
    report = client.get("/api/evals").json()
    assert report["sample_size"] >= 30
    assert 0.0 <= report["routing_accuracy"] <= 1.0
    assert report["threshold_points"]
    assert report["notes"]


def test_mcp_endpoint_speaks_json_rpc(client):
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"x-correlation-id": "c", "ocp-apim-subscription-key": "k"},
    )
    assert "submit_prior_authorization" not in json.dumps(response.json())


def test_mcp_endpoint_rejects_the_write_tool_at_the_gateway(client):
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "submit_prior_authorization",
                "arguments": {
                    "member_id": "NW-100002",
                    "service_code": "72148",
                    "clinical_summary": "x",
                },
            },
        },
        headers={
            "x-correlation-id": "c",
            "ocp-apim-subscription-key": "k",
            "x-agent-id": "member-assistant",
        },
    )
    assert response.json()["error"]["data"]["status"] == 403


def test_mcp_endpoint_handles_batches_notifications_and_bad_bodies(client):
    headers = {"x-correlation-id": "c", "ocp-apim-subscription-key": "k"}
    batch = client.post(
        "/mcp",
        json=[
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ],
        headers=headers,
    )
    assert len(batch.json()) == 1

    only_notification = client.post(
        "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers
    )
    assert only_notification.status_code == 202

    assert client.post("/mcp", content=b"{not json", headers=headers).status_code == 400
    assert client.post("/mcp", json="a string", headers=headers).status_code == 400
    assert client.post("/mcp", json=[], headers=headers).status_code == 202


def test_mcp_tools_listing_marks_which_tools_are_permitted(client):
    tools = client.get("/mcp/tools").json()["tools"]
    permitted = {tool["name"]: tool["permitted"] for tool in tools}
    assert permitted["find_member_coverage"] is True
    assert permitted["submit_prior_authorization"] is False


def test_fhir_stub_serves_a_capability_statement_and_searches(client):
    capability = client.get("/fhir/metadata").json()
    assert capability["fhirVersion"] == "4.0.1"
    assert "Synthetic data only" in capability["implementation"]["description"]

    bundle = client.get("/fhir/Patient", params={"identifier": "NW-100002"}).json()
    assert bundle["total"] == 1
    assert bundle["entry"][0]["resource"]["id"] == "nw-100002"


def test_fhir_stub_filters_claims_by_use_and_reads_by_id(client):
    claims = client.get(
        "/fhir/Claim", params={"patient": "Patient/nw-100002", "use": "preauthorization"}
    ).json()
    assert claims["total"] == 2
    single = client.get("/fhir/Claim/claim-nw-100002-pa1").json()
    assert single["resourceType"] == "Claim"


def test_fhir_stub_404s_for_unknown_types_and_ids(client):
    assert client.get("/fhir/Observation").status_code == 404
    assert client.get("/fhir/Patient/nope").status_code == 404


def test_index_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Payer Care Agents" in response.text
    assert client.get("/static/app.js").status_code == 200


# -------------------------------------------------------------------- cli


def _run(argv, capsys):
    code = main(argv)
    return code, capsys.readouterr().out


def test_demo_runs_every_scenario_and_summarises(capsys):
    code, out = _run(["demo"], capsys)
    assert code == 0
    assert "Payer Care Agents" in out
    assert "AUTOMATED" in out
    assert "HUMAN IN THE LOOP" in out
    assert "audit chain verified" in out


def test_demo_can_be_narrowed_to_named_scenarios(capsys):
    code, out = _run(["demo", "--only", "benefits"], capsys)
    assert code == 0
    assert "Specialist copay and referral rule" in out
    assert "Prompt injection attempt" not in out


def test_demo_rejects_an_unknown_scenario(capsys):
    code, out = _run(["demo", "--only", "nonsense"], capsys)
    assert code == 2
    assert "unknown scenario" in out


def test_ask_prints_a_turn_and_supports_json(capsys):
    code, out = _run(["ask", "What is my specialist copay?"], capsys)
    assert code == 0
    assert "benefits_coverage" in out

    code, out = _run(["ask", "hello", "--json"], capsys)
    assert code == 0
    assert json.loads(out)["routing"]["intent"]


def test_evals_command_prints_metrics_and_the_sweep(capsys):
    code, out = _run(["evals"], capsys)
    assert code == 0
    assert "routing accuracy" in out
    assert "Threshold sweep" in out
    assert "note:" in out

    code, out = _run(["evals", "--json"], capsys)
    assert json.loads(out)["sample_size"] >= 30


def test_tools_command_shows_which_tools_the_gateway_blocks(capsys):
    code, out = _run(["tools"], capsys)
    assert code == 0
    assert "blocked" in out
    assert "allowed" in out


def test_egress_command_evaluates_urls(capsys):
    code, out = _run(
        ["egress", "https://api.openai.com/v1/x", "https://x.search.windows.net/y"], capsys
    )
    assert code == 0
    assert "Deny" in out and "Allow" in out


def test_audit_command_prints_a_verified_chain(capsys):
    code, out = _run(["audit", "--limit", "5"], capsys)
    assert code == 0
    assert "hash chain valid" in out

    code, out = _run(["audit", "--json"], capsys)
    assert len(json.loads(out)) > 10


def test_bare_invocation_defaults_to_the_demo(capsys):
    code, out = _run([], capsys)
    assert code == 0
    assert "Summary" in out


def test_mode_flag_overrides_the_environment():
    parser = build_parser()
    assert parser.parse_args(["--mode", "mock", "demo"]).mode == "mock"
    with pytest.raises(MissingConfigError):
        main(["--mode", "azure", "tools"])


# ----------------------------------------------------------------- config


def test_settings_default_to_mock_and_need_no_environment():
    settings = Settings.from_env({})
    assert settings.mode is RuntimeMode.MOCK
    assert settings.foundry.endpoint is None


def test_settings_read_azure_values_from_the_environment():
    settings = Settings.from_env(
        {
            "PAYER_AGENTS_MODE": "azure",
            "FOUNDRY_PROJECT_ENDPOINT": "https://x.services.ai.azure.com",
            "AZURE_SEARCH_ENDPOINT": "https://x.search.windows.net",
            "MCP_GATEWAY_ENDPOINT": "https://x.azure-api.net/mcp",
            "FOUNDRY_DECISION_DEPLOYMENT": "router-v2",
            "PAYER_AGENTS_AUDIT_LOG": "/tmp/audit.jsonl",
        }
    )
    assert settings.mode is RuntimeMode.AZURE
    assert settings.foundry.decision_deployment == "router-v2"
    assert settings.audit_sink_path is not None


def test_azure_mode_names_every_missing_setting_at_once():
    with pytest.raises(MissingConfigError) as excinfo:
        Settings.from_env({"PAYER_AGENTS_MODE": "azure"})
    message = str(excinfo.value)
    assert "FOUNDRY_PROJECT_ENDPOINT" in message
    assert "AZURE_SEARCH_ENDPOINT" in message
    assert "MCP_GATEWAY_ENDPOINT" in message


def test_blank_environment_values_are_treated_as_absent():
    assert _clean("  ") is None
    assert _clean(None) is None
    assert _clean(" x ") == "x"


def test_api_keys_are_not_echoed_in_a_repr():
    settings = Settings.from_env({"FOUNDRY_API_KEY": "super-secret"})
    assert "super-secret" not in repr(settings)
