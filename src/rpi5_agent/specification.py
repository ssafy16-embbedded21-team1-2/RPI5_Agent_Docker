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


class LiveAideEcuSpecification(BaseModel):
    """Phase 2 specification for Board A & B periodic CAN monitoring (PJT-19/PJT-20)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = "aide-ecu-v1"
    scope: Literal["live_aide_ecu"] = "live_aide_ecu"
    can_interface: str = "can0"
    expected_bitrate_bps: int = 500000
    a_fan_cmd_can_id: str = "0x080"
    a_thermal_status_can_id: str = "0x180"
    b_fan_status_can_id: str = "0x1C0"
    periodic_dlc: int = 8
    periodic_interval_ms: float = Field(default=100, gt=0, allow_inf_nan=False)
    a_cmd_timeout_ms: float = Field(default=300, gt=0, allow_inf_nan=False)
    status_timeout_ms: float = Field(default=500, gt=0, allow_inf_nan=False)
    alive_freeze_threshold: int = Field(default=3, ge=1, le=50)
    b_fault_encoding: Literal["code", "bitmask"] = "code"
    uart_target: str | None = None
    uart_timeout_ms: float = Field(default=2500, gt=0, allow_inf_nan=False)
    timeout_boundary: Literal["age_gt_timeout"] = "age_gt_timeout"


AIDE_ECU_SPECIFICATION = LiveAideEcuSpecification()
