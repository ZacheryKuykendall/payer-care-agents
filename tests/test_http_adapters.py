"""The networked adapters.

These classes are the Azure-mode half of every seam in the architecture, so they are
the half most likely to rot: mock mode exercises the other implementation and nothing
notices when these drift. Faked transports keep them covered without a subscription.
"""

from __future__ import annotations

import json

import pytest

from payer_agents.fhir.client import HttpFhirClient
from payer_agents.fhir.server import as_bundle, search
from payer_agents.retrieval.embeddings import AzureOpenAIEmbedder
from payer_agents.tools.mcp_client import HttpMcpClient, McpToolError


class _RecordingTransport:
    """Serves FHIR searchset bundles from the in-memory synthetic store."""

    def __init__(self, store):
        self.store = store
        self.calls: list[tuple[str, dict]] = []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        return search(self.store, path, dict(params or {}))


@pytest.fixture
def http_fhir(fhir_store) -> HttpFhirClient:
    return HttpFhirClient(_RecordingTransport(fhir_store))


# ------------------------------------------------------------- HttpFhirClient


def test_http_client_finds_a_patient_by_identifier(http_fhir):
    patient = http_fhir.find_patient("NW-100001")
    assert patient["id"] == "nw-100001"
    assert http_fhir.transport.calls[0] == ("Patient", {"identifier": "NW-100001"})


def test_http_client_projects_coverage_identically_to_the_local_client(http_fhir, fhir_client):
    """The two implementations are behind one protocol; divergence here is a bug."""
    assert http_fhir.coverage_summary("NW-100002") == fhir_client.coverage_summary("NW-100002")


def test_http_client_flattens_prior_authorizations_like_the_local_client(http_fhir, fhir_client):
    remote = http_fhir.prior_authorizations("NW-100002")
    local = fhir_client.prior_authorizations("NW-100002")
    assert [a.reference_number for a in remote] == [a.reference_number for a in local]
    assert [a.status for a in remote] == [a.status for a in local]


def test_http_client_filters_by_reference_number(http_fhir):
    only = http_fhir.prior_authorizations("NW-100002", "pa-20260207")
    assert [a.reference_number for a in only] == ["PA-20260207"]
    assert http_fhir.prior_authorizations("NW-100002", "PA-00000000") == []


def test_http_client_returns_conditions_and_medications(http_fhir):
    conditions = http_fhir.conditions("NW-100003")
    assert any("diabetes" in (c["display"] or "").lower() for c in conditions)
    assert len(http_fhir.medications("NW-100003")) == 2


def test_http_client_is_quiet_about_an_unknown_member(http_fhir):
    assert http_fhir.find_patient("NW-999999") is None
    assert http_fhir.coverage_summary("NW-999999") is None
    assert http_fhir.prior_authorizations("NW-999999") == []
    assert http_fhir.conditions("NW-999999") == []
    assert http_fhir.medications("NW-999999") == []


def test_http_client_handles_a_member_with_no_coverage(fhir_store):
    class _NoCoverage(_RecordingTransport):
        def get(self, path, params=None):
            if path == "Coverage":
                return as_bundle([])
            return super().get(path, params)

    assert HttpFhirClient(_NoCoverage(fhir_store)).coverage_summary("NW-100001") is None


def test_entry_parsing_tolerates_an_empty_or_malformed_bundle():
    assert HttpFhirClient._entries(None) == []
    assert HttpFhirClient._entries({"entry": [{"resource": "not an object"}]}) == []


# --------------------------------------------------------------- HttpMcpClient


class _FakeHttpResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeHttpClient:
    def __init__(self, responder):
        self.responder = responder
        self.requests: list[dict] = []

    def post(self, url, json=None, headers=None):
        self.requests.append({"url": url, "body": json, "headers": headers})
        return self.responder(json)


def _tool_response(payload, is_error=False):
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "isError": is_error,
        },
    }


def test_http_mcp_client_lists_tools_over_json_rpc():
    http = _FakeHttpClient(
        lambda body: _FakeHttpResponse(
            {"jsonrpc": "2.0", "id": body["id"], "result": {"tools": [{"name": "t"}]}}
        )
    )
    client = HttpMcpClient("https://apim/mcp", {"Ocp-Apim-Subscription-Key": "k"}, http)
    assert client.list_tools() == [{"name": "t"}]
    sent = http.requests[0]
    assert sent["body"]["method"] == "tools/list"
    assert sent["body"]["jsonrpc"] == "2.0"
    # The gateway credential travels with the agent; a FHIR credential never does.
    assert sent["headers"]["Ocp-Apim-Subscription-Key"] == "k"


def test_http_mcp_client_parses_a_tool_payload():
    http = _FakeHttpClient(lambda body: _FakeHttpResponse(_tool_response({"count": 2})))
    call = HttpMcpClient("https://apim/mcp", client=http).call_tool(
        "get_prior_authorization_status", {"member_id": "NW-100002"}
    )
    assert call.ok is True
    assert call.payload == {"count": 2}
    assert call.arguments == {"member_id": "<redacted>"}


def test_http_mcp_client_keeps_non_json_content_as_text():
    http = _FakeHttpClient(
        lambda body: _FakeHttpResponse(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": "plain text"}], "isError": False},
            }
        )
    )
    assert HttpMcpClient("https://apim/mcp", client=http).call_tool("t").payload == "plain text"


def test_http_mcp_client_surfaces_an_in_band_tool_error():
    http = _FakeHttpClient(
        lambda body: _FakeHttpResponse(_tool_response({"error": "nope"}, is_error=True))
    )
    call = HttpMcpClient("https://apim/mcp", client=http).call_tool("t")
    assert call.ok is False
    assert "nope" in (call.error or "")


def test_http_mcp_client_turns_a_gateway_rejection_into_a_failed_call():
    http = _FakeHttpClient(lambda body: _FakeHttpResponse({"detail": "forbidden"}, 403))
    call = HttpMcpClient("https://apim/mcp", client=http).call_tool("submit_prior_authorization")
    assert call.ok is False
    assert call.gateway_decision == "transport (403)"


def test_http_mcp_client_raises_on_a_json_rpc_error_for_list_calls():
    http = _FakeHttpClient(
        lambda body: _FakeHttpResponse(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "unknown method"}}
        )
    )
    with pytest.raises(McpToolError, match="unknown method"):
        HttpMcpClient("https://apim/mcp", client=http).list_tools()


def test_http_mcp_client_increments_request_ids():
    http = _FakeHttpClient(
        lambda body: _FakeHttpResponse({"jsonrpc": "2.0", "id": body["id"], "result": {}})
    )
    client = HttpMcpClient("https://apim/mcp", client=http)
    client.list_tools()
    client.list_tools()
    assert [request["body"]["id"] for request in http.requests] == [1, 2]


# ------------------------------------------------------- AzureOpenAIEmbedder


def test_azure_embedder_posts_a_batch_and_returns_vectors():
    http = _FakeHttpClient(
        lambda body: _FakeHttpResponse({"data": [{"embedding": [0.1, 0.2]} for _ in body["input"]]})
    )
    # raise_for_status is part of the httpx contract the embedder relies on.
    http.responder = lambda body: type(
        "R",
        (),
        {
            "raise_for_status": staticmethod(lambda: None),
            "json": staticmethod(
                lambda: {"data": [{"embedding": [0.1, 0.2]} for _ in body["input"]]}
            ),
        },
    )()

    embedder = AzureOpenAIEmbedder(
        "https://x.openai.azure.com/", "embed", api_key="k", client=http, dimensions=2
    )
    assert embedder.embed_batch(["a", "b"]) == [[0.1, 0.2], [0.1, 0.2]]
    assert embedder.embed("a") == [0.1, 0.2]

    url = http.requests[0]["url"]
    assert "/openai/deployments/embed/embeddings" in url
    assert "api-version=" in url
    assert http.requests[0]["headers"]["api-key"] == "k"


# ----------------------------------------------------------- fhir stub search


def test_stub_search_rejects_an_unsupported_resource_type(fhir_store):
    with pytest.raises(KeyError):
        search(fhir_store, "Observation", {})


def test_stub_search_ignores_unknown_and_blank_parameters(fhir_store):
    everyone = search(fhir_store, "Patient", {"unsupported": "x", "identifier": ""})
    assert everyone["total"] == 3


def test_stub_search_matches_a_system_qualified_identifier(fhir_store):
    bundle = search(
        fhir_store,
        "Patient",
        {"identifier": "https://example.invalid/northwind/member-id|NW-100003"},
    )
    assert bundle["total"] == 1
