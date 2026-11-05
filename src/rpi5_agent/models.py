from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Scenario(StrEnum):
    HEALTHY = "healthy"
    BMS_TIMEOUT = "bms_timeout"
    BUS_OFF = "bus_off"


class DiagnoseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: Scenario = Scenario.BMS_TIMEOUT
    use_llm: bool = False


class ToolResult(BaseModel):
    tool: str
    target: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: Literal["ok", "unavailable", "error"]
    source: Literal["fixture"] = "fixture"
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class Finding(BaseModel):
    code: str
    severity: Literal["info", "warning", "error"]
    message: str


class Diagnosis(BaseModel):
    request_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    mode: Literal["fixture"] = "fixture"
    scenario: Scenario
    evidence: list[ToolResult]
    findings: list[Finding]
    summary: str
    llm_status: Literal["disabled", "generated", "unavailable"] = "disabled"
    llm_error: str | None = None
