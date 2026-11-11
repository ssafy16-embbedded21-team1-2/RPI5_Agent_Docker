import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from fastapi.testclient import TestClient

from rpi5_agent.api import create_app
from rpi5_agent.config import Settings

VALID_EXPLANATION = {
    "hypothesis": "BMS 송신 또는 중계 경로에 문제가 있을 수 있습니다.",
    "next_check": "BMS 송신 기록과 게이트웨이 수신 기록을 비교하세요.",
}


def start_model_server():
    requests = []
    reply = {
        "content": json.dumps(VALID_EXPLANATION, ensure_ascii=False),
        "finish_reason": "stop",
        "http_status": 200,
    }

    class ModelHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            reply["headers"] = {name.lower(): value for name, value in self.headers.items()}
            requests.append(
                (self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            )
            time.sleep(reply.get("delay_seconds", 0))
            payload = {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": "test-qwen",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": reply["finish_reason"],
                        "message": {"role": "assistant", "content": reply["content"]},
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 48, "total_tokens": 168},
            }
            if reply["http_status"] != 200:
                payload = {
                    "error": {
                        "message": "sensitive-backend-detail",
                        "type": "invalid_request_error",
                        "code": "unsupported_response_format",
                    }
                }
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(reply["http_status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except ConnectionError:
                pass  # The timeout test deliberately disconnects before the response.

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    worker.start()
    return (f"http://127.0.0.1:{server.server_port}/v1", requests, reply), server, worker


@pytest.fixture
def model_servers():
    servers = []

    def create():
        result, server, worker = start_model_server()
        servers.append((server, worker))
        return result

    try:
        yield create
    finally:
        for server, worker in servers:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)


@pytest.fixture
def model_server(model_servers):
    return model_servers()


def request_diagnosis(base_url):
    settings = Settings(
        _env_file=None,
        llm_enabled=True,
        llm_model="test-qwen",
        llm_base_url=base_url,
        llm_timeout_seconds=5,
    )
    with TestClient(create_app(settings)) as client:
        fallback = client.post("/diagnose", json={"scenario": "bms_timeout"}).json()
        response = client.post("/diagnose", json={"scenario": "bms_timeout", "use_llm": True})
    assert response.status_code == 200
    return response.json(), fallback


def test_langchain_requests_schema_and_preserves_code_generated_observations(model_server):
    base_url, requests, _ = model_server
    result, fallback = request_diagnosis(base_url)
    assert result["llm_status"] == "generated"
    assert result["llm_error"] is None
    assert result["explanation"] == VALID_EXPLANATION
    assert result["llm_metadata"] == {
        "finish_reason": "stop",
        "input_tokens": 120,
        "output_tokens": 48,
    }
    assert result["summary"] == (
        fallback["summary"]
        + "\n원인 후보: "
        + VALID_EXPLANATION["hypothesis"]
        + "\n추가 확인: "
        + VALID_EXPLANATION["next_check"]
    )
    assert {item["code"] for item in result["findings"]} == {"GATEWAY_FRESH", "BMS_STALE"}
    path, payload = requests[0]
    assert len(requests) == 1
    assert path == "/v1/chat/completions"
    assert payload["model"] == "test-qwen"
    assert "tools" not in payload
    assert payload.get("max_tokens", payload.get("max_completion_tokens")) == 256
    response_format = payload["response_format"]
    assert response_format["type"] == "json_schema"
    schema = response_format["json_schema"]["schema"]
    assert response_format["json_schema"]["strict"] is True
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"hypothesis", "next_check"}
    assert schema["properties"]["hypothesis"]["maxLength"] == 50
    evidence = json.loads(payload["messages"][1]["content"])["evidence"]
    assert "timestamp" not in evidence[0]


@pytest.mark.parametrize(
    ("content", "finish_reason", "error"),
    [
        (json.dumps(VALID_EXPLANATION), "length", "output_truncated"),
        ('{"hypothesis":', "length", "output_truncated"),
        (json.dumps(VALID_EXPLANATION), "content_filter", "unexpected_finish_reason"),
        (json.dumps(VALID_EXPLANATION), None, "unexpected_finish_reason"),
        ("plain text instead of JSON", "stop", "invalid_model_output"),
        ('{"hypothesis":', "stop", "invalid_model_output"),
        ("", "stop", "invalid_model_output"),
        ("{}", "stop", "invalid_model_output"),
        (json.dumps({**VALID_EXPLANATION, "command": "reset"}), "stop", "invalid_model_output"),
        (
            json.dumps({**VALID_EXPLANATION, "hypothesis": "가" * 51}),
            "stop",
            "invalid_model_output",
        ),
        (json.dumps({**VALID_EXPLANATION, "hypothesis": "   "}), "stop", "invalid_model_output"),
        (json.dumps({**VALID_EXPLANATION, "next_check": 123}), "stop", "invalid_model_output"),
        (
            json.dumps({**VALID_EXPLANATION, "next_check": "첫 줄\n둘째 줄"}),
            "stop",
            "invalid_model_output",
        ),
    ],
)
def test_incomplete_or_invalid_model_output_falls_back(model_server, content, finish_reason, error):
    base_url, requests, reply = model_server
    reply.update(content=content, finish_reason=finish_reason)
    result, fallback = request_diagnosis(base_url)
    assert len(requests) == 1
    assert result["llm_status"] == "unavailable"
    assert result["llm_error"] == error
    assert result["explanation"] is None
    assert result["summary"] == fallback["summary"]
    assert result["findings"] == fallback["findings"]
    assert result["llm_metadata"]["finish_reason"] == finish_reason
    assert result["llm_metadata"]["output_tokens"] == 48


def test_unsupported_schema_is_reported_without_retry_or_unconstrained_fallback(model_server):
    base_url, requests, reply = model_server
    reply["http_status"] = 400
    result, fallback = request_diagnosis(base_url)
    assert len(requests) == 1
    assert result["llm_status"] == "unavailable"
    assert result["llm_error"] in {"BadRequestError", "OpenAIInvalidRequestError"}
    assert result["llm_metadata"] is None
    assert result["summary"] == fallback["summary"]
    assert "sensitive-backend-detail" not in json.dumps(result)


def routed_settings(local_url, external_url, **kwargs):
    return Settings(
        _env_file=None,
        llm_enabled=True,
        llm_base_url=local_url,
        llm_model="test-local",
        external_llm_enabled=True,
        external_llm_base_url=external_url,
        external_llm_model="test-pamout",
        external_llm_api_key="test-private-key",
        external_llm_user_agent="diagnostic-test/1.0",
        **kwargs,
    )


def diagnose_with_routing(settings, **kwargs):
    with TestClient(create_app(settings)) as client:
        fallback = client.post(
            "/diagnose", json={"scenario": kwargs.get("scenario", "bms_timeout")}
        )
        response = client.post("/diagnose", json={"use_llm": True, **kwargs})
    assert response.status_code == 200
    return response.json(), fallback.json()


@pytest.mark.parametrize(
    ("scenario", "target", "backend", "reason"),
    [
        ("healthy", "auto", "local", "local_default"),
        ("bms_timeout", "auto", "local", "local_default"),
        ("bus_off", "auto", "pamout", "error_findings"),
        ("bus_off", "local", "local", "explicit_local"),
        ("bms_timeout", "external", "pamout", "explicit_external"),
    ],
)
def test_routing_uses_explicit_target_or_deterministic_findings(
    model_servers, scenario, target, backend, reason
):
    local_url, local_requests, _ = model_servers()
    external_url, external_requests, external_reply = model_servers()
    result, fallback = diagnose_with_routing(
        routed_settings(local_url, external_url), scenario=scenario, llm_target=target
    )
    assert result["findings"] == fallback["findings"]
    assert result["llm_status"] == "generated"
    assert result["llm_routing"]["backend"] == backend
    assert result["llm_routing"]["reason"] == reason
    assert result["llm_routing"]["requested"] == target
    assert len(result["llm_routing"]["attempts"]) == 1
    assert len(local_requests) == (1 if backend == "local" else 0)
    assert len(external_requests) == (1 if backend == "pamout" else 0)
    if backend == "pamout":
        path, payload = external_requests[0]
        assert path == "/v1/chat/completions"
        assert payload["model"] == "test-pamout"
        assert "response_format" not in payload
        assert "tools" not in payload
        assert payload.get("max_tokens", payload.get("max_completion_tokens")) == 4096
        assert external_reply["headers"]["authorization"] == "Bearer test-private-key"
        assert external_reply["headers"]["user-agent"] == "diagnostic-test/1.0"
    assert "test-private-key" not in json.dumps(result)
    assert "http://" not in json.dumps(result["llm_routing"])


@pytest.mark.parametrize("failure", ["format", "length", "server"])
def test_auto_escalates_local_failure_once_and_records_both_attempts(model_servers, failure):
    local_url, local_requests, local_reply = model_servers()
    external_url, external_requests, _ = model_servers()
    if failure == "format":
        local_reply["content"] = "not JSON"
    elif failure == "length":
        local_reply["finish_reason"] = "length"
    else:
        local_reply["http_status"] = 500
    result, fallback = diagnose_with_routing(routed_settings(local_url, external_url))
    assert result["findings"] == fallback["findings"]
    assert result["llm_status"] == "generated"
    assert result["llm_error"] is None
    route = result["llm_routing"]
    assert route["reason"] == "local_failure"
    assert route["backend"] == "pamout"
    assert [item["backend"] for item in route["attempts"]] == ["local", "pamout"]
    assert [item["status"] for item in route["attempts"]] == ["unavailable", "generated"]
    assert len(local_requests) == len(external_requests) == 1


@pytest.mark.parametrize("target", ["auto", "local"])
def test_local_only_or_disabled_escalation_never_calls_external(model_servers, target):
    local_url, local_requests, local_reply = model_servers()
    external_url, external_requests, _ = model_servers()
    local_reply["content"] = "not JSON"
    settings = routed_settings(
        local_url, external_url, external_llm_fallback_on_local_failure=False
    )
    result, fallback = diagnose_with_routing(settings, llm_target=target)
    assert result["llm_status"] == "unavailable"
    assert result["summary"] == fallback["summary"]
    assert len(local_requests) == 1
    assert not external_requests


def test_explicit_local_does_not_escalate_even_if_fallback_is_enabled(model_servers):
    local_url, local_requests, local_reply = model_servers()
    external_url, external_requests, _ = model_servers()
    local_reply["http_status"] = 500
    result, _ = diagnose_with_routing(
        routed_settings(local_url, external_url), scenario="bus_off", llm_target="local"
    )
    assert result["llm_status"] == "unavailable"
    assert len(local_requests) == 1
    assert not external_requests


@pytest.mark.parametrize(
    ("content", "finish_reason", "http_status"),
    [
        ("not JSON", "stop", 200),
        (json.dumps(VALID_EXPLANATION), "length", 200),
        (json.dumps(VALID_EXPLANATION), None, 200),
        (json.dumps(VALID_EXPLANATION), "stop", 429),
        (json.dumps(VALID_EXPLANATION), "stop", 500),
    ],
)
def test_external_failure_preserves_findings_and_never_loops_back(
    model_servers, content, finish_reason, http_status
):
    local_url, local_requests, _ = model_servers()
    external_url, external_requests, reply = model_servers()
    reply.update(content=content, finish_reason=finish_reason, http_status=http_status)
    result, fallback = diagnose_with_routing(
        routed_settings(local_url, external_url), scenario="bus_off"
    )
    assert result["llm_status"] == "unavailable"
    assert result["summary"] == fallback["summary"]
    assert result["findings"] == fallback["findings"]
    assert result["explanation"] is None
    assert result["llm_routing"]["backend"] == "pamout"
    assert len(result["llm_routing"]["attempts"]) == 1
    assert len(external_requests) == 1
    assert not local_requests
    assert "sensitive-backend-detail" not in json.dumps(result)


@pytest.mark.parametrize("response_format", ["json_object", "json_schema"])
def test_external_structured_output_is_explicitly_configurable(model_servers, response_format):
    local_url, _, _ = model_servers()
    external_url, external_requests, _ = model_servers()
    settings = routed_settings(
        local_url, external_url, external_llm_response_format=response_format
    )
    result, _ = diagnose_with_routing(settings, llm_target="external")
    assert result["llm_status"] == "generated"
    assert external_requests[0][1]["response_format"]["type"] == response_format


def test_external_schema_rejection_does_not_retry_with_weaker_format(model_servers):
    local_url, local_requests, _ = model_servers()
    external_url, external_requests, reply = model_servers()
    reply["http_status"] = 400
    settings = routed_settings(local_url, external_url, external_llm_response_format="json_schema")
    result, _ = diagnose_with_routing(settings, llm_target="external")
    assert result["llm_status"] == "unavailable"
    assert len(external_requests) == 1
    assert external_requests[0][1]["response_format"]["type"] == "json_schema"
    assert not local_requests


def test_local_timeout_escalates_without_sdk_retry(model_servers):
    local_url, local_requests, local_reply = model_servers()
    external_url, external_requests, _ = model_servers()
    local_reply["delay_seconds"] = 0.3
    result, _ = diagnose_with_routing(
        routed_settings(local_url, external_url, llm_timeout_seconds=0.05)
    )
    assert result["llm_status"] == "generated"
    assert "Timeout" in result["llm_routing"]["attempts"][0]["error"]
    assert len(local_requests) == len(external_requests) == 1


def test_external_timeout_preserves_findings_without_retry(model_servers):
    local_url, local_requests, _ = model_servers()
    external_url, external_requests, reply = model_servers()
    reply["delay_seconds"] = 0.3
    result, fallback = diagnose_with_routing(
        routed_settings(local_url, external_url, external_llm_timeout_seconds=0.05),
        llm_target="external",
    )
    assert result["llm_status"] == "unavailable"
    assert "Timeout" in result["llm_error"]
    assert result["findings"] == fallback["findings"]
    assert len(external_requests) == 1
    assert not local_requests


def test_both_backends_failing_keep_both_attempts_and_code_generated_summary(model_servers):
    local_url, local_requests, local_reply = model_servers()
    external_url, external_requests, reply = model_servers()
    local_reply["finish_reason"] = "length"
    reply["http_status"] = 500
    result, fallback = diagnose_with_routing(routed_settings(local_url, external_url))
    assert result["llm_status"] == "unavailable"
    assert result["summary"] == fallback["summary"]
    assert result["llm_routing"]["reason"] == "local_failure"
    attempts = result["llm_routing"]["attempts"]
    assert attempts[0]["metadata"]["finish_reason"] == "length"
    assert [item["status"] for item in attempts] == ["unavailable", "unavailable"]
    assert len(local_requests) == len(external_requests) == 1
