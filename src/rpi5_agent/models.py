from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Scenario(StrEnum):
    HEALTHY = "healthy"
    BMS_TIMEOUT = "bms_timeout"
    BUS_OFF = "bus_off"


class LLMTarget(StrEnum):
    AUTO = "auto"
    LOCAL = "local"
    EXTERNAL = "external"


class DiagnoseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: Scenario = Scenario.BMS_TIMEOUT
    use_llm: bool = False
    llm_target: LLMTarget = LLMTarget.AUTO

    @model_validator(mode="after")
    def target_requires_llm(self) -> "DiagnoseRequest":
        if not self.use_llm and self.llm_target != LLMTarget.AUTO:
            raise ValueError("Set use_llm=true when choosing an LLM target")
        return self


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


class ModelExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    hypothesis: str = Field(min_length=1, max_length=50)
    next_check: str = Field(min_length=1, max_length=50)

    @field_validator("hypothesis", "next_check")
    @classmethod
    def single_line(cls, value: str) -> str:
        if len(value.splitlines()) != 1 or any(ord(char) < 32 for char in value):
            raise ValueError("Explanation fields must be single-line text")
        return value


class LLMMetadata(BaseModel):
    finish_reason: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class LLMAttempt(BaseModel):
    backend: Literal["local", "pamout"]
    model: str
    status: Literal["generated", "unavailable"]
    error: str | None = None
    metadata: LLMMetadata | None = None


class LLMRouting(BaseModel):
    requested: LLMTarget
    backend: Literal["local", "pamout"]
    model: str
    reason: Literal[
        "explicit_local", "explicit_external", "error_findings", "local_default", "local_failure"
    ]
    attempts: list[LLMAttempt] = Field(default_factory=list)


class SummaryResult(BaseModel):
    explanation: ModelExplanation
    metadata: LLMMetadata
    routing: LLMRouting | None = None


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
    explanation: ModelExplanation | None = None
    llm_metadata: LLMMetadata | None = None
    llm_routing: LLMRouting | None = None
