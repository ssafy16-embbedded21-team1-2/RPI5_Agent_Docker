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
