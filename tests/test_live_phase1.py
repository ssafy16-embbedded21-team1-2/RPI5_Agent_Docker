import json
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from fastapi.testclient import TestClient

from rpi5_agent.api import create_app
from rpi5_agent.assessment import evaluate, evaluate_live_hw_test
from rpi5_agent.config import Settings
from rpi5_agent.evidence_summary import build_model_payload
from rpi5_agent.hw_collector import (
    CanCollectorBuffer,
    UartCollectorBuffer,
    compute_alive_counter_gap,
    compute_uart_delta,
    create_collector_http_server,
    parse_hw_test_can_frame,
    parse_hw_test_uart_line,
    parse_ip_link_can_json,
)
from rpi5_agent.models import Scenario, ToolError, ToolResult
from rpi5_agent.tools.live_provider import HttpHardwareProvider

SAMPLE_UART_LINE_1 = (
    "t=404009 ms TEC=0 REC=0 LEC=0 EWG=0 EPV=0 BOFF=0 | "
    "txreq=4041 txok=2954 txabort=0 txbusy=0 errcb=1086 errcode=0x00001000 | "
    "rx=0 last=0x000 dlc=0"
)
SAMPLE_UART_LINE_2 = (
    "t=407009 ms TEC=0 REC=0 LEC=0 EWG=0 EPV=0 BOFF=0 | "
    "txreq=4071 txok=2984 txabort=0 txbusy=0 errcb=1086 errcode=0x00001000 | "
    "rx=1 last=0x123 dlc=4"
)
SAMPLE_UART_REBOOT_LINE = (
    "t=1009 ms TEC=0 REC=0 LEC=0 EWG=0 EPV=0 BOFF=0 | "
    "txreq=10 txok=10 txabort=0 txbusy=0 errcb=0 errcode=0x00000000 | "
    "rx=0 last=0x000 dlc=0"
)


def _adapter_up(state: str = "ERROR-ACTIVE", bus_errors: int = 0) -> dict:
    return {
        "operstate": "UP",
        "can_state": state,
        "bitrate_bps": 500000,
        "rx_packets": 100,
        "tx_packets": 1,
        "rx_errors": 0,
        "tx_errors": 0,
        "bus_errors": bus_errors,
        "rx_dropped": 0,
        "rx_missed": 0,
    }


def _start_test_collector(
    can_buffer: CanCollectorBuffer,
    uart_buffer: UartCollectorBuffer,
    *,
    adapter_state: str = "ERROR-ACTIVE",
    port_available: bool = True,
):
    server: ThreadingHTTPServer = create_collector_http_server(
        "127.0.0.1",
        0,
        can_buffer=can_buffer,
        uart_buffer=uart_buffer,
        adapter_reader=lambda _iface: _adapter_up(adapter_state),
        port_probe=lambda: port_available,
    )
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    worker.start()
    return server, worker, f"http://127.0.0.1:{server.server_port}"


def test_parse_hw_test_can_frame_and_alive_counter_wraparound():
    ok = parse_hw_test_can_frame(0x100, b"\xa5\x5a\x00\xff", dlc=4)
    assert ok == {"valid": True, "magic_ok": True, "alive_counter": 255, "dlc": 4}

    bad_magic = parse_hw_test_can_frame(0x100, b"\x00\x00\x00\x01", dlc=4)
    assert bad_magic["valid"] is False
    bad_dlc = parse_hw_test_can_frame(0x100, b"\xa5\x5a\x00\x01\x02", dlc=5)
    assert bad_dlc["valid"] is False
    wrong_id = parse_hw_test_can_frame(0x123, b"\xa5\x5a\x00\x01", dlc=4)
    assert wrong_id["valid"] is False

    assert compute_alive_counter_gap(None, 63) == 0
    assert compute_alive_counter_gap(254, 255) == 0
    assert compute_alive_counter_gap(255, 0) == 0
    assert compute_alive_counter_gap(10, 13) == 2
    assert compute_alive_counter_gap(254, 1) == 2


def test_can_collector_buffer_tracks_window_gaps_and_expiration():
    t0 = datetime(2026, 11, 17, 5, 0, 0, tzinfo=UTC)
    buf = CanCollectorBuffer()
    buf.record_frame(0x100, b"\xa5\x5a\x00\xfe", dlc=4, rx_utc=t0)
    buf.record_frame(0x100, b"\xa5\x5a\x00\xff", dlc=4, rx_utc=t0 + timedelta(milliseconds=100))
    buf.record_frame(0x100, b"\xa5\x5a\x00\x02", dlc=4, rx_utc=t0 + timedelta(milliseconds=400))

    snap = buf.snapshot(
        interface="can0",
        adapter=_adapter_up(),
        window_ms=5000,
        now=t0 + timedelta(milliseconds=450),
    )
    stream = snap["streams"]["0x100"]
    assert stream["frame_count_in_window"] == 3
    assert stream["valid_frame_count_in_window"] == 3
    assert stream["last_alive_counter"] == 2
    assert stream["sequence_gap_count"] == 2
    assert stream["age_ms"] == 50.0
    assert stream["decoded"]["magic_ok"] is True

    expired = buf.snapshot(
        interface="can0",
        adapter=_adapter_up(),
        window_ms=5000,
        now=t0 + timedelta(seconds=10),
    )
    assert expired["streams"]["0x100"]["frame_count_in_window"] == 0


def test_uart_parser_discards_initial_partial_garbage_and_detects_reboot():
    p1 = parse_hw_test_uart_line(SAMPLE_UART_LINE_1)
    p2 = parse_hw_test_uart_line(SAMPLE_UART_LINE_2)
    assert p1 is not None and p2 is not None
    assert p1["errcb"] == 1086
    assert p1["errcode"] == "0x00001000"
    assert parse_hw_test_uart_line("partial garbage \x01\x02") is None

    delta = compute_uart_delta(p1, p2)
    assert delta == {
        "interval_ms": 3000,
        "epoch_continuous": True,
        "txreq_delta": 30,
        "txok_delta": 30,
        "txabort_delta": 0,
        "txbusy_delta": 0,
        "errcb_delta": 0,
        "rx_delta": 1,
    }

    t0 = datetime(2026, 11, 17, 5, 1, 0, tzinfo=UTC)
    ubuf = UartCollectorBuffer(discard_initial_partial=True)
    # Initial partial line + binary noise before first newline must be ignored.
    ubuf.feed_bytes(
        b"errcode=0x00001000 | rx=0\r\n" + SAMPLE_UART_LINE_1.encode() + b"\r\n",
        now=t0,
    )
    snap1 = ubuf.snapshot(
        target="hw_test_f103", device_by_id="/dev/ttyACM0", port_available=True, now=t0
    )
    assert snap1["status"] == "ok"
    assert snap1["parsed_lines"] == 1
    assert snap1["delta"] is None

    ubuf.feed_bytes(SAMPLE_UART_LINE_2.encode() + b"\r\n", now=t0 + timedelta(seconds=3))
    snap2 = ubuf.snapshot(
        target="hw_test_f103",
        device_by_id="/dev/ttyACM0",
        port_available=True,
        now=t0 + timedelta(seconds=3, milliseconds=120),
    )
    assert snap2["delta"]["txok_delta"] == 30
    assert snap2["delta"]["errcb_delta"] == 0
    assert snap2["sample_age_ms"] == 120.0

    # Simulate STM32 reset (t_ms wraps backwards).
    ubuf.feed_bytes(SAMPLE_UART_REBOOT_LINE.encode() + b"\r\n", now=t0 + timedelta(seconds=5))
    snap3 = ubuf.snapshot(
        target="hw_test_f103",
        device_by_id="/dev/ttyACM0",
        port_available=True,
        now=t0 + timedelta(seconds=5),
    )
    assert snap3["boot_epoch"] == 2
    assert snap3["delta"]["epoch_continuous"] is False


def test_parse_ip_link_can_json_handles_socketcan_up_and_stopped():
    raw_up = json.dumps(
        [
            {
                "ifname": "can0",
                "flags": ["NOARP", "UP", "LOWER_UP", "ECHO"],
                "operstate": "UNKNOWN",
                "linkinfo": {
                    "info_data": {
                        "state": "ERROR-ACTIVE",
                        "bittiming": {"bitrate": 500000},
                    },
                    "info_xstats": {"bus_error": 0},
                },
                "stats64": {
                    "rx": {"packets": 101, "errors": 0, "dropped": 1, "over_errors": 0},
                    "tx": {"packets": 1, "errors": 0, "dropped": 0},
                },
            }
        ]
    )
    parsed = parse_ip_link_can_json(raw_up, "can0")
    assert parsed["operstate"] == "UP"
    assert parsed["can_state"] == "ERROR-ACTIVE"
    assert parsed["bitrate_bps"] == 500000
    assert parsed["rx_packets"] == 101


def test_collector_http_server_is_read_only_and_feeds_http_provider():
    now = datetime.now(UTC)
    can_buf = CanCollectorBuffer()
    can_buf.record_frame(
        0x100, b"\xa5\x5a\x00\x3f", dlc=4, rx_utc=now - timedelta(milliseconds=120)
    )
    can_buf.record_frame(0x100, b"\xa5\x5a\x00\x40", dlc=4, rx_utc=now - timedelta(milliseconds=20))

    uart_buf = UartCollectorBuffer(discard_initial_partial=False)
    uart_buf.feed_bytes((SAMPLE_UART_LINE_1 + "\n").encode(), now=now - timedelta(seconds=3))
    uart_buf.feed_bytes((SAMPLE_UART_LINE_2 + "\n").encode(), now=now - timedelta(milliseconds=50))

    server, worker, base_url = _start_test_collector(can_buf, uart_buf)
    try:
        with urlopen(f"{base_url}/health", timeout=2) as resp:
            health = json.load(resp)
        assert health["read_only"] is True
        assert health["can_state"] == "ERROR-ACTIVE"

        with pytest.raises(HTTPError) as exc:
            urlopen(Request(f"{base_url}/snapshot/can", data=b"{}", method="POST"), timeout=2)
        assert exc.value.code == 405

        provider = HttpHardwareProvider(base_url=base_url)
        calls = provider.plan(Scenario.HEALTHY)
        results = [provider.read(call, Scenario.HEALTHY) for call in calls]
        assert {r.source for r in results} == {"live_can", "live_uart"}
        codes = {f.code for f in evaluate(results)}
        assert codes == {"LOCAL_ADAPTER_ACTIVE", "CAN_TELEMETRY_FRESH", "UART_PAST_ERROR_INACTIVE"}
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_http_provider_reports_collector_unreachable_without_leaking_url():
    provider = HttpHardwareProvider(base_url="http://127.0.0.1:9", timeout_seconds=0.2)
    calls = provider.plan(Scenario.HEALTHY)
    results = [provider.read(call, Scenario.HEALTHY) for call in calls]
    assert all(
        isinstance(r.error, ToolError) and r.error.code == "collector_unreachable" for r in results
    )
    assert all("127.0.0.1" not in r.model_dump_json() for r in results)
    assert {f.code for f in evaluate(results)} == {"COLLECTOR_UNREACHABLE"}


@pytest.mark.parametrize(
    ("cap_overrides", "uart_overrides", "bus_overrides", "expected"),
    [
        (
            {"frame_count_in_window": 0, "age_ms": None, "magic_ok": None},
            {"txreq_delta": 30, "txok_delta": 0, "errcb_delta": 30, "errcb": 1116},
            {},
            {"LOCAL_ADAPTER_ACTIVE", "CAN_TELEMETRY_MISSING", "UART_TX_ACTIVE_NO_CAN_RX"},
        ),
        (
            {"frame_count_in_window": 0, "age_ms": None, "magic_ok": None},
            {"txreq_delta": 0, "txok_delta": 0, "errcb_delta": 0, "errcb": 0},
            {},
            {"LOCAL_ADAPTER_ACTIVE", "CAN_TELEMETRY_MISSING", "UART_TX_STALLED"},
        ),
        (
            {"age_ms": 600.0},
            {"errcb": 1086, "errcb_delta": 0, "txok_delta": 30},
            {},
            {"LOCAL_ADAPTER_ACTIVE", "CAN_TELEMETRY_STALE", "UART_PAST_ERROR_INACTIVE"},
        ),
        (
            {
                "valid_frame_count_in_window": 0,
                "invalid_frame_count_in_window": 1,
                "magic_ok": False,
            },
            {},
            {},
            {"LOCAL_ADAPTER_ACTIVE", "CAN_TELEMETRY_INVALID", "UART_PAST_ERROR_INACTIVE"},
        ),
        (
            {"sequence_gap_count": 3},
            {"epoch_continuous": False},
            {},
            {"LOCAL_ADAPTER_ACTIVE", "CAN_TELEMETRY_FRESH", "CAN_SEQUENCE_GAP", "ECU_BOOT_CHANGED"},
        ),
        (
            {"frame_count_in_window": 0},
            {"boff": 1, "errcb_delta": 5, "txok_delta": 0},
            {"state": "bus_off", "can_state": "BUS-OFF"},
            {
                "LOCAL_ADAPTER_BUS_OFF",
                "CAN_TELEMETRY_MISSING",
                "ECU_UART_BUS_OFF",
                "UART_TX_ACTIVE_NO_CAN_RX",
            },
        ),
        (
            {},
            {"sample_age_ms": 3000.0, "epv": 1, "errcb_delta": 2, "txreq_delta": 10},
            {"state": "error_passive", "can_state": "ERROR-PASSIVE", "tx_errors": 128},
            {
                "LOCAL_ADAPTER_ERROR_PASSIVE",
                "CAN_TELEMETRY_FRESH",
                "UART_DIAGNOSTICS_STALE",
                "ECU_UART_ERROR_PASSIVE",
                "UART_ACTIVE_ERRORS_INCREASING",
            },
        ),
    ],
)
def test_evaluate_live_hw_test_diagnostic_separations(
    cap_overrides, uart_overrides, bus_overrides, expected
):
    bus_data = {
        "state": "active",
        "operstate": "UP",
        "can_state": "ERROR-ACTIVE",
        "rx_errors": 0,
        "tx_errors": 0,
        "bus_errors": 0,
        **bus_overrides,
    }
    cap_data = {
        "can_id": "0x100",
        "dlc": 4,
        "frame_count_in_window": 50,
        "valid_frame_count_in_window": 50,
        "invalid_frame_count_in_window": 0,
        "age_ms": 50.0,
        "last_alive_counter": 10,
        "sequence_gap_count": 0,
        "magic_ok": True,
        **cap_overrides,
    }
    uart_data = {
        "sample_age_ms": 100.0,
        "boot_epoch": 1,
        "t_ms": 407009,
        "tec": 0,
        "rec": 0,
        "lec": 0,
        "ewg": 0,
        "epv": 0,
        "boff": 0,
        "txreq": 4071,
        "txok": 2984,
        "txabort": 0,
        "txbusy": 0,
        "errcb": 1086,
        "errcode": "0x00001000",
        "rx": 0,
        "last_rx_id": "0x000",
        "last_rx_dlc": 0,
        "epoch_continuous": True,
        "interval_ms": 3000,
        "txreq_delta": 30,
        "txok_delta": 30,
        "txabort_delta": 0,
        "txbusy_delta": 0,
        "errcb_delta": 0,
        "rx_delta": 0,
        **uart_overrides,
    }
    evidence = [
        ToolResult(
            tool="can.bus_state",
            target="can0",
            status="ok",
            source="live_can",
            transport="socketcan",
            validity="valid",
            freshness="fresh",
            data=bus_data,
        ),
        ToolResult(
            tool="can.capture",
            target="can0",
            status="ok",
            source="live_can",
            origin_ecu="hw_test_f103",
            transport="socketcan",
            validity="invalid" if cap_data.get("magic_ok") is False else "valid",
            freshness="fresh",
            data=cap_data,
        ),
        ToolResult(
            tool="serial.read_can_diagnostics",
            target="hw_test_f103",
            status="ok",
            source="live_uart",
            origin_ecu="hw_test_f103",
            transport="uart_text",
            validity="valid",
            freshness="fresh",
            data=uart_data,
        ),
    ]
    assert {item.code for item in evaluate_live_hw_test(evidence)} == expected


def test_live_model_payload_filters_unallowlisted_fields():
    evidence = [
        ToolResult(
            tool="serial.read_can_diagnostics",
            target="hw_test_f103",
            status="ok",
            source="live_uart",
            origin_ecu="hw_test_f103",
            transport="uart_text",
            validity="valid",
            freshness="fresh",
            data={
                "sample_age_ms": 80.0,
                "txok_delta": 30,
                "errcb": 1086,
                "errcb_delta": 0,
                "raw_uart_dump": "SECRET_RAW_LINE",
            },
        )
    ]
    payload = build_model_payload(evidence, evaluate_live_hw_test(evidence))
    raw = json.dumps(payload)
    assert payload["mode"] == "live"
    assert payload["assessment_specification"]["id"] == "hw-test-f103-v1"
    assert "SECRET_RAW_LINE" not in raw
    assert payload["evidence"][0]["data"]["errcb"] == 1086


def test_live_api_endpoints_and_verify_guard():
    now = datetime.now(UTC)
    can_buf = CanCollectorBuffer()
    can_buf.record_frame(0x100, b"\xa5\x5a\x00\x10", dlc=4, rx_utc=now - timedelta(milliseconds=40))
    uart_buf = UartCollectorBuffer(discard_initial_partial=False)
    uart_buf.feed_bytes((SAMPLE_UART_LINE_1 + "\n").encode(), now=now - timedelta(seconds=3))
    uart_buf.feed_bytes((SAMPLE_UART_LINE_2 + "\n").encode(), now=now - timedelta(milliseconds=50))

    server, worker, base_url = _start_test_collector(can_buf, uart_buf)
    try:
        settings = Settings(
            _env_file=None,
            evidence_provider="http_collector",
            collector_base_url=base_url,
            llm_enabled=False,
        )
        with TestClient(create_app(settings)) as client:
            assert client.get("/health").json() == {
                "status": "ok",
                "mode": "live",
                "llm_enabled": False,
            }
            caps = client.get("/capabilities").json()
            assert caps["mode"] == "live"
            assert caps["physical_collection"] is True
            assert caps["assessment_specification_id"] == "hw-test-f103-v1"
            assert caps["fixture_verification"] is False
            assert "can.capture" in caps["allowed_read_tools"]
            assert "serial.read_can_diagnostics" in caps["allowed_read_tools"]
            assert client.get("/verification/specification").json()["available"] is False
            assert client.post("/verify", json={}).status_code == 409

            diag = client.post("/diagnose", json={}).json()
            assert diag["mode"] == "live"
            assert diag["scenario"] is None
            assert diag["assessment_specification_id"] == "hw-test-f103-v1"
            assert diag["summary"].startswith("[실측 데이터]")
            assert {item["code"] for item in diag["findings"]} == {
                "LOCAL_ADAPTER_ACTIVE",
                "CAN_TELEMETRY_FRESH",
                "UART_PAST_ERROR_INACTIVE",
            }
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def _start_mock_llm_server():
    recorded: list[tuple[str, dict]] = []
    valid_reply = {
        "hypothesis": "F103 과거 오류 이력만 있고 현재는 정상입니다.",
        "next_check": "보조 UART와 can0 주기 수신을 계속 관찰하세요.",
    }

    class LlmHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            recorded.append((self.path, json.loads(raw)))
            body = json.dumps(
                {
                    "id": "chatcmpl-live-test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "test-qwen",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(valid_reply, ensure_ascii=False),
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 110, "completion_tokens": 42, "total_tokens": 152},
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    llm_server = ThreadingHTTPServer(("127.0.0.1", 0), LlmHandler)
    llm_worker = Thread(
        target=llm_server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    llm_worker.start()
    return (
        f"http://127.0.0.1:{llm_server.server_port}/v1",
        recorded,
        valid_reply,
        llm_server,
        llm_worker,
    )


def test_live_api_with_mock_llm_uses_live_prompt_framing():
    llm_url, llm_requests, valid_reply, llm_server, llm_worker = _start_mock_llm_server()
    now = datetime.now(UTC)
    can_buf = CanCollectorBuffer()
    can_buf.record_frame(0x100, b"\xa5\x5a\x00\x20", dlc=4, rx_utc=now - timedelta(milliseconds=30))
    uart_buf = UartCollectorBuffer(discard_initial_partial=False)

    server, worker, base_url = _start_test_collector(can_buf, uart_buf, port_available=False)
    try:
        settings = Settings(
            _env_file=None,
            evidence_provider="http_collector",
            collector_base_url=base_url,
            llm_enabled=True,
            llm_base_url=llm_url,
            llm_timeout_seconds=5,
        )
        with TestClient(create_app(settings)) as client:
            diag = client.post("/diagnose", json={"use_llm": True, "llm_target": "local"}).json()
        assert diag["llm_status"] == "generated"
        assert diag["explanation"] == valid_reply
        assert {item["code"] for item in diag["findings"]} == {
            "LOCAL_ADAPTER_ACTIVE",
            "CAN_TELEMETRY_FRESH",
            "UART_DIAGNOSTICS_UNAVAILABLE",
        }
        assert len(llm_requests) == 1
        system_msg = llm_requests[0][1]["messages"][0]["content"]
        assert system_msg.startswith("실측 CAN·UART 데이터의 findings")
        user_payload = json.loads(llm_requests[0][1]["messages"][1]["content"])
        assert user_payload["mode"] == "live"
        assert user_payload["assessment_specification"]["id"] == "hw-test-f103-v1"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        llm_server.shutdown()
        llm_server.server_close()
        llm_worker.join(timeout=2)
