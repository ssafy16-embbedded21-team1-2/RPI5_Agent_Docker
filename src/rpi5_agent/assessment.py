"""Deterministic assessment of decoded evidence, independent of acquisition order."""

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from rpi5_agent.models import Finding, ToolError, ToolResult
from rpi5_agent.specification import FIXTURE_SPECIFICATION


class DecodedData(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", allow_inf_nan=False)


class BusStateData(DecodedData):
    state: str


class GatewayData(DecodedData):
    gateway_age_ms: float = Field(ge=0)
    bms_age_ms: float = Field(ge=0)
    bms_valid: bool


class BootData(DecodedData):
    previous_boot_id: str = Field(min_length=1, max_length=64)
    current_boot_id: str = Field(min_length=1, max_length=64)


class CrashData(DecodedData):
    record_present: bool
    frame_valid: bool | None = None
    stacked_pc: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)


def finding(code: str, message: str, severity: str = "warning") -> Finding:
    return Finding(code=code, severity=severity, message=message)


def decode(item: ToolResult | None, model: type[DecodedData]):
    if item is None or item.status != "ok" or item.validity == "invalid":
        return None
    if any(
        item.field_validity.get(key) is False
        for key, field in model.model_fields.items()
        if field.is_required()
    ):
        return None
    try:
        return model.model_validate(item.data)
    except ValidationError:
        return None


def evaluate(evidence: list[ToolResult]) -> list[Finding]:
    grouped: dict[tuple[str, str], list[ToolResult]] = {}
    for item in evidence:
        grouped.setdefault((item.tool, item.target), []).append(item)
    findings = []
    for values in grouped.values():
        if len(values) > 1:
            findings.append(
                finding("AMBIGUOUS_EVIDENCE", "동일 도구·대상의 중복 증거를 판정에서 제외했습니다.")
            )

    def get(tool: str, target: str) -> ToolResult | None:
        values = grouped.get((tool, target), [])
        return values[0] if len(values) == 1 else None

    bus = decode(get("can.bus_state", "rpi_adapter"), BusStateData)
    bus_off = bus is not None and bus.state == "bus_off"
    if bus_off:
        findings.extend(
            [
                finding(
                    "LOCAL_ADAPTER_BUS_OFF", "RPi 로컬 CAN 어댑터가 Bus-Off 상태입니다.", "error"
                ),
                finding(
                    "REMOTE_STATE_UNKNOWN",
                    "게이트웨이와 BMS의 현재 상태는 수집된 증거로 판단할 수 없습니다.",
                ),
            ]
        )
    else:
        if bus is None or bus.state != "active":
            findings.append(
                finding(
                    "LOCAL_ADAPTER_STATE_UNKNOWN",
                    "로컬 CAN 어댑터의 정상 상태를 확인하지 못했습니다.",
                )
            )
        gateway = get("gateway.snapshot", "cell1")
        data = decode(gateway, GatewayData)
        if data is None:
            code = (
                "GATEWAY_EVIDENCE_UNAVAILABLE"
                if gateway is None or gateway.status != "ok"
                else "GATEWAY_EVIDENCE_INVALID"
            )
            findings.append(
                finding(
                    code,
                    "게이트웨이의 유효한 증거를 수집하지 못했습니다. 원격 ECU 고장은 미확인입니다.",
                )
            )
        elif (
            gateway.freshness == "stale"
            or data.gateway_age_ms > FIXTURE_SPECIFICATION.gateway_timeout_ms
        ):
            findings.append(
                finding(
                    "GATEWAY_STALE",
                    "게이트웨이 집계 데이터가 오래되어 현재 BMS 상태를 확정할 수 없습니다.",
                )
            )
        else:
            findings.append(
                finding("GATEWAY_FRESH", "게이트웨이 집계 데이터는 최근에 갱신되었습니다.", "info")
            )
            if data.bms_age_ms > FIXTURE_SPECIFICATION.bms_timeout_ms:
                findings.append(
                    finding(
                        "BMS_STALE",
                        "BMS 원본 데이터의 갱신이 지연되었습니다. 게이트웨이 생존과 구분합니다.",
                    )
                )
            if not data.bms_valid:
                findings.append(finding("BMS_INVALID", "BMS 값이 유효하지 않습니다."))
            elif data.bms_age_ms <= FIXTURE_SPECIFICATION.bms_timeout_ms:
                findings.append(
                    finding("BMS_FRESH", "BMS 값은 유효하며 최근에 갱신되었습니다.", "info")
                )

    for (tool, target), values in grouped.items():
        if len(values) != 1 or tool in {"can.bus_state", "gateway.snapshot"}:
            continue
        item = values[0]
        error_code = item.error.code if isinstance(item.error, ToolError) else None
        if tool.startswith("swd."):
            if item.status != "ok":
                findings.append(
                    finding(
                        "DEBUG_ACCESS_UNAVAILABLE",
                        "SWD 추가 증거를 수집하지 못했습니다. "
                        "보호 설정이나 고장 원인을 확정하지 않고 분석을 계속합니다.",
                    )
                )
            continue
        if tool == "analyze.symbols":
            # Symbol evidence must belong to the exact captured record, not today's image.
            record = get("can.read_crash_record", target)
            if (
                record is None
                or not record.record_id
                or (item.record_id, item.record_build_id)
                != (record.record_id, record.record_build_id)
            ):
                findings.append(
                    finding(
                        "SYMBOL_EVIDENCE_MISMATCH",
                        "심벌 분석 증거가 해당 기록과 일치하지 않아 소스 위치는 미확인입니다.",
                    )
                )
            elif error_code == "symbol_build_mismatch":
                findings.append(
                    finding(
                        "SYMBOL_BUILD_MISMATCH",
                        "기록 Build ID와 심벌 빌드가 달라 소스 위치를 확정하지 않습니다.",
                    )
                )
            else:
                findings.append(
                    finding(
                        "SYMBOLS_MISSING",
                        "해당 기록의 심벌 해석 결과가 없어 소스 위치는 미확인입니다.",
                    )
                )
            continue
        if item.status != "ok" or item.validity == "invalid":
            suffix = "INVALID" if item.validity == "invalid" else item.status.upper()
            findings.append(
                finding(
                    tool.split(".")[-1].removeprefix("read_").upper() + "_" + suffix,
                    {
                        "UNSUPPORTED": "요청한 진단 기능이 미지원입니다. 기록 없음과 구분합니다.",
                        "TIMEOUT": "진단 조회 시간이 초과됐습니다. "
                        "원격 ECU 크래시 여부는 미확인입니다.",
                        "INVALID": "진단 기록의 형식 또는 무결성이 유효하지 않습니다.",
                    }.get(
                        suffix, "진단 증거를 수집하지 못했습니다. 정상 기록으로 간주하지 않습니다."
                    ),
                )
            )
            continue
        if tool == "can.read_boot_record":
            data = decode(item, BootData)
            if data is None or not item.boot_id or data.current_boot_id != item.boot_id:
                findings.append(
                    finding(
                        "BOOT_RECORD_INVALID",
                        "부팅 식별값이 없거나 일치하지 않아 부팅 변화를 판정할 수 없습니다.",
                    )
                )
            elif data.previous_boot_id != data.current_boot_id:
                findings.append(
                    finding(
                        "ECU_BOOT_CHANGED",
                        "관측된 부팅 식별값이 변경됐습니다. "
                        "재부팅 원인이나 특정 함수 고장은 미확인입니다.",
                    )
                )
        elif tool == "can.read_crash_record":
            data = decode(item, CrashData)
            if item.validity != "valid":
                findings.append(
                    finding(
                        "CRASH_RECORD_UNVERIFIED",
                        "기록 유효성이 확인되지 않아 저장 크래시나 기록 부재를 확정하지 않습니다.",
                    )
                )
            elif data is None:
                findings.append(
                    finding("CRASH_RECORD_INVALID", "크래시 기록 형식이 유효하지 않습니다.")
                )
            elif not data.record_present:
                findings.append(
                    finding(
                        "CRASH_RECORD_ABSENT",
                        "조회는 성공했지만 저장 기록이 없습니다. "
                        "과거 고장 부재를 보장하지 않습니다.",
                        "info",
                    )
                )
            elif (
                not all((item.record_id, item.record_build_id, item.capture_boot_id))
                or item.capture_uptime_ms is None
            ):
                findings.append(
                    finding(
                        "CRASH_RECORD_INVALID",
                        "기록 식별값·당시 빌드·부팅 문맥이 부족해 "
                        "크래시 기록을 확정할 수 없습니다.",
                    )
                )
            else:
                findings.append(
                    finding(
                        "CRASH_RECORD_PRESENT",
                        "저장된 크래시 기록이 있습니다. 현재 ECU 고장 여부와 구분합니다.",
                        "error",
                    )
                )
                if (
                    data.frame_valid is not True
                    or item.field_validity.get("stacked_pc") is not True
                    or data.stacked_pc is None
                ):
                    findings.append(
                        finding(
                            "CRASH_FRAME_INVALID",
                            "예외 프레임 또는 주소의 유효성이 부족해 "
                            "원인 명령을 특정할 수 없습니다.",
                        )
                    )
                if get("analyze.symbols", target) is None:
                    findings.append(
                        finding(
                            "CRASH_SOURCE_LOCATION_UNKNOWN",
                            "해당 기록 Build ID의 심벌 분석이 없어 함수·소스 줄은 미확인입니다.",
                        )
                    )
    return findings
