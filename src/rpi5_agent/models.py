import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rpi5_agent.specification import FIXTURE_SPECIFICATION


class Scenario(StrEnum):
    HEALTHY = "healthy"
    BMS_TIMEOUT = "bms_timeout"
    BUS_OFF = "bus_off"
    ECU_UNRESPONSIVE = "ecu_unresponsive"
    ECU_REBOOTED = "ecu_rebooted"
    CRASH_RECORDED = "crash_recorded"
    CRASH_RECORD_ABSENT = "crash_record_absent"
    CRASH_RECORD_UNSUPPORTED = "crash_record_unsupported"
    CRASH_RECORD_TIMEOUT = "crash_record_timeout"
    CRASH_RECORD_INVALID = "crash_record_invalid"
    SYMBOLS_MISSING = "symbols_missing"
    SYMBOL_BUILD_MISMATCH = "symbol_build_mismatch"
    DEBUG_ACCESS_UNAVAILABLE = "debug_access_unavailable"


class DiagnosticProfile(StrEnum):
    PRODUCT = "product"
    BENCH = "bench"


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


class ToolError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    reason: Literal["profile_disabled", "denied", "probe_missing", "unknown"] | None = None
    service_id: int | None = Field(default=None, ge=0, le=255)
    nrc: int | None = Field(default=None, ge=0, le=255)


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str
    target: str


class ToolResult(BaseModel):
    tool: str
    target: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: Literal["ok", "unavailable", "error", "unsupported", "timeout", "invalid"]
    source: Literal["fixture"] = "fixture"
    origin_ecu: str | None = Field(default=None, max_length=64)
    transport: Literal["direct", "gateway_cache", "can_record", "bench_swd"] | None = None
    boot_id: str | None = Field(default=None, max_length=64)
    schema_version: str | None = Field(default=None, max_length=32)
    running_build_id: str | None = Field(default=None, max_length=64)
    record_id: str | None = Field(default=None, max_length=64)
    capture_boot_id: str | None = Field(default=None, max_length=64)
    capture_uptime_ms: int | None = Field(default=None, ge=0)
    record_build_id: str | None = Field(default=None, max_length=64)
    validity: Literal["valid", "invalid", "unknown"] = "unknown"
    field_validity: dict[str, bool | None] = Field(default_factory=dict)
    freshness: Literal["fresh", "stale", "unknown"] = "unknown"
    data: dict[str, Any] = Field(default_factory=dict)
    error: ToolError | str | None = None

    @field_validator("data")
    @classmethod
    def json_safe_data(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("Evidence data must be finite JSON values") from exc
        return value

    @field_validator("timestamp")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Evidence timestamp requires a timezone")
        return value


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
    assessment_specification_id: str = FIXTURE_SPECIFICATION.id
    scenario: Scenario
    diagnostic_profile: DiagnosticProfile = DiagnosticProfile.PRODUCT
    evidence: list[ToolResult]
    findings: list[Finding]
    summary: str
    llm_status: Literal["disabled", "generated", "unavailable"] = "disabled"
    llm_error: str | None = None
    explanation: ModelExplanation | None = None
    llm_metadata: LLMMetadata | None = None
    llm_routing: LLMRouting | None = None
