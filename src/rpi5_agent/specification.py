"""Reviewed assessment limits for the synthetic bench, not a physical CAN specification."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AssessmentSpecification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    scope: Literal["fixture"] = "fixture"
    gateway_timeout_ms: float = Field(gt=0, allow_inf_nan=False)
    bms_timeout_ms: float = Field(gt=0, allow_inf_nan=False)
    timeout_boundary: Literal["age_gt_timeout"] = "age_gt_timeout"


# Existing fixture limits, now owned by the assessor rather than collected data.
# These values have NOT been agreed as the team's on-wire ECU protocol.
FIXTURE_SPECIFICATION = AssessmentSpecification(
    id="fixture-bench-v1", gateway_timeout_ms=500, bms_timeout_ms=1000
)


class LiveHwTestSpecification(BaseModel):
    """Reviewed assessment limits for Phase 1 live hw_test F103 CAN/UART pilot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = "hw-test-f103-v1"
    scope: Literal["live_hw_test"] = "live_hw_test"
    can_interface: str = "can0"
    expected_bitrate_bps: int = 500000
    telemetry_can_id: str = "0x100"
    telemetry_dlc: int = 4
    telemetry_timeout_ms: float = Field(default=500, gt=0, allow_inf_nan=False)
    uart_target: str = "hw_test_f103"
    uart_timeout_ms: float = Field(default=2500, gt=0, allow_inf_nan=False)
    timeout_boundary: Literal["age_gt_timeout"] = "age_gt_timeout"


HW_TEST_SPECIFICATION = LiveHwTestSpecification(
    id="hw-test-f103-v1", telemetry_timeout_ms=500, uart_timeout_ms=2500
)
