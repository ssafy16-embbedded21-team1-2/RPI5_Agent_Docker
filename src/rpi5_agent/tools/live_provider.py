"""Read-only HTTP client provider for Phase 1 host CAN/UART collector snapshots."""

from __future__ import annotations

import json
import threading
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import urlopen

from rpi5_agent.models import Scenario, ToolCall, ToolError, ToolResult
from rpi5_agent.specification import (
    HW_TEST_SPECIFICATION,
    LiveAideEcuSpecification,
    LiveHwTestSpecification,
)

_AIDE_CAPTURE_TARGETS: dict[str, tuple[str, tuple[str, ...]]] = {
    "a_fan_cmd": ("board_a", ("duty_target",)),
    "a_thermal_status": (
        "board_a",
        (
            "temp_degc",
            "temp_valid",
            "duty_target",
            "vib_rms_mg",
            "vib_valid",
            "a_state",
            "thermal_state",
            "fault_flags",
        ),
    ),
    "b_fan_status": ("board_b", ("fan_rpm", "duty_actual", "fan_state", "fault_code")),
}


class HttpHardwareProvider:
    """Fetches CAN/UART snapshots from the host collector HTTP bridge (single timeout, no retry)."""

    mode = "live"

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8090",
        timeout_seconds: float = 3.0,
        can_interface: str = "can0",
        uart_target: str = "hw_test_f103",
        specification: LiveHwTestSpecification | LiveAideEcuSpecification = HW_TEST_SPECIFICATION,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.can_interface = can_interface
        self.uart_target = uart_target
        self.specification = specification
        self._lock = threading.Lock()
        self._cached_can_snapshot: tuple[str, dict[str, Any] | ToolError] | None = None

    def plan(self, scenario: Scenario) -> list[ToolCall]:
        del scenario  # Live provider reads real hardware independent of synthetic Scenario.
        with self._lock:
            self._cached_can_snapshot = None
        if isinstance(self.specification, LiveAideEcuSpecification):
            calls = [
                ToolCall(tool="can.bus_state", target=self.can_interface),
                ToolCall(tool="can.capture", target="a_fan_cmd"),
                ToolCall(tool="can.capture", target="a_thermal_status"),
                ToolCall(tool="can.capture", target="b_fan_status"),
            ]
            if self.specification.uart_target:
                calls.append(
                    ToolCall(
                        tool="serial.read_can_diagnostics",
                        target=self.specification.uart_target,
                    )
                )
            return calls
        return [
            ToolCall(tool="can.bus_state", target=self.can_interface),
            ToolCall(tool="can.capture", target=self.can_interface),
            ToolCall(tool="serial.read_can_diagnostics", target=self.uart_target),
        ]

    def _fetch_json(self, path: str) -> tuple[str, dict[str, Any] | ToolError]:
        url = f"{self.base_url}{path}"
        try:
            with urlopen(url, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Collector payload must be a JSON object")
            return "ok", payload
        except TimeoutError:
            return "timeout", ToolError(code="collector_unreachable")
        except URLError as exc:
            status = "timeout" if isinstance(exc.reason, TimeoutError) else "unavailable"
            return status, ToolError(code="collector_unreachable")
        except (HTTPError, OSError, UnicodeDecodeError, ValueError):
            return "unavailable", ToolError(code="collector_unreachable")

    def _get_can_snapshot(self) -> tuple[str, dict[str, Any] | ToolError]:
        with self._lock:
            cached = self._cached_can_snapshot
        if cached is not None:
            return cached
        path = f"/snapshot/can?interface={quote(self.can_interface)}"
        outcome = self._fetch_json(path)
        with self._lock:
            self._cached_can_snapshot = outcome
        return outcome

    def _aide_capture_config(self, target: str) -> tuple[str, str, float, tuple[str, ...]] | None:
        if not isinstance(self.specification, LiveAideEcuSpecification):
            return None
        meta = _AIDE_CAPTURE_TARGETS.get(target)
        if meta is None:
            return None
        origin_ecu, decoded_keys = meta
        if target == "a_fan_cmd":
            return (
                self.specification.a_fan_cmd_can_id,
                origin_ecu,
                self.specification.a_cmd_timeout_ms,
                decoded_keys,
            )
        if target == "a_thermal_status":
            return (
                self.specification.a_thermal_status_can_id,
                origin_ecu,
                self.specification.status_timeout_ms,
                decoded_keys,
            )
        return (
            self.specification.b_fan_status_can_id,
            origin_ecu,
            self.specification.status_timeout_ms,
            decoded_keys,
        )

    def read(self, call: ToolCall, scenario: Scenario) -> ToolResult:
        del scenario
        is_aide = isinstance(self.specification, LiveAideEcuSpecification)
        schema_version = self.specification.id if is_aide else None
        if call.tool in {"can.bus_state", "can.capture"}:
            capture_cfg = (
                self._aide_capture_config(call.target) if call.tool == "can.capture" else None
            )
            if is_aide and call.tool == "can.capture" and capture_cfg is None:
                return ToolResult(
                    tool=call.tool,
                    target=call.target,
                    status="unsupported",
                    source="live_can",
                    schema_version=schema_version,
                    error=ToolError(code="tool_not_supported"),
                )
            status, body = self._get_can_snapshot()
            if status != "ok" or not isinstance(body, dict):
                err = (
                    body if isinstance(body, ToolError) else ToolError(code="collector_unreachable")
                )
                return ToolResult(
                    tool=call.tool,
                    target=call.target,
                    status=status,  # type: ignore[arg-type]
                    source="live_can",
                    origin_ecu=capture_cfg[1] if capture_cfg is not None else None,
                    transport="socketcan",
                    schema_version=schema_version,
                    validity="unknown",
                    freshness="unknown",
                    error=err,
                )
            if call.tool == "can.bus_state":
                return self._build_bus_state_result(call, body)
            return self._build_capture_result(call, body)

        if call.tool == "serial.read_can_diagnostics":
            path = f"/snapshot/uart?target={quote(call.target)}"
            status, body = self._fetch_json(path)
            if status != "ok" or not isinstance(body, dict):
                err = (
                    body if isinstance(body, ToolError) else ToolError(code="collector_unreachable")
                )
                return ToolResult(
                    tool=call.tool,
                    target=call.target,
                    status=status,  # type: ignore[arg-type]
                    source="live_uart",
                    origin_ecu=call.target,
                    transport="uart_text",
                    schema_version=schema_version,
                    validity="unknown",
                    freshness="unknown",
                    error=err,
                )
            return self._build_uart_result(call, body)

        return ToolResult(
            tool=call.tool,
            target=call.target,
            status="unsupported",
            source="live_can",
            schema_version=schema_version,
            error=ToolError(code="tool_not_supported"),
        )

    def _build_bus_state_result(self, call: ToolCall, body: dict[str, Any]) -> ToolResult:
        adapter = body.get("adapter")
        schema_version = (
            self.specification.id
            if isinstance(self.specification, LiveAideEcuSpecification)
            else None
        )
        if not isinstance(adapter, dict):
            return ToolResult(
                tool=call.tool,
                target=call.target,
                status="invalid",
                source="live_can",
                transport="socketcan",
                schema_version=schema_version,
                validity="invalid",
                freshness="unknown",
                error=ToolError(code="collector_unreachable"),
            )
        can_state = str(adapter.get("can_state") or "UNKNOWN")
        operstate = str(adapter.get("operstate") or "UNKNOWN")
        if can_state == "UNAVAILABLE":
            return ToolResult(
                tool=call.tool,
                target=call.target,
                status="unavailable",
                source="live_can",
                transport="socketcan",
                schema_version=schema_version,
                validity="unknown",
                freshness="unknown",
                error=ToolError(code="can_interface_unavailable"),
            )
        state_map = {
            "ERROR-ACTIVE": "active" if operstate == "UP" else "stopped",
            "ERROR-PASSIVE": "error_passive",
            "BUS-OFF": "bus_off",
            "STOPPED": "stopped",
        }
        normalized_state = state_map.get(can_state, "unknown")
        return ToolResult(
            tool=call.tool,
            target=call.target,
            status="ok",
            source="live_can",
            transport="socketcan",
            schema_version=schema_version,
            validity="valid",
            freshness="fresh",
            data={
                "state": normalized_state,
                "operstate": operstate,
                "can_state": can_state,
                "bitrate_bps": adapter.get("bitrate_bps"),
                "rx_packets": adapter.get("rx_packets", 0),
                "tx_packets": adapter.get("tx_packets", 0),
                "rx_errors": adapter.get("rx_errors", 0),
                "tx_errors": adapter.get("tx_errors", 0),
                "bus_errors": adapter.get("bus_errors", 0),
                "rx_dropped": adapter.get("rx_dropped", 0),
                "rx_missed": adapter.get("rx_missed", 0),
            },
        )

    def _build_capture_result(self, call: ToolCall, body: dict[str, Any]) -> ToolResult:
        is_aide = isinstance(self.specification, LiveAideEcuSpecification)
        schema_version = self.specification.id if is_aide else None
        capture_cfg = self._aide_capture_config(call.target) if is_aide else None
        if capture_cfg is not None:
            can_id, origin_ecu, timeout_ms, decoded_keys = capture_cfg
        else:
            can_id = self.specification.telemetry_can_id  # type: ignore[union-attr]
            origin_ecu = self.uart_target
            timeout_ms = self.specification.telemetry_timeout_ms  # type: ignore[union-attr]
            decoded_keys = ("magic_ok",)

        streams = body.get("streams")
        if not isinstance(streams, dict):
            return ToolResult(
                tool=call.tool,
                target=call.target,
                status="invalid",
                source="live_can",
                origin_ecu=origin_ecu,
                transport="socketcan",
                schema_version=schema_version,
                validity="invalid",
                freshness="unknown",
                error=ToolError(code="collector_unreachable"),
            )
        if is_aide:
            dlc_key = f"{can_id}:dlc{self.specification.periodic_dlc}"
            stream = streams.get(dlc_key)
            if stream is None:
                candidate = streams.get(can_id)
                if isinstance(candidate, dict) and "magic_ok" not in (
                    candidate.get("decoded") or {}
                ):
                    stream = candidate
        else:
            stream = streams.get(can_id)
        if not isinstance(stream, dict):
            return ToolResult(
                tool=call.tool,
                target=call.target,
                status="ok",
                source="live_can",
                origin_ecu=origin_ecu,
                transport="socketcan",
                schema_version=schema_version,
                validity="unknown",
                freshness="unknown",
                data={
                    "can_id": can_id,
                    "frame_count_in_window": 0,
                    "valid_frame_count_in_window": 0,
                    "invalid_frame_count_in_window": 0,
                    "sequence_gap_count": 0,
                    "consecutive_frozen_count": 0,
                },
            )
        decoded = stream.get("decoded")
        decoded_dict = decoded if isinstance(decoded, dict) else {}
        frame_count = int(stream.get("frame_count_in_window", 0))
        valid_frame = bool(decoded_dict.get("valid", False))
        valid_count = int(
            stream.get("valid_frame_count_in_window", frame_count if valid_frame else 0)
        )
        invalid_count = int(
            stream.get("invalid_frame_count_in_window", 0 if valid_frame else frame_count)
        )
        age_ms = stream.get("age_ms")
        is_fresh = frame_count > 0 and isinstance(age_ms, int | float) and age_ms <= timeout_ms
        freshness = "fresh" if is_fresh else ("stale" if frame_count > 0 else "unknown")
        validity = "valid" if valid_frame else "invalid"
        data: dict[str, Any] = {
            "can_id": can_id,
            "dlc": stream.get("dlc"),
            "frame_count_in_window": frame_count,
            "valid_frame_count_in_window": valid_count,
            "invalid_frame_count_in_window": invalid_count,
            "age_ms": age_ms,
            "last_alive_counter": stream.get("last_alive_counter"),
            "sequence_gap_count": stream.get("sequence_gap_count", 0),
            "consecutive_frozen_count": stream.get("consecutive_frozen_count", 0),
        }
        for key in decoded_keys:
            data[key] = decoded_dict.get(key)
        field_validity: dict[str, bool | None] = {}
        if is_aide and call.target == "a_thermal_status" and valid_frame:
            field_validity = {
                "temp_degc": decoded_dict.get("temp_valid"),
                "vib_rms_mg": decoded_dict.get("vib_valid"),
            }
        return ToolResult(
            tool=call.tool,
            target=call.target,
            status="ok",
            source="live_can",
            origin_ecu=origin_ecu,
            transport="socketcan",
            schema_version=schema_version,
            validity=validity,
            field_validity=field_validity,
            freshness=freshness,
            data=data,
        )

    def _build_uart_result(self, call: ToolCall, body: dict[str, Any]) -> ToolResult:
        uart_status = body.get("status")
        if uart_status == "unavailable":
            return ToolResult(
                tool=call.tool,
                target=call.target,
                status="unavailable",
                source="live_uart",
                origin_ecu=call.target,
                transport="uart_text",
                validity="unknown",
                freshness="unknown",
                error=ToolError(code="serial_unavailable"),
            )
        latest = body.get("latest")
        if uart_status == "timeout" or not isinstance(latest, dict):
            return ToolResult(
                tool=call.tool,
                target=call.target,
                status="timeout",
                source="live_uart",
                origin_ecu=call.target,
                transport="uart_text",
                validity="unknown",
                freshness="unknown",
                error=ToolError(code="serial_timeout"),
            )
        if uart_status != "ok":
            return ToolResult(
                tool=call.tool,
                target=call.target,
                status="invalid",
                source="live_uart",
                origin_ecu=call.target,
                transport="uart_text",
                validity="invalid",
                freshness="unknown",
                error=ToolError(code="serial_invalid"),
            )
        delta = body.get("delta")
        delta_dict = delta if isinstance(delta, dict) else {}
        sample_age_ms = body.get("sample_age_ms")
        is_fresh = (
            isinstance(sample_age_ms, int | float)
            and sample_age_ms <= self.specification.uart_timeout_ms
        )
        return ToolResult(
            tool=call.tool,
            target=call.target,
            status="ok",
            source="live_uart",
            origin_ecu=call.target,
            transport="uart_text",
            boot_id=f"epoch-{body.get('boot_epoch', 1)}",
            validity="valid",
            freshness="fresh" if is_fresh else "stale",
            data={
                "sample_age_ms": sample_age_ms,
                "boot_epoch": body.get("boot_epoch", 1),
                "t_ms": latest.get("t_ms"),
                "tec": latest.get("tec"),
                "rec": latest.get("rec"),
                "lec": latest.get("lec"),
                "ewg": latest.get("ewg"),
                "epv": latest.get("epv"),
                "boff": latest.get("boff"),
                "txreq": latest.get("txreq"),
                "txok": latest.get("txok"),
                "txabort": latest.get("txabort"),
                "txbusy": latest.get("txbusy"),
                "errcb": latest.get("errcb"),
                "errcode": latest.get("errcode"),
                "rx": latest.get("rx"),
                "last_rx_id": latest.get("last_rx_id"),
                "last_rx_dlc": latest.get("last_rx_dlc"),
                "epoch_continuous": delta_dict.get("epoch_continuous"),
                "interval_ms": delta_dict.get("interval_ms"),
                "txreq_delta": delta_dict.get("txreq_delta"),
                "txok_delta": delta_dict.get("txok_delta"),
                "txabort_delta": delta_dict.get("txabort_delta"),
                "txbusy_delta": delta_dict.get("txbusy_delta"),
                "errcb_delta": delta_dict.get("errcb_delta"),
                "rx_delta": delta_dict.get("rx_delta"),
            },
        )
