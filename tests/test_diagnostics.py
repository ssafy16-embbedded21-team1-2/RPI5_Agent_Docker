from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient

from rpi5_agent.api import create_app
from rpi5_agent.config import Settings
from rpi5_agent.models import LLMMetadata, ModelExplanation, Scenario, SummaryResult
from rpi5_agent.tools.fixtures import collect_fixture
from rpi5_agent.workflow import evaluate


def config(**kwargs):
    return Settings(_env_file=None, **kwargs)


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("healthy", {"GATEWAY_FRESH", "BMS_FRESH"}),
        ("bms_timeout", {"GATEWAY_FRESH", "BMS_STALE"}),
        ("bus_off", {"LOCAL_ADAPTER_BUS_OFF", "REMOTE_STATE_UNKNOWN"}),
    ],
)
def test_scenarios_keep_gateway_and_bms_states_distinct(scenario, expected):
    with TestClient(create_app(config(llm_enabled=False))) as client:
        response = client.post("/diagnose", json={"scenario": scenario})
    assert response.status_code == 200
    result = response.json()
    assert {item["code"] for item in result["findings"]} == expected
    assert result["mode"] == "fixture"
    assert result["llm_status"] == "disabled"
    assert all(item["source"] == "fixture" for item in result["evidence"])
    assert result["timestamp"].endswith("Z")


def test_unknown_error_counters_are_not_zero():
    evidence = collect_fixture(Scenario.BUS_OFF)
    assert evidence[0].data["tec"] is None
    assert evidence[0].data["rec"] is None
    assert evidence[1].status == "unavailable"


def test_invalid_and_stale_values_are_distinct():
    evidence = collect_fixture(Scenario.HEALTHY)
    evidence[1].data["bms_valid"] = False
    codes = {item.code for item in evaluate(evidence)}
    assert "BMS_INVALID" in codes
    assert "BMS_FRESH" not in codes
    assert "BMS_STALE" not in codes


def test_old_gateway_does_not_imply_fresh_bms():
    evidence = collect_fixture(Scenario.HEALTHY)
    evidence[1].data["gateway_age_ms"] = 501
    assert {item.code for item in evaluate(evidence)} == {"GATEWAY_STALE"}


def test_unknown_scenario_and_control_fields_are_rejected():
    with TestClient(create_app(config(llm_enabled=False))) as client:
        assert client.post("/diagnose", json={"scenario": "real_can"}).status_code == 422
        assert client.post("/diagnose", json={"command": "reset"}).status_code == 422
        assert client.post("/diagnose", json={"use_llm": True}).status_code == 409


def test_llm_failure_preserves_findings_and_does_not_leak_exception_details():
    class BrokenModel:
        def summarize(self, evidence, findings, *, target="auto"):
            raise TimeoutError("sensitive-key-not-for-response")

    with TestClient(create_app(config(llm_enabled=True), BrokenModel())) as client:
        response = client.post("/diagnose", json={"use_llm": True})
    result = response.json()
    assert result["llm_status"] == "unavailable"
    assert result["llm_error"] == "TimeoutError"
    assert "BMS_STALE" in {item["code"] for item in result["findings"]}
    assert "sensitive-key" not in response.text


def test_busy_agent_rejects_concurrent_diagnoses_and_recovers():
    entered, release = Event(), Event()

    class SlowModel:
        def summarize(self, evidence, findings, *, target="auto"):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test release not received")
            return SummaryResult(
                explanation=ModelExplanation(
                    hypothesis="BMS 송신 지연 가능성이 있습니다.",
                    next_check="BMS 송신 기록을 확인하세요.",
                ),
                metadata=LLMMetadata(finish_reason="stop"),
            )

    with TestClient(create_app(config(llm_enabled=True), SlowModel())) as client:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(client.post, "/diagnose", json={"use_llm": True})
            try:
                assert entered.wait(3)
                assert client.post("/diagnose", json={}).status_code == 429
            finally:
                release.set()
            assert pending.result(timeout=5).json()["llm_status"] == "generated"
        assert client.post("/diagnose", json={}).status_code == 200


def test_health_does_not_claim_model_or_hardware_readiness():
    with TestClient(create_app(config(llm_enabled=False))) as client:
        assert client.get("/health").json() == {
            "status": "ok",
            "mode": "fixture",
            "llm_enabled": False,
        }
