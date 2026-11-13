import json

import pytest
from fastapi.testclient import TestClient

from rpi5_agent.api import create_app
from rpi5_agent.assessment import evaluate
from rpi5_agent.config import Settings
from rpi5_agent.evidence_summary import build_model_payload
from rpi5_agent.models import (
    DiagnoseRequest,
    DiagnosticProfile,
    Finding,
    LLMMetadata,
    ModelExplanation,
    Scenario,
    SummaryResult,
)
from rpi5_agent.tools.fixtures import FixtureProvider, collect_fixture
from rpi5_agent.verification import verify_fixture
from rpi5_agent.workflow import build_workflow


@pytest.mark.parametrize("profile", list(DiagnosticProfile))
@pytest.mark.parametrize("scenario", list(Scenario))
def test_reviewed_fixture_cases_pass_in_both_read_profiles(profile, scenario):
    settings = Settings(_env_file=None, diagnostic_profile=profile)
    with TestClient(create_app(settings)) as client:
        result = client.post("/verify", json={"scenario": scenario}).json()
    assert result["verdict"] == "passed"
    assert result["scope"] == "fixture_findings"
    assert result["physical_validation"] is False
    assert result["llm_semantics"] == "not_evaluated"
    assert result["diagnosis"]["assessment_specification_id"] == "fixture-bench-v1"
    assert not result["missing_finding_codes"]
    assert not result["unexpected_finding_codes"]


def test_missing_unexpected_and_duplicate_findings_fail_the_case():
    diagnosis = build_workflow().invoke({"request": DiagnoseRequest(scenario="healthy")})["result"]
    diagnosis.findings = [
        Finding(code="BMS_STALE", severity="warning", message="deliberately wrong result"),
    ] * 2
    result = verify_fixture(diagnosis)
    assert result.verdict == "failed"
    assert result.missing_finding_codes == ["BMS_FRESH", "GATEWAY_FRESH"]
    assert result.unexpected_finding_codes == ["BMS_STALE"]
    assert result.duplicate_finding_codes == ["BMS_STALE"]
    diagnosis.assessment_specification_id = "another-bench-v2"
    assert verify_fixture(diagnosis).verdict == "not_evaluated"


@pytest.mark.parametrize(
    ("gateway_age", "bms_age", "expected"),
    [
        (500, 1000, {"GATEWAY_FRESH", "BMS_FRESH"}),
        (500, 1001, {"GATEWAY_FRESH", "BMS_STALE"}),
        (501, 0, {"GATEWAY_STALE"}),
    ],
)
def test_timeouts_use_reviewed_limits_not_provider_fields(gateway_age, bms_age, expected):
    evidence = collect_fixture(Scenario.HEALTHY)
    evidence[1].data.update(
        gateway_age_ms=gateway_age,
        bms_age_ms=bms_age,
        gateway_timeout_ms=10**9,
        bms_timeout_ms=10**9,
    )
    findings = evaluate(evidence)
    assert {item.code for item in findings} == expected
    payload = build_model_payload(evidence, findings)
    assert payload["assessment_specification"]["gateway_timeout_ms"] == 500
    assert payload["assessment_specification"]["bms_timeout_ms"] == 1000
    assert "timeout_ms" not in json.dumps(payload["evidence"][1]["data"])


def test_custom_provider_has_no_implicit_fixture_acceptance_suite():
    with TestClient(create_app(Settings(_env_file=None), provider=FixtureProvider())) as client:
        assert client.get("/verification/specification").json()["available"] is False
        assert client.get("/capabilities").json()["fixture_verification"] is False
        assert client.post("/verify", json={}).status_code == 409
        assert client.post("/diagnose", json={}).status_code == 200


def test_fixture_catalog_and_request_cannot_override_the_standard():
    with TestClient(create_app(Settings(_env_file=None))) as client:
        catalog = client.get("/verification/specification").json()
        assert catalog["available"] is True
        assert {c["case_id"] for c in catalog["cases"]} == {s.value for s in Scenario}
        assert client.post("/verify", json={"expected_finding_codes": []}).status_code == 422
        assert client.post("/verify", json={"bms_timeout_ms": 999999}).status_code == 422
        assert client.post("/verify", json={"use_llm": True}).status_code == 409


def test_case_pass_does_not_claim_llm_semantic_accuracy_or_leak_expected_labels():
    class WrongSummary:
        def summarize(self, evidence, findings, *, target="auto"):
            payload = build_model_payload(evidence, findings)
            assert "scenario" not in payload
            assert "expected_finding_codes" not in json.dumps(payload)
            assert "fixture-acceptance-v1" not in json.dumps(payload)
            return SummaryResult(
                explanation=ModelExplanation(
                    hypothesis="현재 ECU가 고장났습니다.", next_check="고장을 확인하세요."
                ),
                metadata=LLMMetadata(finish_reason="stop"),
            )

    with TestClient(
        create_app(Settings(_env_file=None, llm_enabled=True), WrongSummary())
    ) as client:
        result = client.post("/verify", json={"scenario": "healthy", "use_llm": True}).json()
    assert result["verdict"] == "passed"
    assert result["diagnosis"]["llm_status"] == "generated"
    assert result["llm_semantics"] == "not_evaluated"
