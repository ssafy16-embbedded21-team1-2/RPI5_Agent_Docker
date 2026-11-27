"""Host-side CAN/UART collector engine and read-only HTTP bridge (stdlib only)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

DEFAULT_UART_DEVICE_BY_ID = (
    "/dev/serial/by-id/usb-STMicroelectronics_STM32_STLink_066CFF535551836687112626-if02"
)
HW_TEST_CAN_ID = 0x100
HW_TEST_MAGIC = b"\xa5\x5a\x00"

A_FAN_CMD_CAN_ID = 0x080
A_THERMAL_STATUS_CAN_ID = 0x180
B_FAN_STATUS_CAN_ID = 0x1C0
BOARD_B_A_FAN_CMD_CAN_ID = 0x100
BOARD_B_A_THERMAL_STATUS_CAN_ID = 0x300
BOARD_B_FAN_STATUS_CAN_ID = 0x340
TEMP_AMBIENT_INVALID_RAW = 0x7FFF
VIB_RMS_INVALID_RAW = 0xFFFF

UART_LINE_PATTERN = re.compile(
    r"^t=(\d+) ms TEC=(\d+) REC=(\d+) LEC=(\d+) EWG=(\d+) EPV=(\d+) BOFF=(\d+) "
    r"\| txreq=(\d+) txok=(\d+) txabort=(\d+) txbusy=(\d+) errcb=(\d+) "
    r"errcode=(0x[0-9A-Fa-f]+) \| rx=(\d+) last=(0x[0-9A-Fa-f]+) dlc=(\d+)$"
)

_MONOTONIC_COUNTER_KEYS = ("txreq", "txok", "txabort", "txbusy", "errcb", "rx")


def format_can_id(can_id: int) -> str:
    """Format an 11-bit standard CAN ID as 3-digit uppercase hex (e.g., 0x080)."""
    return f"0x{can_id:03X}"


def parse_hw_test_can_frame(
    can_id: int, data: bytes | bytearray | list[int], *, dlc: int | None = None
) -> dict[str, Any]:
    """Decode and validate a Phase 1 hw_test 0x100 classic CAN frame."""
    raw = bytes(data)
    actual_dlc = dlc if dlc is not None else len(raw)
    if can_id == HW_TEST_CAN_ID and actual_dlc == 4 and len(raw) == 4 and raw[:3] == HW_TEST_MAGIC:
        return {
            "valid": True,
            "magic_ok": True,
            "alive_counter": raw[3],
            "dlc": 4,
        }
    return {
        "valid": False,
        "magic_ok": False,
        "alive_counter": None,
        "dlc": actual_dlc,
    }


def parse_a_fan_cmd_frame(
    can_id: int,
    data: bytes | bytearray | list[int],
    *,
    dlc: int | None = None,
    expected_can_id: int = A_FAN_CMD_CAN_ID,
) -> dict[str, Any]:
    """Decode and validate a Phase 2 A_FAN_CMD (0x080) classic CAN frame."""
    raw = bytes(data)
    actual_dlc = dlc if dlc is not None else len(raw)
    if can_id == expected_can_id and actual_dlc == 8 and len(raw) == 8 and 0 <= raw[0] <= 100:
        return {
            "valid": True,
            "dlc": 8,
            "duty_target": raw[0],
            "alive_counter": raw[7],
        }
    return {
        "valid": False,
        "dlc": actual_dlc,
        "duty_target": None,
        "alive_counter": None,
    }


def parse_a_thermal_status_frame(
    can_id: int,
    data: bytes | bytearray | list[int],
    *,
    dlc: int | None = None,
    expected_can_id: int = A_THERMAL_STATUS_CAN_ID,
) -> dict[str, Any]:
    """Decode and validate a Phase 2 A_THERMAL_STATUS (0x180) classic CAN frame."""
    raw = bytes(data)
    actual_dlc = dlc if dlc is not None else len(raw)
    if can_id == expected_can_id and actual_dlc == 8 and len(raw) == 8:
        temp_raw = int.from_bytes(raw[0:2], "little", signed=True)
        duty_target = raw[2]
        vib_raw = int.from_bytes(raw[3:5], "little", signed=False)
        a_state = raw[5] & 0x0F
        thermal_state = (raw[5] >> 4) & 0x0F
        fault_flags = raw[6]
        alive_counter = raw[7]
        if 0 <= duty_target <= 100 and a_state <= 3 and thermal_state <= 3:
            temp_valid = temp_raw != TEMP_AMBIENT_INVALID_RAW
            vib_valid = vib_raw != VIB_RMS_INVALID_RAW
            return {
                "valid": True,
                "dlc": 8,
                "temp_degc": round(temp_raw * 0.1, 1) if temp_valid else None,
                "temp_valid": temp_valid,
                "duty_target": duty_target,
                "vib_rms_mg": vib_raw if vib_valid else None,
                "vib_valid": vib_valid,
                "a_state": a_state,
                "thermal_state": thermal_state,
                "fault_flags": fault_flags,
                "alive_counter": alive_counter,
            }
    return {
        "valid": False,
        "dlc": actual_dlc,
        "temp_degc": None,
        "temp_valid": None,
        "duty_target": None,
        "vib_rms_mg": None,
        "vib_valid": None,
        "a_state": None,
        "thermal_state": None,
        "fault_flags": None,
        "alive_counter": None,
    }


def parse_b_fan_status_frame(
    can_id: int,
    data: bytes | bytearray | list[int],
    *,
    dlc: int | None = None,
    expected_can_id: int = B_FAN_STATUS_CAN_ID,
) -> dict[str, Any]:
    """Decode and validate a Phase 2 B_FAN_STATUS (0x1C0) classic CAN frame."""
    raw = bytes(data)
    actual_dlc = dlc if dlc is not None else len(raw)
    if can_id == expected_can_id and actual_dlc == 8 and len(raw) == 8:
        fan_rpm = int.from_bytes(raw[0:2], "little", signed=False)
        duty_actual = raw[2]
        fan_state = raw[3]
        fault_code = raw[4]
        alive_counter = raw[7]
        if 0 <= duty_actual <= 100 and fan_state <= 4:
            return {
                "valid": True,
                "dlc": 8,
                "fan_rpm": fan_rpm,
                "duty_actual": duty_actual,
                "fan_state": fan_state,
                "fault_code": fault_code,
                "alive_counter": alive_counter,
            }
    return {
        "valid": False,
        "dlc": actual_dlc,
        "fan_rpm": None,
        "duty_actual": None,
        "fan_state": None,
        "fault_code": None,
        "alive_counter": None,
    }


def compute_alive_counter_gap(prev_counter: int | None, curr_counter: int) -> int:
    """Return missed uint8 sequence steps between two distinct valid alive_counter values."""
    if prev_counter is None or curr_counter == prev_counter:
        return 0
    expected = (prev_counter + 1) & 0xFF
    if curr_counter == expected:
        return 0
    return (curr_counter - prev_counter - 1) & 0xFF


def is_alive_counter_frozen(prev_counter: int | None, curr_counter: int) -> bool:
    """Return True when a consecutive valid frame repeats the exact same alive_counter."""
    return prev_counter is not None and curr_counter == prev_counter


DEFAULT_CAN_CODECS: dict[int, Callable[..., dict[str, Any]]] = {
    HW_TEST_CAN_ID: parse_hw_test_can_frame,
    A_FAN_CMD_CAN_ID: parse_a_fan_cmd_frame,
    A_THERMAL_STATUS_CAN_ID: parse_a_thermal_status_frame,
    B_FAN_STATUS_CAN_ID: parse_b_fan_status_frame,
}


@dataclass(frozen=True, slots=True)
class _CanFrameEvent:
    rx_utc: datetime
    dlc: int
    valid: bool
    decoded: dict[str, Any]
    alive_counter: int | None
    gap: int
    consecutive_frozen: int


class _CanStreamState:
    def __init__(self, max_events: int) -> None:
        self.events: deque[_CanFrameEvent] = deque(maxlen=max_events)
        self.last_valid_counter: int | None = None
        self.consecutive_frozen: int = 0
        self.last_event: _CanFrameEvent | None = None


class CanCollectorBuffer:
    """Thread-safe sliding-window accumulator for configured Classic CAN frames."""

    def __init__(
        self,
        *,
        max_events: int = 5000,
        codecs: dict[int, Callable[..., dict[str, Any]]] | None = None,
        can_id_scheme: Literal["auto", "pjt20", "board_b"] = "auto",
        a_fan_cmd_id: int | None = None,
        a_thermal_status_id: int | None = None,
        b_fan_status_id: int | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._max_events = max_events
        self.can_id_scheme = can_id_scheme
        self._custom_codecs = dict(codecs) if codecs is not None else None
        self._id_routes, self._dlc8_shared_route = self._build_routes(
            can_id_scheme,
            a_fan_cmd_id=a_fan_cmd_id,
            a_thermal_status_id=a_thermal_status_id,
            b_fan_status_id=b_fan_status_id,
        )
        self._streams: dict[str, _CanStreamState] = {}

    @staticmethod
    def _build_routes(
        can_id_scheme: Literal["auto", "pjt20", "board_b"],
        *,
        a_fan_cmd_id: int | None,
        a_thermal_status_id: int | None,
        b_fan_status_id: int | None,
    ) -> tuple[
        dict[int, tuple[str, Callable[..., dict[str, Any]]]],
        tuple[str, Callable[..., dict[str, Any]]] | None,
    ]:
        routes: dict[int, tuple[str, Callable[..., dict[str, Any]]]] = {}
        dlc8_shared: tuple[str, Callable[..., dict[str, Any]]] | None = None

        if can_id_scheme == "auto":
            routes[HW_TEST_CAN_ID] = (format_can_id(HW_TEST_CAN_ID), parse_hw_test_can_frame)
            for cid, parser in (
                (A_FAN_CMD_CAN_ID, parse_a_fan_cmd_frame),
                (A_THERMAL_STATUS_CAN_ID, parse_a_thermal_status_frame),
                (B_FAN_STATUS_CAN_ID, parse_b_fan_status_frame),
                (BOARD_B_A_THERMAL_STATUS_CAN_ID, parse_a_thermal_status_frame),
                (BOARD_B_FAN_STATUS_CAN_ID, parse_b_fan_status_frame),
            ):
                routes[cid] = (format_can_id(cid), partial(parser, expected_can_id=cid))
            dlc8_shared = (
                f"{format_can_id(HW_TEST_CAN_ID)}:dlc8",
                partial(parse_a_fan_cmd_frame, expected_can_id=HW_TEST_CAN_ID),
            )
        elif can_id_scheme == "pjt20":
            routes[HW_TEST_CAN_ID] = (format_can_id(HW_TEST_CAN_ID), parse_hw_test_can_frame)
            for cid, parser in (
                (A_FAN_CMD_CAN_ID, parse_a_fan_cmd_frame),
                (A_THERMAL_STATUS_CAN_ID, parse_a_thermal_status_frame),
                (B_FAN_STATUS_CAN_ID, parse_b_fan_status_frame),
            ):
                routes[cid] = (format_can_id(cid), partial(parser, expected_can_id=cid))
        else:  # board_b
            routes[BOARD_B_A_FAN_CMD_CAN_ID] = (
                f"{format_can_id(BOARD_B_A_FAN_CMD_CAN_ID)}:dlc8",
                partial(parse_a_fan_cmd_frame, expected_can_id=BOARD_B_A_FAN_CMD_CAN_ID),
            )
            routes[BOARD_B_A_THERMAL_STATUS_CAN_ID] = (
                format_can_id(BOARD_B_A_THERMAL_STATUS_CAN_ID),
                partial(
                    parse_a_thermal_status_frame, expected_can_id=BOARD_B_A_THERMAL_STATUS_CAN_ID
                ),
            )
            routes[BOARD_B_FAN_STATUS_CAN_ID] = (
                format_can_id(BOARD_B_FAN_STATUS_CAN_ID),
                partial(parse_b_fan_status_frame, expected_can_id=BOARD_B_FAN_STATUS_CAN_ID),
            )

        for cid, parser in (
            (a_fan_cmd_id, parse_a_fan_cmd_frame),
            (a_thermal_status_id, parse_a_thermal_status_frame),
            (b_fan_status_id, parse_b_fan_status_frame),
        ):
            if cid is None:
                continue
            bound = partial(parser, expected_can_id=cid)
            if cid == HW_TEST_CAN_ID:
                if can_id_scheme == "auto":
                    dlc8_shared = (f"{format_can_id(cid)}:dlc8", bound)
                elif can_id_scheme == "board_b":
                    routes[cid] = (f"{format_can_id(cid)}:dlc8", bound)
                else:
                    routes[cid] = (format_can_id(cid), bound)
            else:
                routes[cid] = (format_can_id(cid), bound)
        return routes, dlc8_shared

    def _resolve_route(
        self, can_id: int, actual_dlc: int
    ) -> tuple[str, Callable[..., dict[str, Any]]] | None:
        if self._custom_codecs is not None:
            codec = self._custom_codecs.get(can_id)
            if codec is None:
                return None
            return (format_can_id(can_id), codec)
        if can_id == HW_TEST_CAN_ID and actual_dlc == 8 and self._dlc8_shared_route is not None:
            return self._dlc8_shared_route
        return self._id_routes.get(can_id)

    def record_frame(
        self,
        can_id: int,
        data: bytes | bytearray | list[int],
        *,
        dlc: int | None = None,
        rx_utc: datetime | None = None,
    ) -> dict[str, Any]:
        ts = rx_utc if rx_utc is not None else datetime.now(UTC)
        actual_dlc = dlc if dlc is not None else len(bytes(data))
        route = self._resolve_route(can_id, actual_dlc)
        if route is None:
            return {"valid": False, "alive_counter": None, "dlc": actual_dlc}
        stream_key, codec = route
        parsed = codec(can_id, data, dlc=dlc)
        with self._lock:
            stream = self._streams.get(stream_key)
            if stream is None:
                stream = _CanStreamState(self._max_events)
                self._streams[stream_key] = stream
            gap = 0
            if parsed["valid"] and parsed.get("alive_counter") is not None:
                counter = int(parsed["alive_counter"])
                if is_alive_counter_frozen(stream.last_valid_counter, counter):
                    stream.consecutive_frozen += 1
                else:
                    gap = compute_alive_counter_gap(stream.last_valid_counter, counter)
                    stream.consecutive_frozen = 0
                    stream.last_valid_counter = counter
            else:
                stream.consecutive_frozen = 0
            event = _CanFrameEvent(
                rx_utc=ts,
                dlc=int(parsed["dlc"]),
                valid=bool(parsed["valid"]),
                decoded=dict(parsed),
                alive_counter=parsed.get("alive_counter"),
                gap=gap,
                consecutive_frozen=stream.consecutive_frozen,
            )
            stream.events.append(event)
            stream.last_event = event
        return parsed

    def snapshot(
        self,
        *,
        interface: str,
        adapter: dict[str, Any],
        window_ms: int = 5000,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        ref_now = now if now is not None else datetime.now(UTC)
        with self._lock:
            stream_items = [
                (stream_key, list(stream.events), stream.last_event)
                for stream_key, stream in self._streams.items()
            ]

        streams: dict[str, Any] = {}
        for stream_key, events, last_event in stream_items:
            in_window = [
                ev for ev in events if (ref_now - ev.rx_utc).total_seconds() * 1000.0 <= window_ms
            ]
            if in_window:
                latest = in_window[-1]
                age_ms = max(0.0, (ref_now - latest.rx_utc).total_seconds() * 1000.0)
                valid_count = sum(1 for ev in in_window if ev.valid)
                invalid_count = len(in_window) - valid_count
                streams[stream_key] = {
                    "dlc": latest.dlc,
                    "frame_count_in_window": len(in_window),
                    "valid_frame_count_in_window": valid_count,
                    "invalid_frame_count_in_window": invalid_count,
                    "last_rx_utc": latest.rx_utc.isoformat(),
                    "age_ms": round(age_ms, 2),
                    "last_alive_counter": latest.alive_counter,
                    "sequence_gap_count": sum(ev.gap for ev in in_window),
                    "consecutive_frozen_count": latest.consecutive_frozen,
                    "decoded": dict(latest.decoded),
                }
            elif last_event is not None:
                age_ms = max(0.0, (ref_now - last_event.rx_utc).total_seconds() * 1000.0)
                streams[stream_key] = {
                    "dlc": last_event.dlc,
                    "frame_count_in_window": 0,
                    "valid_frame_count_in_window": 0,
                    "invalid_frame_count_in_window": 0,
                    "last_rx_utc": last_event.rx_utc.isoformat(),
                    "age_ms": round(age_ms, 2),
                    "last_alive_counter": last_event.alive_counter,
                    "sequence_gap_count": 0,
                    "consecutive_frozen_count": 0,
                    "decoded": dict(last_event.decoded),
                }

        return {
            "collected_at": ref_now.isoformat(),
            "interface": interface,
            "adapter": adapter,
            "window_ms": window_ms,
            "streams": streams,
        }


def parse_hw_test_uart_line(line: str) -> dict[str, Any] | None:
    """Parse one complete ASCII line from hw_test CAN normal v1 firmware."""
    cleaned = line.rstrip("\r\n")
    if not cleaned or any(ord(ch) < 32 or ord(ch) > 126 for ch in cleaned):
        return None
    match = UART_LINE_PATTERN.match(cleaned)
    if match is None:
        return None
    return {
        "t_ms": int(match.group(1)),
        "tec": int(match.group(2)),
        "rec": int(match.group(3)),
        "lec": int(match.group(4)),
        "ewg": int(match.group(5)),
        "epv": int(match.group(6)),
        "boff": int(match.group(7)),
        "txreq": int(match.group(8)),
        "txok": int(match.group(9)),
        "txabort": int(match.group(10)),
        "txbusy": int(match.group(11)),
        "errcb": int(match.group(12)),
        "errcode": match.group(13),
        "rx": int(match.group(14)),
        "last_rx_id": match.group(15),
        "last_rx_dlc": int(match.group(16)),
    }


def compute_uart_delta(prev: dict[str, Any] | None, curr: dict[str, Any]) -> dict[str, Any] | None:
    """Compute counter deltas when prev and curr belong to the same monotonic boot epoch."""
    if prev is None:
        return None
    interval_ms = int(curr["t_ms"]) - int(prev["t_ms"])
    monotonic = interval_ms > 0 and all(
        int(curr[key]) >= int(prev[key]) for key in _MONOTONIC_COUNTER_KEYS
    )
    if not monotonic:
        return {
            "interval_ms": None,
            "epoch_continuous": False,
            "txreq_delta": None,
            "txok_delta": None,
            "txabort_delta": None,
            "txbusy_delta": None,
            "errcb_delta": None,
            "rx_delta": None,
        }
    return {
        "interval_ms": interval_ms,
        "epoch_continuous": True,
        "txreq_delta": int(curr["txreq"]) - int(prev["txreq"]),
        "txok_delta": int(curr["txok"]) - int(prev["txok"]),
        "txabort_delta": int(curr["txabort"]) - int(prev["txabort"]),
        "txbusy_delta": int(curr["txbusy"]) - int(prev["txbusy"]),
        "errcb_delta": int(curr["errcb"]) - int(prev["errcb"]),
        "rx_delta": int(curr["rx"]) - int(prev["rx"]),
    }


class UartCollectorBuffer:
    """Thread-safe UART stream buffer discarding startup bytes and tracking 2-sample deltas."""

    def __init__(self, *, discard_initial_partial: bool = True) -> None:
        self._lock = threading.Lock()
        self._discard_initial_partial = discard_initial_partial
        self._awaiting_sync = discard_initial_partial
        self._rx_buf = bytearray()
        self._prev: dict[str, Any] | None = None
        self._latest: dict[str, Any] | None = None
        self._latest_rx_utc: datetime | None = None
        self._delta: dict[str, Any] | None = None
        self._boot_epoch: int = 1
        self._parsed_lines: int = 0
        self._rejected_lines: int = 0

    def reset_stream(self) -> None:
        with self._lock:
            self._rx_buf.clear()
            self._awaiting_sync = self._discard_initial_partial

    def feed_bytes(self, chunk: bytes, *, now: datetime | None = None) -> int:
        """Ingest raw serial bytes and parse complete newline-delimited frames."""
        if not chunk:
            return 0
        ref_now = now if now is not None else datetime.now(UTC)
        accepted = 0
        with self._lock:
            self._rx_buf.extend(chunk)
            if len(self._rx_buf) > 16384 and b"\n" not in self._rx_buf:
                # Guard memory and resync cleanly if a noisy port never sends LF.
                self._rx_buf.clear()
                self._awaiting_sync = True
                return 0
            while b"\n" in self._rx_buf:
                raw_line, rest = self._rx_buf.split(b"\n", 1)
                self._rx_buf = bytearray(rest)
                if self._awaiting_sync:
                    # Drop the initial partial line / buffered ST-LINK VCP noise after open.
                    self._awaiting_sync = False
                    continue
                stripped = bytes(raw_line).rstrip(b"\r")
                if not stripped or any(byte < 32 or byte > 126 for byte in stripped):
                    self._rejected_lines += 1
                    continue
                parsed = parse_hw_test_uart_line(stripped.decode("ascii"))
                if parsed is None:
                    self._rejected_lines += 1
                    continue
                prev = self._latest
                delta = compute_uart_delta(prev, parsed)
                if delta is not None and not delta["epoch_continuous"]:
                    self._boot_epoch += 1
                self._prev = prev
                self._latest = parsed
                self._latest_rx_utc = ref_now
                self._delta = delta
                self._parsed_lines += 1
                accepted += 1
        return accepted

    def snapshot(
        self,
        *,
        target: str,
        device_by_id: str,
        port_available: bool,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        ref_now = now if now is not None else datetime.now(UTC)
        with self._lock:
            latest = dict(self._latest) if self._latest is not None else None
            delta = dict(self._delta) if self._delta is not None else None
            latest_utc = self._latest_rx_utc
            boot_epoch = self._boot_epoch
            parsed_lines = self._parsed_lines
            rejected_lines = self._rejected_lines

        if latest is None or latest_utc is None:
            status = "timeout" if port_available else "unavailable"
            sample_age_ms = None
        else:
            sample_age_ms = round(max(0.0, (ref_now - latest_utc).total_seconds() * 1000.0), 2)
            status = "ok" if port_available else "unavailable"

        return {
            "collected_at": ref_now.isoformat(),
            "target": target,
            "device_by_id": device_by_id,
            "status": status,
            "port_available": port_available,
            "sample_age_ms": sample_age_ms,
            "boot_epoch": boot_epoch,
            "parsed_lines": parsed_lines,
            "rejected_lines": rejected_lines,
            "latest": latest,
            "delta": delta,
        }


def parse_ip_link_can_json(raw_json: str, interface: str = "can0") -> dict[str, Any]:
    """Extract normalized adapter telemetry from `ip -j -details -statistics link show`."""
    payload = json.loads(raw_json)
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"Interface {interface} not found in ip link JSON")
    entry = next(
        (item for item in payload if isinstance(item, dict) and item.get("ifname") == interface),
        payload[0],
    )
    if not isinstance(entry, dict):
        raise ValueError("Invalid ip link entry")
    flags = entry.get("flags") or []
    linkinfo = entry.get("linkinfo") or {}
    info_kind = str(linkinfo.get("info_kind") or "")
    info_data = linkinfo.get("info_data") or {}
    info_xstats = linkinfo.get("info_xstats") or {}
    bittiming = info_data.get("bittiming") or {}
    stats64 = entry.get("stats64") or {}
    rx_stats = stats64.get("rx") or {}
    tx_stats = stats64.get("tx") or {}

    raw_can_state = info_data.get("state")
    if not raw_can_state and info_kind == "vcan":
        can_state = "ERROR-ACTIVE" if "UP" in flags else "STOPPED"
    else:
        can_state = str(raw_can_state or "UNKNOWN")
    raw_operstate = str(entry.get("operstate") or "UNKNOWN")
    is_up = raw_operstate == "UP" or ("UP" in flags and can_state != "STOPPED")
    operstate = "UP" if is_up else raw_operstate

    return {
        "operstate": operstate,
        "can_state": can_state,
        "bitrate_bps": int(bittiming["bitrate"]) if "bitrate" in bittiming else None,
        "rx_packets": int(rx_stats.get("packets", 0)),
        "tx_packets": int(tx_stats.get("packets", 0)),
        "rx_errors": int(rx_stats.get("errors", 0)),
        "tx_errors": int(tx_stats.get("errors", 0)),
        "bus_errors": int(info_xstats.get("bus_error", 0)),
        "rx_dropped": int(rx_stats.get("dropped", 0)),
        "rx_missed": int(rx_stats.get("missed_errors", rx_stats.get("over_errors", 0))),
    }


def read_ip_link_can_state(interface: str = "can0") -> dict[str, Any]:
    """Query local Linux SocketCAN interface status via iproute2."""
    proc = subprocess.run(
        ["ip", "-j", "-details", "-statistics", "link", "show", interface],
        capture_output=True,
        text=True,
        timeout=2.0,
        check=True,
    )
    return parse_ip_link_can_json(proc.stdout, interface=interface)


def create_collector_http_server(
    host: str,
    port: int,
    *,
    can_buffer: CanCollectorBuffer,
    uart_buffer: UartCollectorBuffer,
    can_interface: str = "can0",
    uart_target: str = "hw_test_f103",
    uart_device_path: str = DEFAULT_UART_DEVICE_BY_ID,
    adapter_reader: Callable[[str], dict[str, Any]] | None = None,
    port_probe: Callable[[], bool] | None = None,
) -> ThreadingHTTPServer:
    """Create the strictly read-only host HTTP snapshot server."""
    read_adapter = adapter_reader if adapter_reader is not None else read_ip_link_can_state
    check_port = (
        port_probe if port_probe is not None else (lambda: os.path.exists(uart_device_path))
    )

    class CollectorHandler(BaseHTTPRequestHandler):
        def _send_json(self, status: int, body: dict[str, Any]) -> None:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            if parsed.path == "/health":
                try:
                    adapter = read_adapter(can_interface)
                    can_present = True
                except Exception:
                    adapter = None
                    can_present = False
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "read_only": True,
                        "can_interface": can_interface,
                        "can_present": can_present,
                        "can_state": adapter.get("can_state") if adapter else None,
                        "can_id_scheme": getattr(can_buffer, "can_id_scheme", "auto"),
                        "uart_target": uart_target,
                        "uart_device_by_id": uart_device_path,
                        "uart_port_present": check_port(),
                    },
                )
                return

            if parsed.path == "/snapshot/can":
                req_if = query.get("interface", [can_interface])[0]
                if req_if != can_interface:
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "unknown_can_interface", "interface": req_if},
                    )
                    return
                try:
                    raw_win = int(query.get("window_ms", ["5000"])[0])
                    window_ms = max(100, min(raw_win, 60000))
                except ValueError:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_window_ms"})
                    return
                try:
                    adapter = read_adapter(can_interface)
                except Exception as exc:
                    adapter = {
                        "operstate": "UNKNOWN",
                        "can_state": "UNAVAILABLE",
                        "bitrate_bps": None,
                        "rx_packets": 0,
                        "tx_packets": 0,
                        "rx_errors": 0,
                        "tx_errors": 0,
                        "bus_errors": 0,
                        "rx_dropped": 0,
                        "rx_missed": 0,
                        "probe_error": type(exc).__name__,
                    }
                self._send_json(
                    HTTPStatus.OK,
                    can_buffer.snapshot(
                        interface=can_interface, adapter=adapter, window_ms=window_ms
                    ),
                )
                return

            if parsed.path == "/snapshot/uart":
                req_target = query.get("target", [uart_target])[0]
                if req_target != uart_target:
                    self._send_json(
                        HTTPStatus.NOT_FOUND, {"error": "unknown_uart_target", "target": req_target}
                    )
                    return
                self._send_json(
                    HTTPStatus.OK,
                    uart_buffer.snapshot(
                        target=uart_target,
                        device_by_id=uart_device_path,
                        port_available=check_port(),
                    ),
                )
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_POST(self) -> None:
            self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read_only_collector"})

        def do_PUT(self) -> None:
            self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read_only_collector"})

        def do_DELETE(self) -> None:
            self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read_only_collector"})

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    return ThreadingHTTPServer((host, port), CollectorHandler)
