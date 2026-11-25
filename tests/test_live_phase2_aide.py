import json
from datetime import UTC, datetime, timedelta
from threading import Thread

import pytest
from fastapi.testclient import TestClient

from rpi5_agent.api import create_app
from rpi5_agent.assessment import evaluate, evaluate_live_aide_ecu
from rpi5_agent.config import Settings
from rpi5_agent.evidence_summary import build_model_payload
from rpi5_agent.hw_collector import (
    CanCollectorBuffer,
    UartCollectorBuffer,
    compute_alive_counter_gap,
    create_collector_http_server,
    is_alive_counter_frozen,
    parse_a_fan_cmd_frame,
    parse_a_thermal_status_frame,
    parse_b_fan_status_frame,
)
from rpi5_agent.llm import RoutedSummarizer
from rpi5_agent.models import LLMTarget
from rpi5_agent.specification import LiveAideEcuSpecification
from rpi5_agent.tools.collection import collect_evidence
from rpi5_agent.tools.live_provider import HttpHardwareProvider


def _adapter_up(state: str = "ERROR-ACTIVE", operstate: str = "UP") -> dict:
    return {
        "operstate": operstate,
        "can_state": state,
        "bitrate_bps": 500000,
        "rx_packets": 300,
        "tx_packets": 10,
        "rx_errors": 0,
        "tx_errors": 0,
        "bus_errors": 0,
        "rx_dropped": 0,
        "rx_missed": 0,
    }


def _start_test_collector(
    can_buffer: CanCollectorBuffer,
    uart_buffer: UartCollectorBuffer,
    *,
    adapter_state: str = "ERROR-ACTIVE",
    operstate: str = "UP",
):
    server = create_collector_http_server(
        "127.0.0.1",
        0,
        can_buffer=can_buffer,
        uart_buffer=uart_buffer,
        adapter_reader=lambda _iface: _adapter_up(adapter_state, operstate),
        port_probe=lambda: False,
    )
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    worker.start()
    return server, worker, f"http://127.0.0.1:{server.server_port}"


def _build_a_thermal_bytes(
    *,
    temp_raw: int = 0x01A4,
    duty_target: int = 58,
    vib_raw: int = 120,
    a_state: int = 1,
    thermal_state: int = 0,
    fault_flags: int = 0,
    alive_cnt: int = 7,
) -> bytes:
    state_byte = (a_state & 0x0F) | ((thermal_state & 0x0F) << 4)
    return (
        temp_raw.to_bytes(2, "little", signed=True)
        + bytes([duty_target])
        + vib_raw.to_bytes(2, "little", signed=False)
        + bytes([state_byte, fault_flags, alive_cnt])
    )


def _build_b_fan_bytes(
    *,
    fan_rpm: int = 1500,
    duty_actual: int = 58,
    fan_state: int = 2,
    fault_code: int = 0,
    alive_cnt: int = 7,
) -> bytes:
    return fan_rpm.to_bytes(2, "little", signed=False) + bytes(
        [duty_actual, fan_state, fault_code, 0, 0, alive_cnt]
    )


def _record_healthy_aide_bus(buf: CanCollectorBuffer, now: datetime) -> None:
    ts = now - timedelta(milliseconds=40)
    buf.record_frame(0x080, bytes.fromhex("3A 00 00 00 00 00 00 07"), dlc=8, rx_utc=ts)
    buf.record_frame(0x180, bytes.fromhex("A4 01 3A 78 00 01 00 07"), dlc=8, rx_utc=ts)
    buf.record_frame(0x1C0, _build_b_fan_bytes(fan_rpm=1500, duty_actual=58), dlc=8, rx_utc=ts)


def test_parse_phase2_reference_frames_and_boundary_rejections():
    cmd_ok = parse_a_fan_cmd_frame(0x080, bytes.fromhex("3A 00 00 00 00 00 00 07"), dlc=8)
    assert cmd_ok == {"valid": True, "dlc": 8, "duty_target": 58, "alive_counter": 7}

    cmd_fail_duty = parse_a_fan_cmd_frame(0x080, bytes.fromhex("64 00 00 00 00 00 00 08"), dlc=8)
    assert cmd_fail_duty == {"valid": True, "dlc": 8, "duty_target": 100, "alive_counter": 8}

    assert (
        parse_a_fan_cmd_frame(0x080, bytes.fromhex("65 00 00 00 00 00 00 08"), dlc=8)["valid"]
        is False
    )
    assert (
        parse_a_fan_cmd_frame(0x080, bytes.fromhex("3A 00 00 00 00 00 00"), dlc=7)["valid"] is False
    )

    th_ok = parse_a_thermal_status_frame(0x180, bytes.fromhex("A4 01 3A 78 00 01 00 07"), dlc=8)
    assert th_ok == {
        "valid": True,
        "dlc": 8,
        "temp_degc": 42.0,
        "temp_valid": True,
        "duty_target": 58,
        "vib_rms_mg": 120,
        "vib_valid": True,
        "a_state": 1,
        "thermal_state": 0,
        "fault_flags": 0,
        "alive_counter": 7,
    }

    th_failsafe = parse_a_thermal_status_frame(
        0x180, bytes.fromhex("FF 7F 64 78 00 32 01 08"), dlc=8
    )
    assert th_failsafe["valid"] is True
    assert th_failsafe["temp_degc"] is None
    assert th_failsafe["temp_valid"] is False
    assert th_failsafe["duty_target"] == 100
    assert th_failsafe["a_state"] == 2
    assert th_failsafe["thermal_state"] == 3
    assert th_failsafe["fault_flags"] == 0x01

    th_neg = parse_a_thermal_status_frame(
        0x180, _build_a_thermal_bytes(temp_raw=-55, vib_raw=0xFFFF), dlc=8
    )
    assert th_neg["valid"] is True
    assert th_neg["temp_degc"] == -5.5
    assert th_neg["temp_valid"] is True
    assert th_neg["vib_rms_mg"] is None
    assert th_neg["vib_valid"] is False

    assert (
        parse_a_thermal_status_frame(0x180, _build_a_thermal_bytes(a_state=4), dlc=8)["valid"]
        is False
    )
    assert (
        parse_a_thermal_status_frame(0x180, _build_a_thermal_bytes(thermal_state=4), dlc=8)["valid"]
        is False
    )
    assert (
        parse_a_thermal_status_frame(0x180, _build_a_thermal_bytes(duty_target=101), dlc=8)["valid"]
        is False
    )

    fan_ok = parse_b_fan_status_frame(0x1C0, bytes.fromhex("DC 05 3C 02 00 00 00 07"), dlc=8)
    assert fan_ok == {
        "valid": True,
        "dlc": 8,
        "fan_rpm": 1500,
        "duty_actual": 60,
        "fan_state": 2,
        "fault_code": 0,
        "alive_counter": 7,
    }
    assert parse_b_fan_status_frame(0x1C0, _build_b_fan_bytes(fan_state=5), dlc=8)["valid"] is False
    assert (
        parse_b_fan_status_frame(0x1C0, _build_b_fan_bytes(duty_actual=101), dlc=8)["valid"]
        is False
    )


def test_collector_buffer_tracks_multiple_streams_and_separates_gap_from_freeze():
    assert compute_alive_counter_gap(7, 7) == 0
    assert is_alive_counter_frozen(7, 7) is True
    assert is_alive_counter_frozen(7, 8) is False

    t0 = datetime(2026, 11, 25, 6, 0, 0, tzinfo=UTC)
    buf = CanCollectorBuffer()
    buf.record_frame(0x100, b"\xa5\x5a\x00\x01", dlc=4, rx_utc=t0)
    buf.record_frame(0x080, bytes.fromhex("3A 00 00 00 00 00 00 07"), dlc=8, rx_utc=t0)
    buf.record_frame(0x180, bytes.fromhex("A4 01 3A 78 00 01 00 07"), dlc=8, rx_utc=t0)
    # Repeat alive_cnt=7 three more times on 0x1C0 (consecutive_frozen_count == 3, gap == 0).
    for step in range(4):
        buf.record_frame(
            0x1C0,
            _build_b_fan_bytes(alive_cnt=7),
            dlc=8,
            rx_utc=t0 + timedelta(milliseconds=100 * step),
        )

    snap_frozen = buf.snapshot(
        interface="can0", adapter=_adapter_up(), now=t0 + timedelta(milliseconds=320)
    )
    assert set(snap_frozen["streams"].keys()) == {"0x100", "0x080", "0x180", "0x1C0"}
    b_stream = snap_frozen["streams"]["0x1C0"]
    assert b_stream["consecutive_frozen_count"] == 3
    assert b_stream["sequence_gap_count"] == 0

    # Advancing alive_cnt to 8 clears the frozen counter without adding a gap.
    buf.record_frame(
        0x1C0,
        _build_b_fan_bytes(alive_cnt=8),
        dlc=8,
        rx_utc=t0 + timedelta(milliseconds=400),
    )
    snap_resumed = buf.snapshot(
        interface="can0", adapter=_adapter_up(), now=t0 + timedelta(milliseconds=420)
    )
    assert snap_resumed["streams"]["0x1C0"]["consecutive_frozen_count"] == 0
    assert snap_resumed["streams"]["0x1C0"]["sequence_gap_count"] == 0


def test_healthy_3node_aide_bus_yields_all_info_and_uses_zero_token_template():
    now = datetime.now(UTC)
    can_buf = CanCollectorBuffer()
    _record_healthy_aide_bus(can_buf, now)
    server, worker, base_url = _start_test_collector(
        can_buf, UartCollectorBuffer(discard_initial_partial=False)
    )
    try:
        spec = LiveAideEcuSpecification()
        provider = HttpHardwareProvider(base_url=base_url, specification=spec)
        calls = provider.plan(None)  # type: ignore[arg-type]
        assert [c.tool for c in calls] == [
            "can.bus_state",
            "can.capture",
            "can.capture",
            "can.capture",
        ]
        evidence = collect_evidence(provider, None, Settings(_env_file=None).diagnostic_profile)  # type: ignore[arg-type]
        findings = evaluate(evidence, spec=spec)
        assert {item.code for item in findings} == {
            "LOCAL_ADAPTER_ACTIVE",
            "A_FAN_CMD_FRESH",
            "A_THERMAL_STATUS_FRESH",
            "B_FAN_STATUS_FRESH",
        }
        assert all(item.severity == "info" for item in findings)

        summarizer = RoutedSummarizer(
            Settings(_env_file=None, llm_enabled=True, external_llm_enabled=False)
        )
        summary = summarizer.summarize(evidence, findings, target=LLMTarget.AUTO)
        assert summary.routing is not None
        assert summary.routing.backend == "template"
        assert summary.routing.reason == "deterministic_template"
        assert summary.metadata.input_tokens == 0
        assert summary.metadata.output_tokens == 0
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@pytest.mark.parametrize(
    ("setup_mode", "expected_present", "expected_absent"),
    [
        (
            "bmp280_failsafe_ok",
            {
                "A_FAN_CMD_FRESH",
                "A_THERMAL_STATUS_FRESH",
                "BOARD_A_FAILSAFE_ACTIVE",
                "BOARD_A_FAULT_TEMP_SENSOR",
            },
            {"BOARD_A_FAILSAFE_DUTY_VIOLATION", "BOARD_A_TEMP_INVALID"},
        ),
        (
            "bmp280_failsafe_duty_violation",
            {
                "BOARD_A_FAILSAFE_ACTIVE",
                "BOARD_A_FAILSAFE_DUTY_VIOLATION",
                "BOARD_A_FAULT_TEMP_SENSOR",
            },
            set(),
        ),
        (
            "over_temp_not_sensor_fault",
            {"THERMAL_STATE_OVER_TEMP"},
            {"BOARD_A_FAULT_TEMP_SENSOR", "BOARD_A_FAILSAFE_ACTIVE"},
        ),
        (
            "thermal_warning",
            {"THERMAL_STATE_WARNING"},
            {"THERMAL_STATE_OVER_TEMP", "BOARD_A_FAULT_TEMP_SENSOR"},
        ),
        (
            "vib_sensor_fault_and_simultaneous_timeout",
            {
                "BOARD_A_FAULT_VIB_SENSOR",
                "BOARD_A_FAULT_FAN_STATUS_TIMEOUT",
                "CROSS_BOARD_B_STATUS_ABSENT_CONFIRMED",
                "B_FAN_STATUS_MISSING",
            },
            {"BOARD_A_VIB_INVALID"},
        ),
        (
            "board_a_rx_path_suspected",
            {
                "BOARD_A_FAULT_FAN_STATUS_TIMEOUT",
                "B_FAN_STATUS_FRESH",
                "CROSS_BOARD_A_RX_PATH_SUSPECTED",
            },
            {"CROSS_BOARD_B_STATUS_ABSENT_CONFIRMED"},
        ),
        (
            "board_a_cmd_absent_confirmed",
            {"A_FAN_CMD_MISSING", "FAN_FAULT_CMD_TIMEOUT", "CROSS_BOARD_A_CMD_ABSENT_CONFIRMED"},
            {"CROSS_BOARD_B_RX_PATH_SUSPECTED"},
        ),
        (
            "board_b_rx_path_suspected",
            {"A_FAN_CMD_FRESH", "FAN_FAULT_CMD_TIMEOUT", "CROSS_BOARD_B_RX_PATH_SUSPECTED"},
            {"CROSS_BOARD_A_CMD_ABSENT_CONFIRMED"},
        ),
        (
            "test_modes_active",
            {"BOARD_A_TEST_MODE_ACTIVE", "FAN_TEST_MODE_ACTIVE"},
            {"BOARD_A_FAILSAFE_ACTIVE"},
        ),
    ],
)
def test_evaluate_live_aide_ecu_fault_and_cross_correlation_scenarios(
    setup_mode, expected_present, expected_absent
):
    now = datetime.now(UTC)
    ts = now - timedelta(milliseconds=30)
    buf = CanCollectorBuffer()

    if setup_mode == "bmp280_failsafe_ok":
        buf.record_frame(0x080, bytes.fromhex("64 00 00 00 00 00 00 08"), dlc=8, rx_utc=ts)
        buf.record_frame(0x180, bytes.fromhex("FF 7F 64 78 00 32 01 08"), dlc=8, rx_utc=ts)
        buf.record_frame(0x1C0, _build_b_fan_bytes(duty_actual=100, fan_rpm=2800), dlc=8, rx_utc=ts)
    elif setup_mode == "bmp280_failsafe_duty_violation":
        buf.record_frame(0x080, bytes.fromhex("3A 00 00 00 00 00 00 08"), dlc=8, rx_utc=ts)
        buf.record_frame(
            0x180,
            _build_a_thermal_bytes(
                temp_raw=0x7FFF, duty_target=58, a_state=2, thermal_state=3, fault_flags=0x01
            ),
            dlc=8,
            rx_utc=ts,
        )
        buf.record_frame(0x1C0, _build_b_fan_bytes(), dlc=8, rx_utc=ts)
    elif setup_mode == "over_temp_not_sensor_fault":
        buf.record_frame(0x080, bytes.fromhex("64 00 00 00 00 00 00 09"), dlc=8, rx_utc=ts)
        buf.record_frame(
            0x180,
            _build_a_thermal_bytes(
                temp_raw=580, duty_target=100, a_state=1, thermal_state=2, fault_flags=0x00
            ),
            dlc=8,
            rx_utc=ts,
        )
        buf.record_frame(0x1C0, _build_b_fan_bytes(duty_actual=100, fan_rpm=2900), dlc=8, rx_utc=ts)
    elif setup_mode == "thermal_warning":
        buf.record_frame(0x080, bytes.fromhex("55 00 00 00 00 00 00 09"), dlc=8, rx_utc=ts)
        buf.record_frame(
            0x180,
            _build_a_thermal_bytes(
                temp_raw=490, duty_target=85, a_state=1, thermal_state=1, fault_flags=0x00
            ),
            dlc=8,
            rx_utc=ts,
        )
        buf.record_frame(0x1C0, _build_b_fan_bytes(duty_actual=85, fan_rpm=2400), dlc=8, rx_utc=ts)
    elif setup_mode == "vib_sensor_fault_and_simultaneous_timeout":
        buf.record_frame(0x080, bytes.fromhex("3A 00 00 00 00 00 00 09"), dlc=8, rx_utc=ts)
        buf.record_frame(
            0x180,
            _build_a_thermal_bytes(vib_raw=0xFFFF, fault_flags=0x06),
            dlc=8,
            rx_utc=ts,
        )
    elif setup_mode == "board_a_rx_path_suspected":
        buf.record_frame(0x080, bytes.fromhex("3A 00 00 00 00 00 00 09"), dlc=8, rx_utc=ts)
        buf.record_frame(
            0x180,
            _build_a_thermal_bytes(fault_flags=0x04),
            dlc=8,
            rx_utc=ts,
        )
        buf.record_frame(0x1C0, _build_b_fan_bytes(), dlc=8, rx_utc=ts)
    elif setup_mode == "board_a_cmd_absent_confirmed":
        buf.record_frame(0x180, _build_a_thermal_bytes(fault_flags=0x08), dlc=8, rx_utc=ts)
        buf.record_frame(
            0x1C0,
            _build_b_fan_bytes(fan_state=3, fault_code=0x03),
            dlc=8,
            rx_utc=ts,
        )
    elif setup_mode == "board_b_rx_path_suspected":
        buf.record_frame(0x080, bytes.fromhex("3A 00 00 00 00 00 00 09"), dlc=8, rx_utc=ts)
        buf.record_frame(0x180, _build_a_thermal_bytes(fault_flags=0x08), dlc=8, rx_utc=ts)
        buf.record_frame(
            0x1C0,
            _build_b_fan_bytes(fan_state=3, fault_code=0x03),
            dlc=8,
            rx_utc=ts,
        )
    elif setup_mode == "test_modes_active":
        buf.record_frame(0x080, bytes.fromhex("64 00 00 00 00 00 00 09"), dlc=8, rx_utc=ts)
        buf.record_frame(
            0x180,
            _build_a_thermal_bytes(
                temp_raw=0x7FFF, duty_target=100, a_state=3, thermal_state=3, fault_flags=0x01
            ),
            dlc=8,
            rx_utc=ts,
        )
        buf.record_frame(
            0x1C0,
            _build_b_fan_bytes(fan_rpm=0, duty_actual=0, fan_state=4, fault_code=0x01),
            dlc=8,
            rx_utc=ts,
        )

    server, worker, base_url = _start_test_collector(
        buf, UartCollectorBuffer(discard_initial_partial=False)
    )
    try:
        spec = LiveAideEcuSpecification()
        provider = HttpHardwareProvider(base_url=base_url, specification=spec)
        evidence = [provider.read(call, None) for call in provider.plan(None)]  # type: ignore[arg-type]
        codes = {item.code for item in evaluate_live_aide_ecu(evidence, spec)}
        assert expected_present <= codes
        assert not (expected_absent & codes)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_alive_freeze_vs_gap_and_bitmask_fault_encoding_and_bus_off_guard():
    now = datetime.now(UTC)
    buf = CanCollectorBuffer()
    buf.record_frame(
        0x080,
        bytes.fromhex("3A 00 00 00 00 00 00 01"),
        dlc=8,
        rx_utc=now - timedelta(milliseconds=220),
    )
    buf.record_frame(
        0x080,
        bytes.fromhex("3A 00 00 00 00 00 00 04"),
        dlc=8,
        rx_utc=now - timedelta(milliseconds=20),
    )
    buf.record_frame(
        0x180, _build_a_thermal_bytes(alive_cnt=5), dlc=8, rx_utc=now - timedelta(milliseconds=20)
    )
    for step in range(4):
        buf.record_frame(
            0x1C0,
            _build_b_fan_bytes(fan_state=3, fault_code=0x03, alive_cnt=9),
            dlc=8,
            rx_utc=now - timedelta(milliseconds=320 - 100 * step),
        )

    server, worker, base_url = _start_test_collector(
        buf, UartCollectorBuffer(discard_initial_partial=False)
    )
    try:
        spec = LiveAideEcuSpecification(b_fault_encoding="bitmask")
        provider = HttpHardwareProvider(base_url=base_url, specification=spec)
        evidence = [provider.read(call, None) for call in provider.plan(None)]  # type: ignore[arg-type]
        codes = {item.code for item in evaluate_live_aide_ecu(evidence, spec)}
        assert {
            "A_FAN_CMD_FRESH",
            "A_FAN_CMD_SEQUENCE_GAP",
            "A_THERMAL_STATUS_FRESH",
            "B_FAN_STATUS_ALIVE_FROZEN",
        } <= codes
        assert "B_FAN_STATUS_FRESH" not in codes
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)

    # Bitmask fault decoding when B_FAN_STATUS is fresh.
    buf2 = CanCollectorBuffer()
    ts = now - timedelta(milliseconds=20)
    buf2.record_frame(0x080, bytes.fromhex("3A 00 00 00 00 00 00 01"), dlc=8, rx_utc=ts)
    buf2.record_frame(0x180, _build_a_thermal_bytes(alive_cnt=1), dlc=8, rx_utc=ts)
    buf2.record_frame(
        0x1C0, _build_b_fan_bytes(fan_state=3, fault_code=0x03, alive_cnt=1), dlc=8, rx_utc=ts
    )
    server2, worker2, base_url2 = _start_test_collector(
        buf2, UartCollectorBuffer(discard_initial_partial=False)
    )
    try:
        spec = LiveAideEcuSpecification(b_fault_encoding="bitmask")
        provider2 = HttpHardwareProvider(base_url=base_url2, specification=spec)
        evidence2 = [provider2.read(call, None) for call in provider2.plan(None)]  # type: ignore[arg-type]
        codes2 = {item.code for item in evaluate_live_aide_ecu(evidence2, spec)}
        assert {"FAN_FAULT_NO_ROTATION", "FAN_FAULT_RPM_DEVIATION"} <= codes2
    finally:
        server2.shutdown()
        server2.server_close()
        worker2.join(timeout=2)

    # Local adapter BUS-OFF stops remote CAN fault misdiagnosis.
    server3, worker3, base_url3 = _start_test_collector(
        CanCollectorBuffer(),
        UartCollectorBuffer(discard_initial_partial=False),
        adapter_state="BUS-OFF",
    )
    try:
        spec = LiveAideEcuSpecification()
        provider3 = HttpHardwareProvider(base_url=base_url3, specification=spec)
        evidence3 = [provider3.read(call, None) for call in provider3.plan(None)]  # type: ignore[arg-type]
        codes3 = {item.code for item in evaluate_live_aide_ecu(evidence3, spec)}
        assert codes3 == {"LOCAL_ADAPTER_BUS_OFF", "REMOTE_STATE_UNKNOWN"}
    finally:
        server3.shutdown()
        server3.server_close()
        worker3.join(timeout=2)


def test_phase2_api_capabilities_diagnose_and_model_payload_guard():
    now = datetime.now(UTC)
    can_buf = CanCollectorBuffer()
    _record_healthy_aide_bus(can_buf, now)
    server, worker, base_url = _start_test_collector(
        can_buf, UartCollectorBuffer(discard_initial_partial=False)
    )
    try:
        settings = Settings(
            _env_file=None,
            evidence_provider="http_collector",
            live_specification="aide_ecu",
            collector_base_url=base_url,
            llm_enabled=False,
        )
        with TestClient(create_app(settings)) as client:
            caps = client.get("/capabilities").json()
            assert caps["mode"] == "live"
            assert caps["assessment_specification_id"] == "aide-ecu-v1"
            assert caps["physical_collection"] is True

            diag = client.post("/diagnose", json={}).json()
            assert diag["mode"] == "live"
            assert diag["assessment_specification_id"] == "aide-ecu-v1"
            assert {item["code"] for item in diag["findings"]} == {
                "LOCAL_ADAPTER_ACTIVE",
                "A_FAN_CMD_FRESH",
                "A_THERMAL_STATUS_FRESH",
                "B_FAN_STATUS_FRESH",
            }
            evidence = create_collector_evidence_fixture(base_url)
            payload = build_model_payload(evidence, evaluate_live_aide_ecu(evidence))
            raw_payload = json.dumps(payload, ensure_ascii=False)
            assert payload["assessment_specification"]["id"] == "aide-ecu-v1"
            assert "secret_raw_frame" not in raw_payload
            assert "MUST_BE_FILTERED" not in raw_payload
            assert len(raw_payload.encode()) <= 6000
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def create_collector_evidence_fixture(base_url: str):
    provider = HttpHardwareProvider(base_url=base_url, specification=LiveAideEcuSpecification())
    calls = provider.plan(None)  # type: ignore[arg-type]
    evidence = [provider.read(call, None) for call in calls]  # type: ignore[arg-type]
    evidence[2].data["secret_raw_frame"] = "MUST_BE_FILTERED"
    return evidence
