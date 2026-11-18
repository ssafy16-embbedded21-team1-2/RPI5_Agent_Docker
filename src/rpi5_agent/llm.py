import json
import re

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from openai import ContentFilterFinishReasonError, LengthFinishReasonError
from pydantic import ValidationError

from rpi5_agent.config import Settings
from rpi5_agent.errors import SummaryError
from rpi5_agent.evidence_summary import build_model_payload
from rpi5_agent.models import (
    Finding,
    LLMAttempt,
    LLMMetadata,
    LLMRouting,
    LLMTarget,
    ModelExplanation,
    SummaryResult,
    ToolResult,
)

HEALTHY_TEMPLATE_EXPLANATION = ModelExplanation(
    hypothesis="모든 진단 항목이 정상 범위 내에 있습니다.",
    next_check="추가 조치 없이 주기적 상태 감시를 유지하세요.",
)

_FORBIDDEN_CONTROL_TERMS = (
    "보호 해제",
    "보호해제",
    "잠금 해제",
    "잠금해제",
    "플래싱",
    "재플래싱",
    "st-flash",
    "swd.unlock",
    "swd.halt",
    "swd.reset",
)
_FAULT_TERMS = (
    "고장",
    "불량",
    "오류 발생",
    "손상",
    "불완전",
    "단선",
    "bus-off",
    "타임아웃",
)
_NEGATION_TERMS = ("없습니다", "아닙니다", "정상")
_UNVERIFIED_SYMBOL_CODES = frozenset(
    {"SYMBOLS_MISSING", "SYMBOL_BUILD_MISMATCH", "CRASH_SOURCE_LOCATION_UNKNOWN"}
)
_SOURCE_LINE_RE = re.compile(r"(?:^|[^A-Za-z0-9_])[A-Za-z0-9_]+\.(?:c|h|cpp|hpp):\d+(?!\d)")


def _summarize_validation_error(exc: ValidationError) -> str:
    errors = exc.errors(include_url=False)
    if not errors:
        return "field=root,type=validation_error"
    err = errors[0]
    loc = ".".join(str(part) for part in err.get("loc", ())) or "root"
    err_type = str(err.get("type", "validation_error"))
    inp = err.get("input")
    if isinstance(inp, str):
        return f"field={loc},type={err_type},len={len(inp)}"
    return f"field={loc},type={err_type}"


def validate_explanation_semantics(
    explanation: ModelExplanation,
    evidence: list[ToolResult],
    findings: list[Finding],
) -> str | None:
    """Reject semantically contradicting or forbidden explanations deterministically."""
    _ = evidence
    hypothesis_lower = explanation.hypothesis.lower()
    next_check_lower = explanation.next_check.lower()
    if any(
        term in hypothesis_lower or term in next_check_lower for term in _FORBIDDEN_CONTROL_TERMS
    ):
        return "forbidden_control_action"

    finding_codes = {item.code for item in findings}
    if findings and all(item.severity == "info" for item in findings):
        if any(term in hypothesis_lower for term in _FAULT_TERMS) and not any(
            neg in hypothesis_lower for neg in _NEGATION_TERMS
        ):
            return "healthy_contradiction"

    if "CRASH_RECORD_PRESENT" in finding_codes and not any(
        item.severity == "error" and item.code != "CRASH_RECORD_PRESENT" for item in findings
    ):
        if "현재 ecu 고장" in hypothesis_lower or "현재 고장" in hypothesis_lower:
            return "historical_crash_as_current"

    if finding_codes & _UNVERIFIED_SYMBOL_CODES:
        if _SOURCE_LINE_RE.search(explanation.hypothesis) or _SOURCE_LINE_RE.search(
            explanation.next_check
        ):
            return "unverified_symbol_claim"

    return None


class ChatSummarizer:
    """One configured backend, with the same strict output validation for both providers."""

    def __init__(self, settings: Settings, *, external: bool = False):
        self.model_id = settings.external_llm_model if external else settings.llm_model
        self.response_format = settings.external_llm_response_format if external else "json_schema"
        self.model = ChatOpenAI(
            model=self.model_id,
            base_url=settings.external_llm_base_url if external else settings.llm_base_url,
            api_key=settings.external_llm_api_key if external else settings.llm_api_key,
            default_headers=(
                {"User-Agent": settings.external_llm_user_agent} if external else None
            ),
            temperature=0.2,
            max_tokens=(settings.external_llm_max_tokens if external else settings.llm_max_tokens),
            timeout=(
                settings.external_llm_timeout_seconds if external else settings.llm_timeout_seconds
            ),
            max_retries=0,
            use_responses_api=False,
        )

    def summarize(self, evidence: list[ToolResult], findings: list[Finding]) -> SummaryResult:
        # Keep the request small for the existing 2048-token llama-server configuration.
        payload = build_model_payload(evidence, findings)
        intro = (
            "실측 CAN·UART 데이터의 findings에 대한 진단 보조 설명을 작성하세요. "
            if payload.get("mode") == "live"
            else "가상 데이터의 findings에 대한 진단 보조 설명을 작성하세요. "
        )
        messages = [
            SystemMessage(
                content=(
                    intro + "hypothesis(원인 후보), next_check(추가 확인) "
                    "두 필드만 가진 JSON을 출력하세요. "
                    "각 값은 한국어 한 문장, 50자 이내의 단일 행으로 작성하세요. "
                    "입력된 findings의 warning·error 항목과 evidence 수치만 근거로 삼아 "
                    "통신 주기, 물리 배선·종단저항, 전원, 어댑터 상태, "
                    "빌드·심벌 일치 여부를 점검 대상으로 제시하세요. "
                    "원인은 단정하지 말고 가능성으로 서술하며, 근거가 부족하면 "
                    "'현재 증거만으로 원인을 특정할 수 없습니다.'라고 쓰세요. "
                    "capture_boot_id나 record_build_id가 현재와 다르면 "
                    "과거 부팅/빌드 사건임을 구분하세요."
                )
            ),
            HumanMessage(content=json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
        ]
        # Pamout documents chat completions, but does not promise JSON Schema support.
        # Prompt-only is a configured provider mode, never a silent retry after schema failure.
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "diagnostic_explanation",
                "strict": True,
                "schema": ModelExplanation.model_json_schema(),
            },
        }
        invoke_options = {}
        if self.response_format == "json_schema":
            invoke_options["response_format"] = response_format
        elif self.response_format == "json_object":
            invoke_options["response_format"] = {"type": "json_object"}
        try:
            response = self.model.invoke(messages, **invoke_options)
        except (LengthFinishReasonError, ContentFilterFinishReasonError) as exc:
            # The SDK may reject these before LangChain returns an AIMessage.
            completion_usage = exc.completion.usage if exc.completion is not None else None
            is_length = isinstance(exc, LengthFinishReasonError)
            metadata = LLMMetadata(
                finish_reason="length" if is_length else "content_filter",
                input_tokens=completion_usage.prompt_tokens if completion_usage else None,
                output_tokens=completion_usage.completion_tokens if completion_usage else None,
            )
            raise SummaryError(
                "output_truncated" if is_length else "unexpected_finish_reason", metadata
            ) from exc
        usage = response.usage_metadata or {}
        metadata = LLMMetadata(
            finish_reason=response.response_metadata.get("finish_reason"),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
        )
        # Even valid JSON must not be treated as complete after a token-limit stop.
        if metadata.finish_reason == "length":
            raise SummaryError("output_truncated", metadata)
        if metadata.finish_reason != "stop":
            raise SummaryError("unexpected_finish_reason", metadata)
        if not isinstance(response.content, str) or not response.content.strip():
            raise SummaryError("invalid_model_output", metadata, error_detail="empty_content")
        try:
            explanation = ModelExplanation.model_validate_json(response.content)
        except ValidationError as exc:
            raise SummaryError(
                "invalid_model_output",
                metadata,
                error_detail=_summarize_validation_error(exc),
            ) from exc
        violation = validate_explanation_semantics(explanation, evidence, findings)
        if violation is not None:
            raise SummaryError(
                "invalid_model_output",
                metadata,
                error_detail=f"semantic_violation:{violation}",
            )
        return SummaryResult(explanation=explanation, metadata=metadata)


class RoutedSummarizer:
    """Deterministic request routing, with at most one call per selected backend."""

    def __init__(self, settings: Settings):
        self.local = ChatSummarizer(settings)
        self.external = (
            ChatSummarizer(settings, external=True) if settings.external_llm_enabled else None
        )
        self.fallback_on_failure = settings.external_llm_fallback_on_local_failure
        self.prefer_external_for_warnings = settings.auto_prefer_external_for_warnings

    def summarize(
        self,
        evidence: list[ToolResult],
        findings: list[Finding],
        *,
        target: LLMTarget = LLMTarget.AUTO,
    ) -> SummaryResult:
        if (
            target == LLMTarget.AUTO
            and findings
            and all(item.severity == "info" for item in findings)
        ):
            return SummaryResult(
                explanation=HEALTHY_TEMPLATE_EXPLANATION,
                metadata=LLMMetadata(finish_reason="stop", input_tokens=0, output_tokens=0),
                routing=LLMRouting(
                    requested=target,
                    backend="template",
                    model="deterministic-template",
                    reason="deterministic_template",
                    attempts=[],
                ),
            )

        if target == LLMTarget.EXTERNAL:
            if self.external is None:
                raise SummaryError("external_llm_disabled")
            candidates = [("pamout", self.external)]
            reason = "explicit_external"
        elif target == LLMTarget.LOCAL:
            candidates = [("local", self.local)]
            reason = "explicit_local"
        elif self.external is not None and any(item.severity == "error" for item in findings):
            candidates = [("pamout", self.external)]
            reason = "error_findings"
        elif (
            self.external is not None
            and self.prefer_external_for_warnings
            and any(item.severity == "warning" for item in findings)
        ):
            candidates = [("pamout", self.external)]
            reason = "warning_findings"
        else:
            candidates = [("local", self.local)]
            reason = "local_default"
            if self.external is not None and self.fallback_on_failure:
                candidates.append(("pamout", self.external))

        route = LLMRouting(
            requested=target,
            backend=candidates[0][0],
            model=candidates[0][1].model_id,
            reason=reason,
        )
        for index, (backend, model) in enumerate(candidates):
            route.backend = backend
            route.model = model.model_id
            if index:
                route.reason = "local_failure"
            try:
                result = model.summarize(evidence, findings)
            except Exception as exc:
                code = exc.code if isinstance(exc, SummaryError) else type(exc).__name__
                metadata = exc.metadata if isinstance(exc, SummaryError) else None
                error_detail = exc.error_detail if isinstance(exc, SummaryError) else None
                route.attempts.append(
                    LLMAttempt(
                        backend=backend,
                        model=model.model_id,
                        status="unavailable",
                        error=code,
                        error_detail=error_detail,
                        metadata=metadata,
                    )
                )
                if index == len(candidates) - 1:
                    raise SummaryError(code, metadata, route, error_detail=error_detail) from exc
            else:
                route.attempts.append(
                    LLMAttempt(
                        backend=backend,
                        model=model.model_id,
                        status="generated",
                        metadata=result.metadata,
                    )
                )
                result.routing = route
                return result
        raise RuntimeError("No LLM backend was selected")
