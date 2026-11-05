import logging
from typing import Protocol, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from rpi5_agent.errors import SummaryError
from rpi5_agent.models import DiagnoseRequest, Diagnosis, Finding, SummaryResult, ToolResult
from rpi5_agent.tools.fixtures import collect_fixture

logger = logging.getLogger(__name__)


class Summarizer(Protocol):
    def summarize(self, evidence: list[ToolResult], findings: list[Finding]) -> SummaryResult: ...


class DiagnosticState(TypedDict, total=False):
    request: DiagnoseRequest
    evidence: list[ToolResult]
    findings: list[Finding]
    result: Diagnosis


def evaluate(evidence: list[ToolResult]) -> list[Finding]:
    bus, gateway = evidence
    if bus.data["state"] == "bus_off":
        return [
            Finding(
                code="LOCAL_ADAPTER_BUS_OFF",
                severity="error",
                message="RPi 로컬 CAN 어댑터가 Bus-Off 상태입니다.",
            ),
            Finding(
                code="REMOTE_STATE_UNKNOWN",
                severity="warning",
                message="게이트웨이와 BMS의 현재 상태는 수집된 증거로 판단할 수 없습니다.",
            ),
        ]
    if gateway.status != "ok":
        return [
            Finding(
                code="GATEWAY_EVIDENCE_UNAVAILABLE",
                severity="warning",
                message="게이트웨이 증거를 수집하지 못했습니다.",
            )
        ]
    data = gateway.data
    findings = []
    if data["gateway_age_ms"] > data["gateway_timeout_ms"]:
        return [
            Finding(
                code="GATEWAY_STALE",
                severity="warning",
                message="게이트웨이 집계 데이터가 오래되어 현재 BMS 상태를 확정할 수 없습니다.",
            )
        ]
    if data["gateway_age_ms"] <= data["gateway_timeout_ms"]:
        findings.append(
            Finding(
                code="GATEWAY_FRESH",
                severity="info",
                message="게이트웨이 집계 데이터는 최근에 갱신되었습니다.",
            )
        )
    if data["bms_age_ms"] > data["bms_timeout_ms"]:
        findings.append(
            Finding(
                code="BMS_STALE",
                severity="warning",
                message="BMS 원본 데이터의 갱신이 지연되었습니다. 게이트웨이 생존과 구분합니다.",
            )
        )
    if not data["bms_valid"]:
        findings.append(
            Finding(
                code="BMS_INVALID",
                severity="warning",
                message="BMS 값이 유효하지 않습니다.",
            )
        )
    if data["bms_valid"] and data["bms_age_ms"] <= data["bms_timeout_ms"]:
        findings.append(
            Finding(
                code="BMS_FRESH",
                severity="info",
                message="BMS 값은 유효하며 최근에 갱신되었습니다.",
            )
        )
    return findings


def build_workflow(summarizer: Summarizer | None = None):
    def collect(state: DiagnosticState) -> dict:
        return {"evidence": collect_fixture(state["request"].scenario)}

    def assess(state: DiagnosticState) -> dict:
        return {"findings": evaluate(state["evidence"])}

    def report(state: DiagnosticState) -> dict:
        result = Diagnosis(
            request_id=str(uuid4()),
            scenario=state["request"].scenario,
            evidence=state["evidence"],
            findings=state["findings"],
            summary="[가상 데이터]\n관측: " + " ".join(item.message for item in state["findings"]),
        )
        if state["request"].use_llm:
            if summarizer is None:
                raise RuntimeError("LLM was requested but no summarizer is configured")
            try:
                model_result = summarizer.summarize(state["evidence"], state["findings"])
                result.explanation = model_result.explanation
                result.llm_metadata = model_result.metadata
                result.summary += (
                    f"\n원인 후보: {model_result.explanation.hypothesis}"
                    f"\n추가 확인: {model_result.explanation.next_check}"
                )
                result.llm_status = "generated"
            except Exception as exc:
                # Keep deterministic findings available; do not report model failure as success.
                result.llm_status = "unavailable"
                if isinstance(exc, SummaryError):
                    result.llm_error = exc.code
                    result.llm_metadata = exc.metadata
                else:
                    result.llm_error = type(exc).__name__
                logger.warning("LLM summary unavailable: %s", result.llm_error)
        return {"result": result}

    graph = StateGraph(DiagnosticState)
    graph.add_node("collect", collect)
    graph.add_node("assess", assess)
    graph.add_node("report", report)
    graph.add_edge(START, "collect")
    graph.add_edge("collect", "assess")
    graph.add_edge("assess", "report")
    graph.add_edge("report", END)
    return graph.compile()
