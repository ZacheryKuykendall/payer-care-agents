"""PHI boundary: redaction, outbound egress controls, and the audit chain."""

from __future__ import annotations

import json

import pytest

from payer_agents.phi.audit import (
    GENESIS_HASH,
    AuditLog,
    JsonlAuditSink,
    MemoryAuditSink,
    PhiLeakError,
)
from payer_agents.phi.egress import (
    EgressAction,
    EgressBlocked,
    EgressMode,
    EgressPolicy,
    EgressRule,
    GuardedTransport,
    load_egress_policy,
)
from payer_agents.phi.redaction import PhiCategory, Redactor

# --------------------------------------------------------------- redaction


def test_redacts_structured_identifiers():
    result = Redactor().redact(
        "My ID is NW-100001, SSN 123-45-6789, phone 555-867-5309, "
        "email a.b@example.invalid, DOB 03/11/1954, MRN: 4457821."
    )
    assert "NW-100001" not in result.text
    assert "123-45-6789" not in result.text
    assert "555-867-5309" not in result.text
    assert "a.b@example.invalid" not in result.text
    assert "03/11/1954" not in result.text
    assert set(result.counts) >= {
        PhiCategory.MEMBER_ID.value,
        PhiCategory.SSN.value,
        PhiCategory.PHONE.value,
        PhiCategory.EMAIL.value,
        PhiCategory.DATE_OF_BIRTH.value,
        PhiCategory.MRN.value,
    }
    assert result.redacted is True


def test_authorization_references_survive_redaction():
    """PA references look like identifiers but are plan artefacts, not PHI.
    Stripping them would break the prior-authorization flow for no privacy gain."""
    result = Redactor().redact("Status of PA-20260207 for member NW-100002?")
    assert "PA-20260207" in result.text
    assert "NW-100002" not in result.text


def test_tokens_are_stable_and_reversible_within_one_conversation():
    redactor = Redactor()
    first = redactor.redact("member NW-100001 called")
    second = redactor.redact("NW-100001 called again")
    assert first.spans[0].token == second.spans[0].token
    assert redactor.rehydrate(first.text) == "member NW-100001 called"
    assert redactor.lookup(first.spans[0].token) == "NW-100001"
    assert redactor.first_value(PhiCategory.MEMBER_ID) == "NW-100001"


def test_distinct_values_get_distinct_tokens():
    redactor = Redactor()
    result = redactor.redact("NW-100001 and NW-100002")
    tokens = {span.token for span in result.spans}
    assert len(tokens) == 2


def test_redaction_is_a_no_op_on_clean_text():
    result = Redactor().redact("What is my copay for a specialist visit?")
    assert result.redacted is False
    assert result.counts == {}
    assert Redactor().redact("").text == ""
    assert Redactor().first_value(PhiCategory.SSN) is None


def test_overlapping_patterns_resolve_to_the_most_specific():
    """An SSN also matches looser digit patterns; it must be tagged as an SSN once."""
    result = Redactor().redact("SSN 123-45-6789")
    assert len(result.spans) == 1
    assert result.spans[0].category is PhiCategory.SSN


def test_span_offsets_point_into_the_redacted_text():
    result = Redactor().redact("member NW-100001 called")
    span = result.spans[0]
    assert result.text[span.start : span.end] == span.token


# ------------------------------------------------------------------ egress


@pytest.fixture
def policy() -> EgressPolicy:
    return EgressPolicy(
        mode=EgressMode.ENFORCED,
        default_action=EgressAction.DENY,
        rules=[
            EgressRule(name="allow-gateway", host="apim.example.net", action=EgressAction.ALLOW),
            EgressRule(name="allow-search", host="*.search.windows.net", action=EgressAction.ALLOW),
            EgressRule(
                name="deny-backend", host="*.fhir.azurehealthcareapis.com", action=EgressAction.DENY
            ),
        ],
    )


@pytest.mark.parametrize(
    "url, allowed, rule",
    [
        ("https://apim.example.net/mcp", True, "allow-gateway"),
        ("https://x.search.windows.net/indexes", True, "allow-search"),
        ("https://acct.fhir.azurehealthcareapis.com/Patient", False, "deny-backend"),
        ("https://unlisted.example.org/collect", False, "<default>"),
    ],
)
def test_policy_evaluates_in_order_with_first_match_wins(policy, url, allowed, rule):
    decision = policy.evaluate(url)
    assert decision.allowed is allowed
    assert decision.rule_name == rule


def test_direct_backend_access_is_denied_even_though_the_host_is_legitimate(policy):
    """The control this architecture leans on: clinical data must traverse the
    gateway so the call is authenticated, rate limited and audited."""
    decision = policy.evaluate("https://acct.fhir.azurehealthcareapis.com/Patient")
    assert decision.allowed is False
    assert decision.would_deny is True


def test_audit_mode_records_a_would_deny_without_blocking(policy):
    policy.mode = EgressMode.AUDIT
    decision = policy.evaluate("https://unlisted.example.org/x")
    assert decision.allowed is True
    assert decision.would_deny is True
    assert decision.mode is EgressMode.AUDIT


def test_evaluation_fails_closed_on_an_unparsable_destination(policy):
    decision = policy.evaluate("not a url at all")
    assert decision.allowed is False
    assert "fails closed" in decision.reason


def test_a_missing_policy_file_yields_deny_all(tmp_path):
    fallback = load_egress_policy(tmp_path / "absent.json")
    assert fallback.default_action is EgressAction.DENY
    assert fallback.rules == []
    assert fallback.evaluate("https://anything.example.net").allowed is False
    # No argument means the shipped policy, not the fallback.
    assert load_egress_policy(None).name == "payer-agents-egress"


def test_rule_limit_matches_the_azure_cap():
    with pytest.raises(ValueError, match="at most 480"):
        EgressPolicy(rules=[EgressRule(name=f"r{i}", host=f"h{i}.x") for i in range(481)])


def test_arm_projection_keeps_content_safety_and_network_modes_separate(policy):
    properties = policy.to_arm_properties()
    assert properties["mode"] == "Blocking"  # content safety
    assert properties["egressPolicy"]["mode"] == "Enforced"  # network
    assert properties["egressPolicy"]["defaultAction"] == "Deny"
    assert properties["egressPolicy"]["rules"][0] == {
        "name": "allow-gateway",
        "ruleType": "Fqdn",
        "match": {"host": "apim.example.net"},
        "action": {"actionType": "Allow"},
    }


def test_shipped_policy_denies_the_destinations_it_claims_to():
    shipped = load_egress_policy()
    assert shipped.evaluate("https://api.openai.com/v1/chat").allowed is False
    assert shipped.evaluate("https://x.search.windows.net/q").allowed is True
    assert shipped.evaluate("https://a.fhir.azurehealthcareapis.com/Patient").allowed is False


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeHttp:
    def __init__(self):
        self.calls: list[str] = []
        self.closed = False

    def get(self, url, params=None, headers=None):
        self.calls.append(url)
        return _FakeResponse({"resourceType": "Bundle", "entry": []})

    def close(self):
        self.closed = True


def test_guarded_transport_blocks_before_the_request_leaves_the_process(policy):
    http = _FakeHttp()
    transport = GuardedTransport("https://acct.fhir.azurehealthcareapis.com", policy, inner=http)
    with pytest.raises(EgressBlocked) as excinfo:
        transport.get("Patient", {"identifier": "NW-100001"})
    assert http.calls == []
    assert excinfo.value.decision.rule_name == "deny-backend"
    assert len(transport.decisions) == 1


def test_guarded_transport_allows_and_records_a_permitted_destination(policy):
    http = _FakeHttp()
    seen = []
    transport = GuardedTransport(
        "https://apim.example.net/fhir", policy, inner=http, on_decision=seen.append
    )
    assert transport.get("Patient")["resourceType"] == "Bundle"
    assert http.calls == ["https://apim.example.net/fhir/Patient"]
    assert seen[0].allowed is True
    transport.close()
    assert http.closed is False  # the transport does not own an injected client


def test_each_transport_keeps_its_own_decision_log(policy):
    first = GuardedTransport("https://apim.example.net", policy, inner=_FakeHttp())
    second = GuardedTransport("https://apim.example.net", policy, inner=_FakeHttp())
    first.get("Patient")
    assert len(first.decisions) == 1
    assert second.decisions == []


# ------------------------------------------------------------------- audit


def test_records_are_hash_chained_and_verifiable():
    log = AuditLog([MemoryAuditSink()])
    log.record("routing", "classified", intent="benefits_coverage")
    log.record("gate", "evaluated", outcome="allow")
    assert log.verify() is True
    assert log.events[0].previous_hash == GENESIS_HASH
    assert log.events[1].previous_hash == log.events[0].hash
    assert [e.sequence for e in log.events] == [1, 2]


def test_tampering_with_a_record_breaks_the_chain():
    log = AuditLog([MemoryAuditSink()])
    log.record("routing", "classified", intent="benefits_coverage")
    log.record("gate", "evaluated")
    log.events[0].detail["intent"] = "prior_auth_status"
    assert log.verify() is False


def test_removing_a_record_breaks_the_chain():
    log = AuditLog([MemoryAuditSink()])
    for index in range(3):
        log.record("x", f"a{index}")
    del log.events[1]
    assert log.verify() is False


def test_the_log_refuses_unredacted_phi():
    log = AuditLog([MemoryAuditSink()])
    with pytest.raises(PhiLeakError, match="member_id"):
        log.record("tool", "lookup", member_id="NW-100001")
    with pytest.raises(PhiLeakError, match="ssn"):
        log.record("tool", "lookup", note="patient ssn 123-45-6789")


def test_strict_mode_can_be_disabled_for_trusted_callers():
    log = AuditLog([MemoryAuditSink()], strict=False)
    log.record("tool", "lookup", member_id="NW-100001")
    assert log.events[0].detail["member_id"] == "NW-100001"


def test_records_fan_out_to_every_sink_and_survive_a_round_trip(tmp_path):
    path = tmp_path / "audit.jsonl"
    memory = MemoryAuditSink()
    log = AuditLog([memory, JsonlAuditSink(path)])
    log.record("response", "delivered", citation_count=3)
    log.close()

    assert len(memory) == 1
    assert next(iter(memory)).category == "response"
    written = json.loads(path.read_text(encoding="utf-8").strip())
    assert written["detail"]["citation_count"] == 3
    assert written["hash"] == log.events[0].hash
    assert log.memory is memory


def test_as_dicts_is_json_serialisable():
    log = AuditLog()
    log.record("request", "received", channel="member_portal")
    assert json.dumps(log.as_dicts())
    assert log.memory is not None
