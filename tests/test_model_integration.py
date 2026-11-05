import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from fastapi.testclient import TestClient

from rpi5_agent.api import create_app
from rpi5_agent.config import Settings


def test_langchain_uses_chat_completions_and_keeps_deterministic_findings():
    requests = []

    class ModelHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(
                (self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            )
            body = json.dumps(
                {
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "test-qwen",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": "가상 BMS 데이터가 오래되었습니다.",
                            },
                        }
                    ],
                },
                ensure_ascii=False,
            ).encode()
            self.send_response(200)
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
        settings = Settings(
            _env_file=None,
            llm_enabled=True,
            llm_model="test-qwen",
            llm_base_url=f"http://127.0.0.1:{server.server_port}/v1",
            llm_timeout_seconds=5,
        )
        with TestClient(create_app(settings)) as client:
            response = client.post("/diagnose", json={"scenario": "bms_timeout", "use_llm": True})
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)

    assert response.status_code == 200
    result = response.json()
    assert result["llm_status"] == "generated"
    assert "가상 BMS" in result["summary"]
    assert "BMS_STALE" in {item["code"] for item in result["findings"]}
    path, payload = requests[0]
    assert path == "/v1/chat/completions"
    assert payload["model"] == "test-qwen"
    assert "tools" not in payload
    assert payload.get("max_tokens", payload.get("max_completion_tokens")) == 256
    assert len(payload["messages"]) == 2
