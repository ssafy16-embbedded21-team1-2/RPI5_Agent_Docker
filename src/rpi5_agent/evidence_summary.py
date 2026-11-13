"""Small, allowlisted model input; full evidence stays in the diagnostic response."""

import json

from rpi5_agent.errors import SummaryError
from rpi5_agent.models import Finding, ToolError, ToolResult
from rpi5_agent.specification import FIXTURE_SPECIFICATION

DATA_FIELDS = {
    "can.bus_state": {"state", "tec", "rec"},
    "gateway.snapshot": {
        "gateway_age_ms",
        "bms_age_ms",
        "bms_valid",
    },
    "can.read_boot_record": {"previous_boot_id", "current_boot_id"},
    "can.read_crash_record": {"record_present", "frame_valid", "fault_kind", "stacked_pc"},
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
    payload = {
        "mode": "fixture",
        "assessment_specification": FIXTURE_SPECIFICATION.model_dump(mode="json"),
        "evidence": compact,
        "findings": [item.model_dump(include={"code", "message"}) for item in findings],
    }
    # A byte cap is a guard against accidental large inputs, not a token guarantee.
    if len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()) > 6000:
        raise SummaryError("evidence_context_too_large")
    return payload
