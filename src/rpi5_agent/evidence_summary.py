"""Small, allowlisted model input; full evidence stays in the diagnostic response."""

import json

from rpi5_agent.errors import SummaryError
from rpi5_agent.models import Finding, ToolError, ToolResult
from rpi5_agent.specification import FIXTURE_SPECIFICATION, HW_TEST_SPECIFICATION

DATA_FIELDS = {
    "can.bus_state": {
        "state",
        "tec",
        "rec",
        "operstate",
        "can_state",
        "bitrate_bps",
        "rx_packets",
        "tx_packets",
        "rx_errors",
        "tx_errors",
        "bus_errors",
        "rx_dropped",
        "rx_missed",
    },
    "can.capture": {
        "can_id",
        "dlc",
        "frame_count_in_window",
        "valid_frame_count_in_window",
        "invalid_frame_count_in_window",
        "age_ms",
        "last_alive_counter",
        "sequence_gap_count",
        "magic_ok",
    },
    "gateway.snapshot": {
        "gateway_age_ms",
        "bms_age_ms",
        "bms_valid",
    },
    "can.read_boot_record": {"previous_boot_id", "current_boot_id"},
    "can.read_crash_record": {"record_present", "frame_valid", "fault_kind", "stacked_pc"},
    "serial.read_can_diagnostics": {
        "sample_age_ms",
        "boot_epoch",
        "t_ms",
        "tec",
        "rec",
        "lec",
        "ewg",
        "epv",
        "boff",
        "txreq",
        "txok",
        "txabort",
        "txbusy",
        "errcb",
        "errcode",
        "rx",
        "last_rx_id",
        "last_rx_dlc",
        "epoch_continuous",
        "interval_ms",
        "txreq_delta",
        "txok_delta",
        "txabort_delta",
        "txbusy_delta",
        "errcb_delta",
        "rx_delta",
    },
    "serial.get_identity": {"device_id", "boot_id", "firmware_build_id", "schema_version"},
}


def build_model_payload(evidence: list[ToolResult], findings: list[Finding]) -> dict:
    compact = []
    for item in evidence:
        view = item.model_dump(
            mode="json",
            exclude_none=True,
            include={
                "tool",
                "target",
                "status",
                "source",
                "origin_ecu",
                "transport",
                "freshness",
                "validity",
                "boot_id",
                "schema_version",
                "running_build_id",
                "record_id",
                "record_build_id",
                "capture_boot_id",
                "capture_uptime_ms",
            },
        )
        # Bound each scalar too; an unknown log, stack dump, error body, or ELF path
        # must not become model input just because it was placed inside data.
        view["data"] = {
            key: value
            for key, value in item.data.items()
            if key in DATA_FIELDS.get(item.tool, set())
            and (
                value is None
                or type(value) in {bool, int, float}
                or isinstance(value, str)
                and len(value) <= 64
            )
        }
        relevant = DATA_FIELDS.get(item.tool, set())
        view["field_validity"] = {
            key: value for key, value in item.field_validity.items() if key in relevant
        }
        if isinstance(item.error, ToolError):
            view["error"] = item.error.model_dump(exclude_none=True)
        # Legacy free-text errors are never forwarded.
        compact.append(view)
    is_live = any(
        item.source in {"live_can", "live_uart", "manual_measurement"} for item in evidence
    )
    spec = HW_TEST_SPECIFICATION if is_live else FIXTURE_SPECIFICATION
    payload = {
        "mode": "live" if is_live else "fixture",
        "assessment_specification": spec.model_dump(mode="json"),
        "evidence": compact,
        "findings": [item.model_dump(include={"code", "severity", "message"}) for item in findings],
    }
    # A byte cap is a guard against accidental large inputs, not a token guarantee.
    if len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()) > 6000:
        raise SummaryError("evidence_context_too_large")
    return payload
