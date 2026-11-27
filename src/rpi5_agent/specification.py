"""Reviewed assessment limits for the synthetic bench, not a physical CAN specification."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def normalize_standard_can_id(value: str | int) -> str:
    """Validate and normalize an 11-bit standard CAN ID (0x000..0x7FF) to 3-digit hex."""
    if isinstance(value, bool):
        raise ValueError("CAN ID must be an integer or 0x-prefixed hex string")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        cleaned = value.strip()
        if not cleaned.lower().startswith("0x") or len(cleaned) <= 2:
            raise ValueError("CAN ID string must start with 0x (e.g. 0x100)")
        try:
            parsed = int(cleaned, 16)
        except ValueError as exc:
            raise ValueError("CAN ID string must be valid hexadecimal") from exc
    else:
        raise ValueError("CAN ID must be an integer or 0x-prefixed hex string")
    if not 0 <= parsed <= 0x7FF:
        raise ValueError("Standard CAN ID must be between 0x000 and 0x7FF")
    return f"0x{parsed:03X}"


AideCanIdScheme = Literal["pjt20", "board_b", "custom"]

AIDE_CAN_ID_SCHEMES: dict[Literal["pjt20", "board_b"], dict[str, str | float]] = {
    "pjt20": {
        "a_fan_cmd_can_id": "0x080",
        "a_thermal_status_can_id": "0x180",
        "b_fan_status_can_id": "0x1C0",
        "a_cmd_timeout_ms": 300.0,
    },
    "board_b": {
        "a_fan_cmd_can_id": "0x100",
        "a_thermal_status_can_id": "0x300",
        "b_fan_status_can_id": "0x340",
        "a_cmd_timeout_ms": 500.0,
    },
}


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
    can_id_scheme: AideCanIdScheme = "pjt20"
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

    @field_validator("a_fan_cmd_can_id", "a_thermal_status_can_id", "b_fan_status_can_id")
    @classmethod
    def validate_periodic_can_id(cls, value: str) -> str:
        return normalize_standard_can_id(value)

    @model_validator(mode="after")
    def validate_distinct_can_ids(self) -> "LiveAideEcuSpecification":
        ids = {self.a_fan_cmd_can_id, self.a_thermal_status_can_id, self.b_fan_status_can_id}
        if len(ids) != 3:
            raise ValueError(
                "A_FAN_CMD, A_THERMAL_STATUS, and B_FAN_STATUS CAN IDs must be distinct"
            )
        return self


AIDE_ECU_SPECIFICATION = LiveAideEcuSpecification()
