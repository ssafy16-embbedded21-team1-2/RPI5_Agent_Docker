import logging
from typing import Protocol, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from rpi5_agent.assessment import evaluate as evaluate
from rpi5_agent.errors import SummaryError
from rpi5_agent.models import (
    DiagnoseRequest,
    Diagnosis,
    DiagnosticProfile,
    Finding,
    LLMTarget,
    SummaryResult,
    ToolError,
    ToolResult,
)
from rpi5_agent.tools.collection import EvidenceProvider, collect_evidence
from rpi5_agent.tools.fixtures import FixtureProvider

logger = logging.getLogger(__name__)


class Summarizer(Protocol):
    def summarize(
        self,
        evidence: list[ToolResult],
        findings: list[Finding],
        *,
        target: LLMTarget = LLMTarget.AUTO,
    ) -> SummaryResult: ...


class DiagnosticState(TypedDict, total=False):
    request: DiagnoseRequest
    evidence: list[ToolResult]
    findings: list[Finding]
    result: Diagnosis


def build_workflow(
    summarizer: Summarizer | None = None,
    *,
    provider: EvidenceProvider | None = None,
    profile: DiagnosticProfile = DiagnosticProfile.PRODUCT,
):
    collector = provider if provider is not None else FixtureProvider()

    def collect(state: DiagnosticState) -> dict:
        try:
            evidence = collect_evidence(collector, state["request"].scenario, profile)
        except Exception:
            evidence = [
                ToolResult(
                    tool="evidence.collect",
                    target="diagnosis",
                    status="error",
                    error=ToolError(code="collection_plan_failed"),
                )
            ]
        return {"evidence": evidence}

    def assess(state: DiagnosticState) -> dict:
        return {"findings": evaluate(state["evidence"])}

    def report(state: DiagnosticState) -> dict:
        result = Diagnosis(
            request_id=str(uuid4()),
            diagnostic_profile=profile,
            scenario=state["request"].scenario,
            evidence=state["evidence"],
            findings=state["findings"],
            summary="[가상 데이터]\n관측: " + " ".join(item.message for item in state["findings"]),
        )
        if state["request"].use_llm:
            if summarizer is None:
                raise RuntimeError("LLM was requested but no summarizer is configured")
            try:
                model_result = summarizer.summarize(
                    state["evidence"], state["findings"], target=state["request"].llm_target
                )
                result.explanation = model_result.explanation
                result.llm_metadata = model_result.metadata
                result.llm_routing = model_result.routing
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
                    result.llm_routing = exc.routing
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
