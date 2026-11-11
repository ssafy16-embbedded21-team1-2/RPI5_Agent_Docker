import json

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
        messages = [
            SystemMessage(
                content=(
                    "가상 데이터의 findings에 대한 진단 보조 설명을 작성하세요. "
                    "hypothesis(원인 후보), next_check(추가 확인) "
                    "두 필드만 가진 JSON을 출력하세요. "
                    "각 값은 한국어 한 문장, 50자 이내입니다. 목록과 줄바꿈은 쓰지 마세요. "
                    "관측 사실을 반복하지 말고 원인 후보 하나와 확인 방법 하나만 쓰세요. "
                    "원인은 가능성으로 표현하고 근거가 부족하면 "
                    "'현재 증거만으로 원인을 특정할 수 없습니다.'라고 쓰세요. "
                    "정상 판정이면 고장을 만들지 마세요. 미관측 수치와 DTC를 추가하지 마세요. "
                    "로컬 Bus-Off를 원격 ECU 상태로 해석하거나 펌웨어 손상을 단정하지 마세요."
                    "저장 기록은 과거 사건입니다. 현재 빌드와 기록 빌드, 부팅을 구분하세요. "
                    "미지원·조회 실패·기록 없음은 서로 다르며 정상의 근거가 아닙니다. "
                    "심벌·예외 프레임이 불명확하면 함수나 소스 줄을 추측하지 마세요. "
                    "SWD 접근 불가를 보안 잠금으로 단정하지 마세요. "
                    "보호 해제·정지·리셋·플래싱을 제안하지 마세요."
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
            raise SummaryError("invalid_model_output", metadata)
        try:
            explanation = ModelExplanation.model_validate_json(response.content)
        except ValidationError as exc:
            raise SummaryError("invalid_model_output", metadata) from exc
        return SummaryResult(explanation=explanation, metadata=metadata)


class RoutedSummarizer:
    """Deterministic request routing, with at most one call per selected backend."""

    def __init__(self, settings: Settings):
        self.local = ChatSummarizer(settings)
        self.external = (
            ChatSummarizer(settings, external=True) if settings.external_llm_enabled else None
        )
        self.fallback_on_failure = settings.external_llm_fallback_on_local_failure

    def summarize(
        self,
        evidence: list[ToolResult],
        findings: list[Finding],
        *,
        target: LLMTarget = LLMTarget.AUTO,
    ) -> SummaryResult:
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
                route.attempts.append(
                    LLMAttempt(
                        backend=backend,
                        model=model.model_id,
                        status="unavailable",
                        error=code,
                        metadata=metadata,
                    )
                )
                if index == len(candidates) - 1:
                    raise SummaryError(code, metadata, route) from exc
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
