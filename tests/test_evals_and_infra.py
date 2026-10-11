"""Evaluation harness, demo scenarios, and the infrastructure that must stay in sync."""

from __future__ import annotations

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

from payer_agents.config import DATA_ROOT, REPO_ROOT
from payer_agents.demo_scenarios import SCENARIOS
from payer_agents.domain import Intent, MemberRequest
from payer_agents.evals.runner import EvalCase, load_cases, run_suite
from payer_agents.phi.egress import load_egress_policy

INFRA = REPO_ROOT / "infra"
POLICIES = INFRA / "apim" / "policies"


# ------------------------------------------------------------------- cases


def test_case_file_covers_every_intent_including_the_abstention():
    cases = load_cases()
    assert len(cases) >= 30
    assert {case.expected_intent for case in cases} == set(Intent)


def test_case_file_includes_adversarial_and_ambiguous_traffic():
    """A suite of only well-formed questions measures nothing useful about a gate."""
    cases = load_cases()
    human_expected = [case for case in cases if case.expect_human]
    assert len(human_expected) >= 10
    assert len(human_expected) / len(cases) > 0.2


def test_case_ids_are_unique():
    ids = [case.id for case in load_cases()]
    assert len(ids) == len(set(ids))


# ----------------------------------------------------------------- harness


@pytest.fixture(scope="module")
def report(orchestrator):
    return run_suite(orchestrator)


def test_routing_accuracy_holds_on_the_labelled_set(report):
    assert report.sample_size == len(load_cases())
    assert report.routing_accuracy >= 0.90


def test_every_case_that_needs_a_human_gets_one(report):
    """The metric that matters most. A missed escalation is not a rounding error."""
    missed = [outcome for outcome in report.outcomes if outcome.expect_human and outcome.automated]
    assert missed == []
    assert report.human_routing_accuracy == 1.0


def test_automation_rate_is_meaningful_in_both_directions(report):
    # High enough to be worth building; low enough that the gates clearly fire.
    assert 0.4 <= report.automation_rate <= 0.8


def test_answers_that_are_shown_are_well_grounded(report):
    assert report.mean_grounding_score is not None
    assert report.mean_grounding_score >= 2.5


def test_report_includes_calibration_and_a_threshold_sweep(report):
    assert report.calibration.sample_size == report.sample_size
    assert len(report.threshold_points) == 11
    assert report.threshold_points[0].automation_rate == 1.0
    assert report.threshold_points[-1].automation_rate == 0.0


def test_report_confusion_matrix_is_keyed_by_expected_intent(report):
    for expected, predictions in report.confusion.items():
        assert expected in {intent.value for intent in Intent}
        assert sum(predictions.values()) > 0


def test_report_refuses_to_present_itself_as_a_benchmark(report):
    joined = " ".join(report.notes).lower()
    assert "not microsoft-decision-1" in joined or "not a benchmark" in joined
    assert any("synthetic" in note.lower() for note in report.notes)


def test_summary_lines_are_printable(report):
    assert any("routing accuracy" in line for line in report.summary_lines())


def test_harness_runs_on_a_custom_case_list(orchestrator):
    custom = [
        EvalCase(
            id="c1", text="What is my specialist copay?", expected_intent=Intent.BENEFITS_COVERAGE
        )
    ]
    assert run_suite(orchestrator, custom).sample_size == 1


# --------------------------------------------------------------- scenarios


def test_demo_scenarios_exercise_both_outcomes(orchestrator):
    outcomes = {
        scenario.key: orchestrator.handle(
            MemberRequest(text=scenario.text, channel=scenario.channel)
        ).automated
        for scenario in SCENARIOS
    }
    assert outcomes["benefits"] is True
    assert outcomes["policy"] is True
    assert outcomes["prior_auth"] is True
    assert outcomes["emergency"] is False
    assert outcomes["clinical_advice"] is False
    assert outcomes["injection"] is False
    assert outcomes["ambiguous"] is False


def test_scenario_keys_are_unique_and_documented():
    keys = [scenario.key for scenario in SCENARIOS]
    assert len(keys) == len(set(keys))
    assert all(len(scenario.teaches) > 40 for scenario in SCENARIOS)


# ------------------------------------------------------- synthetic data only


def test_no_bundle_claims_to_be_anything_but_synthetic():
    for path in (DATA_ROOT / "fhir" / "bundles").glob("*.json"):
        bundle = json.loads(path.read_text(encoding="utf-8"))
        tags = {tag["code"] for tag in bundle["meta"]["tag"]}
        assert "synthetic" in tags, path.name


def test_every_corpus_document_carries_a_synthetic_banner():
    for path in (DATA_ROOT / "corpus").rglob("*.md"):
        body = path.read_text(encoding="utf-8")
        assert "SYNTHETIC" in body, f"{path.name} is missing its synthetic-data banner"


def test_corpus_front_matter_is_complete():
    for path in (DATA_ROOT / "corpus").rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        front = yaml.safe_load(text.split("---")[1])
        assert {"doc_id", "title", "doc_type"} <= set(front), path.name
        assert front["doc_type"] in {"benefits", "formulary", "policy", "utilization_management"}


def test_example_hostnames_are_reserved_for_documentation():
    """RFC 2606 keeps a stray copy-paste from pointing at a real domain."""
    for path in (DATA_ROOT / "corpus").rglob("*.md"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "https://" in line and "docs.example.invalid" not in line:
                assert "example.invalid" in line or "learn.microsoft.com" in line, line


# ------------------------------------------------------------------ infra


def test_generated_egress_rules_match_the_policy_document():
    """One policy, two enforcement points. This is the test that keeps them one."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "export_egress_policy.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_generated_rules_are_in_the_arm_shape():
    rules = json.loads((INFRA / "generated" / "egress-rules.json").read_text(encoding="utf-8"))
    policy = load_egress_policy()
    assert len(rules) == len(policy.rules)
    assert rules[0]["ruleType"] == "Fqdn"
    assert set(rules[0]) == {"name", "ruleType", "match", "action"}


def test_export_script_can_emit_the_full_arm_properties(tmp_path, capsys):
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import export_egress_policy

    assert export_egress_policy.main(["--format", "arm", "--stdout"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["properties"]["egressPolicy"]["defaultAction"] == "Deny"
    assert payload["properties"]["mode"] == "Blocking"


def test_export_check_fails_when_the_generated_file_is_missing(tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import export_egress_policy

    monkeypatch.setattr(export_egress_policy, "RULES_FILE", tmp_path / "absent.json")
    assert export_egress_policy.main(["--check"]) == 1


@pytest.mark.parametrize("name", ["global.xml", "mcp-server.xml", "fhir-api.xml"])
def test_apim_policies_are_well_formed_xml(name):
    root = ET.parse(POLICIES / name).getroot()
    assert root.tag == "policies"
    assert {child.tag for child in root} == {"inbound", "backend", "outbound", "on-error"}


def test_mcp_policy_enforces_the_controls_the_simulator_claims():
    """Parity check against src/payer_agents/gateway/apim_sim.py."""
    xml = (POLICIES / "mcp-server.xml").read_text(encoding="utf-8")
    for element in (
        "validate-azure-ad-token",
        "check-header",
        "llm-content-safety",
        "rate-limit-by-key",
        "quota-by-key",
        "authentication-managed-identity",
        "log-to-eventhub",
    ):
        assert f"<{element}" in xml, element
    # Rejections are logged too: the on-error section must emit, not just outbound.
    assert xml.count("<log-to-eventhub") == 2


def test_mcp_policy_rate_limits_per_agent_not_per_ip():
    xml = (POLICIES / "mcp-server.xml").read_text(encoding="utf-8")
    assert "counter-key='@((string)context.Variables[\"agentId\"])'" in xml
    assert "context.Request.IpAddress" not in xml


def test_fhir_policy_is_read_only_and_member_scoped():
    xml = (POLICIES / "fhir-api.xml").read_text(encoding="utf-8")
    assert 'context.Request.Method != "GET"' in xml
    assert "member_scope" in xml
    assert "405" in xml and "403" in xml


def test_global_policy_stamps_a_correlation_id_and_logs_no_payloads():
    """Response-body logging at the All APIs scope breaks MCP transport, and in a
    payer context it writes clinical payloads into a diagnostic store."""
    root = ET.parse(POLICIES / "global.xml").getroot()
    elements = {child.tag for section in root for child in section}
    assert "set-header" in elements
    assert not elements & {"log-to-eventhub", "set-body", "trace"}
    assert "x-correlation-id" in (POLICIES / "global.xml").read_text(encoding="utf-8")


def test_bicep_files_exist_and_reference_their_modules():
    main = (INFRA / "main.bicep").read_text(encoding="utf-8")
    for module in (
        "monitoring",
        "foundry",
        "rai-egress-policy",
        "search",
        "fhir",
        "apim",
        "key-vault",
        "rbac",
    ):
        assert f"modules/{module}.bicep" in main, module
        assert (INFRA / "modules" / f"{module}.bicep").exists()


def test_bicep_loads_the_policy_xml_rather_than_inlining_it():
    apim = (INFRA / "modules" / "apim.bicep").read_text(encoding="utf-8")
    assert "loadTextContent('../apim/policies/global.xml')" in apim
    assert "loadTextContent('../apim/policies/fhir-api.xml')" in apim


def test_egress_module_is_labelled_preview_and_separates_the_two_modes():
    module = (INFRA / "modules" / "rai-egress-policy.bicep").read_text(encoding="utf-8")
    assert "PREVIEW" in module
    assert "2026-05-15-preview" in module
    assert "properties.egressPolicy.mode" in module


def test_bicepparam_loads_the_generated_rules():
    param = (INFRA / "main.bicepparam").read_text(encoding="utf-8")
    assert "loadJsonContent('generated/egress-rules.json')" in param
    assert "param egressMode = 'Audit'" in param


def test_infrastructure_defaults_to_entra_auth_not_keys():
    for name in ("foundry.bicep", "search.bicep"):
        module = (INFRA / "modules" / name).read_text(encoding="utf-8")
        assert "disableLocalAuth: true" in module


def test_rbac_grants_the_gateway_read_only_access_to_fhir():
    rbac = (INFRA / "modules" / "rbac.bicep").read_text(encoding="utf-8")
    assert "fhirDataReader" in rbac
    assert "fhirDataWriter" not in rbac
    assert "fhirDataContributor" not in rbac


def test_repository_ships_no_real_credentials():
    # Assembled at runtime so this file does not trip its own scan.
    suspicious = ("BEGIN " + "PRIVATE KEY", "BEGIN RSA " + "PRIVATE KEY", "Account" + "Key=")
    skip_dirs = {".git", ".venv", "__pycache__", ".pytest_cache", "htmlcov"}
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or skip_dirs & set(path.parts):
            continue
        if path == Path(__file__) or path.suffix in {".png", ".jpg", ".ico", ".woff2"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for marker in suspicious:
            assert marker not in text, f"{path} contains {marker}"


def test_docs_label_every_preview_feature():
    status = (REPO_ROOT / "docs" / "PREVIEW-STATUS.md").read_text(encoding="utf-8")
    for feature in (
        "egress",
        "Agent 365",
        "agent-framework-foundry-hosting",
        "Microsoft-Decision-1",
    ):
        assert feature.lower() in status.lower(), feature


def test_readme_has_the_sections_a_reviewer_looks_for():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for heading in (
        "Run the demo",
        "What this does for the business",
        "Governance",
        "Integration patterns",
        "Architecture",
        "Design decisions and trade-offs",
        "Interview walkthrough",
    ):
        assert heading in readme, heading
    assert "```mermaid" in readme


def test_every_doc_referenced_by_the_readme_exists():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for token in readme.split("]("):
        target = token.split(")")[0].strip()
        if target.startswith(("http", "#", "mailto:")) or not target:
            continue
        assert (REPO_ROOT / Path(target.split("#")[0])).exists(), target
