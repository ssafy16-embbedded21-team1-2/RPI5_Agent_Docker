import json
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


@pytest.fixture
def model_server():
    requests = []
    reply = {
        "content": json.dumps(VALID_EXPLANATION, ensure_ascii=False),
        "finish_reason": "stop",
        "http_status": 200,
    }

    class ModelHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(
                (self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            )
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
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests, reply
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


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
