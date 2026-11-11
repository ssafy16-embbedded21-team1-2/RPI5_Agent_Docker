import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from rpi5_agent.api import create_app
from rpi5_agent.assessment import evaluate
from rpi5_agent.config import Settings
from rpi5_agent.errors import SummaryError
from rpi5_agent.evidence_summary import build_model_payload
from rpi5_agent.models import DiagnosticProfile, Scenario, ToolCall, ToolError, ToolResult
from rpi5_agent.tools.collection import collect_evidence
from rpi5_agent.tools.fixtures import FixtureProvider, collect_fixture


def codes(evidence):
    return {item.code for item in evaluate(evidence)}


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("ecu_unresponsive", "GATEWAY_EVIDENCE_UNAVAILABLE"),
        ("ecu_rebooted", "ECU_BOOT_CHANGED"),
        ("crash_recorded", "CRASH_RECORD_PRESENT"),
        ("crash_record_absent", "CRASH_RECORD_ABSENT"),
        ("crash_record_unsupported", "CRASH_RECORD_UNSUPPORTED"),
        ("crash_record_timeout", "CRASH_RECORD_TIMEOUT"),
        ("crash_record_invalid", "CRASH_RECORD_INVALID"),
        ("symbols_missing", "SYMBOLS_MISSING"),
        ("symbol_build_mismatch", "SYMBOL_BUILD_MISMATCH"),
        ("debug_access_unavailable", "DEBUG_ACCESS_UNAVAILABLE"),
    ],
)
def test_product_diagnoses_finish_without_swd(scenario, expected):
    settings = Settings(_env_file=None, llm_enabled=False)
    with TestClient(create_app(settings)) as client:
        response = client.post("/diagnose", json={"scenario": scenario})
    assert response.status_code == 200
    result = response.json()
    assert result["diagnostic_profile"] == "product"
    assert result["mode"] == "fixture"
    assert expected in {item["code"] for item in result["findings"]}
    if "crash_record_" in scenario and scenario != "crash_recorded":
        assert "CRASH_RECORD_PRESENT" not in {item["code"] for item in result["findings"]}


def test_evidence_order_and_extra_records_do_not_change_gateway_assessment():
    evidence = collect_fixture(Scenario.CRASH_RECORDED)
    assert codes(evidence) == codes(list(reversed(evidence)))
    assert {"GATEWAY_FRESH", "BMS_FRESH", "CRASH_RECORD_PRESENT"} <= codes(evidence)
    record = evidence[-1]
    assert record.running_build_id != record.record_build_id
    assert record.boot_id != record.capture_boot_id
    # Unknown current freshness does not invalidate a historical record.
    record.freshness = "stale"
    assert "CRASH_RECORD_PRESENT" in codes(evidence)


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "invalid", "negative", "type", "nan", "field_invalid"]
)
def test_missing_or_malformed_gateway_never_becomes_healthy(mutation):
    evidence = collect_fixture(Scenario.HEALTHY)
    gateway = evidence[1]
    if mutation == "missing":
        evidence.pop()
    elif mutation == "duplicate":
        evidence.append(gateway.model_copy(deep=True))
    elif mutation == "invalid":
        gateway.data = {}
    elif mutation == "negative":
        gateway.data["bms_age_ms"] = -1
    elif mutation == "type":
        gateway.data["bms_valid"] = "false"
    elif mutation == "field_invalid":
        gateway.field_validity["bms_age_ms"] = False
    else:
        gateway.data["gateway_age_ms"] = float("nan")
    assert "BMS_FRESH" not in codes(evidence)
    assert "GATEWAY_FRESH" not in codes(evidence)


def test_missing_bus_does_not_crash_or_discard_other_evidence():
    evidence = collect_fixture(Scenario.CRASH_RECORDED)[1:]
    assert {"LOCAL_ADAPTER_STATE_UNKNOWN", "CRASH_RECORD_PRESENT"} <= codes(evidence)
    assert "LOCAL_ADAPTER_BUS_OFF" not in codes(evidence)
    assert "GATEWAY_EVIDENCE_UNAVAILABLE" in codes([])


@pytest.mark.parametrize(
    "field", ["record_id", "record_build_id", "capture_boot_id", "capture_uptime_ms"]
)
def test_incomplete_record_context_does_not_confirm_crash(field):
    evidence = collect_fixture(Scenario.CRASH_RECORDED)
    setattr(evidence[-1], field, None)
    assert "CRASH_RECORD_INVALID" in codes(evidence)
    assert "CRASH_RECORD_PRESENT" not in codes(evidence)


def test_unknown_validity_and_bad_frame_do_not_produce_source_location():
    evidence = collect_fixture(Scenario.CRASH_RECORDED)
    evidence[-1].validity = "unknown"
    assert "CRASH_RECORD_UNVERIFIED" in codes(evidence)
    assert "CRASH_RECORD_PRESENT" not in codes(evidence)
    evidence[-1].validity = "valid"
    evidence[-1].field_validity["stacked_pc"] = False
    assert {"CRASH_RECORD_PRESENT", "CRASH_FRAME_INVALID"} <= codes(evidence)
    assert "CRASH_SOURCE_LOCATION_UNKNOWN" in codes(evidence)


def test_symbol_evidence_is_bound_to_record_and_build():
    evidence = collect_fixture(Scenario.SYMBOL_BUILD_MISMATCH)
    evidence[-1].record_id = "unrelated-record"
    assert "SYMBOL_EVIDENCE_MISMATCH" in codes(evidence)
    assert "SYMBOL_BUILD_MISMATCH" not in codes(evidence)


class SpyProvider(FixtureProvider):
    def __init__(self, tools):
        self.tools = tools
        self.executed = []

    def plan(self, scenario):
        return [ToolCall(tool=tool, target="cell2") for tool in self.tools]

    def read(self, call, scenario):
        self.executed.append(call.tool)
        raise TimeoutError("sensitive-provider-error")


@pytest.mark.parametrize("profile", list(DiagnosticProfile))
def test_reset_flash_and_unlock_are_never_executed(profile):
    provider = SpyProvider(["swd.halt", "swd.reset_halt", "fw.st_flash", "swd.unlock"])
    result = collect_evidence(provider, Scenario.HEALTHY, profile)
    assert not provider.executed
    assert all(item.status == "unavailable" for item in result)


def test_product_policy_blocks_swd_before_provider_but_bench_allows_read_once():
    provider = SpyProvider(["swd.read_fault", "can.read_crash_record"])
    result = collect_evidence(provider, Scenario.HEALTHY, DiagnosticProfile.PRODUCT)
    assert provider.executed == ["can.read_crash_record"]
    assert result[0].error.code == "debug_access_unavailable"
    assert result[0].error.reason == "profile_disabled"
    assert result[1].status == "timeout"
    assert "sensitive-provider-error" not in json.dumps([r.model_dump(mode="json") for r in result])
    provider.executed.clear()
    collect_evidence(provider, Scenario.HEALTHY, DiagnosticProfile.BENCH)
    assert provider.executed == ["swd.read_fault", "can.read_crash_record"]


@pytest.mark.parametrize("count", [2, 17])
def test_invalid_plan_does_no_reads_and_diagnosis_still_finishes(count):
    provider = SpyProvider(["can.read_crash_record"] * count)
    settings = Settings(_env_file=None, llm_enabled=False)
    with TestClient(create_app(settings, provider=provider)) as client:
        result = client.post("/diagnose", json={}).json()
    assert not provider.executed
    assert result["evidence"][0]["error"]["code"] == "collection_plan_failed"
    assert "BMS_FRESH" not in {item["code"] for item in result["findings"]}


def test_failed_optional_read_preserves_gateway_evidence():
    class PartialProvider(FixtureProvider):
        def read(self, call, scenario):
            if call.tool == "can.read_crash_record":
                raise OSError("sensitive-raw-error")
            return super().read(call, scenario)

    with TestClient(create_app(Settings(_env_file=None), provider=PartialProvider())) as client:
        result = client.post("/diagnose", json={"scenario": "crash_recorded"}).json()
    assert {"BMS_FRESH", "CRASH_RECORD_ERROR"} <= {item["code"] for item in result["findings"]}
    assert "sensitive-raw-error" not in json.dumps(result)


def test_nonfinite_provider_data_is_rejected_before_response_serialization():
    class InvalidProvider(FixtureProvider):
        def read(self, call, scenario):
            result = super().read(call, scenario)
            if call.tool == "gateway.snapshot":
                result.data["gateway_age_ms"] = float("nan")
            return result

    with TestClient(create_app(Settings(_env_file=None), provider=InvalidProvider())) as client:
        response = client.post("/diagnose", json={})
    assert response.status_code == 200
    assert response.json()["evidence"][1]["error"]["code"] == "collection_failed"
    assert "BMS_FRESH" not in {item["code"] for item in response.json()["findings"]}


def test_capabilities_report_policy_without_claiming_physical_support():
    with TestClient(create_app(Settings(_env_file=None))) as client:
        caps = client.get("/capabilities").json()
        assert caps["diagnostic_profile"] == "product"
        assert caps["physical_collection"] is False
        assert not any(tool.startswith("swd.") for tool in caps["allowed_read_tools"])
        assert len(client.get("/scenarios").json()["scenarios"]) == len(Scenario)
        assert client.post("/diagnose", json={"diagnostic_profile": "bench"}).status_code == 422


def test_model_payload_keeps_provenance_but_excludes_raw_logs_and_legacy_errors():
    evidence = collect_fixture(Scenario.CRASH_RECORDED)
    evidence[-1].data["raw_stack"] = "private-dump"
    evidence[-1].data["api_key"] = "private-key"
    evidence[-1].error = "sensitive-unstructured-error"
    payload = build_model_payload(evidence, evaluate(evidence))
    raw = json.dumps(payload)
    assert all(
        value not in raw
        for value in ["private-dump", "private-key", "sensitive-unstructured-error"]
    )
    record = payload["evidence"][-1]
    assert record["record_build_id"] != record["running_build_id"]
    assert record["capture_boot_id"] != record["boot_id"]
    assert record["source"] == "fixture"
    assert record["transport"] == "can_record"


@pytest.mark.parametrize("scenario", list(Scenario))
def test_all_fixture_model_contexts_fit_byte_guard(scenario):
    evidence = collect_fixture(scenario)
    build_model_payload(evidence, evaluate(evidence))


def test_oversized_model_context_is_rejected_without_silent_truncation():
    evidence = collect_fixture(Scenario.CRASH_RECORDED) * 16
    with pytest.raises(SummaryError, match="evidence_context_too_large"):
        build_model_payload(evidence, evaluate(evidence))


def test_evidence_contract_validates_timestamp_and_structured_error():
    with pytest.raises(ValidationError):
        ToolResult(
            tool="can.bus_state", target="rpi_adapter", status="ok", timestamp="2026-11-11T16:00:00"
        )
    with pytest.raises(ValidationError):
        ToolError(code="bad\ncode")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, diagnostic_profile="unknown")
