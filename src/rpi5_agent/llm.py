import json

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from openai import ContentFilterFinishReasonError, LengthFinishReasonError
from pydantic import ValidationError

from rpi5_agent.config import Settings
from rpi5_agent.errors import SummaryError
from rpi5_agent.models import Finding, LLMMetadata, ModelExplanation, SummaryResult, ToolResult


class LocalSummarizer:
    def __init__(self, settings: Settings):
        self.model = ChatOpenAI(
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            temperature=0.2,
            max_tokens=settings.llm_max_tokens,
            timeout=settings.llm_timeout_seconds,
            max_retries=0,
            use_responses_api=False,
        )

    def summarize(self, evidence: list[ToolResult], findings: list[Finding]) -> SummaryResult:
        # Keep the request small for the existing 2048-token llama-server configuration.
        payload = {
            "mode": "fixture",
            "evidence": [
                item.model_dump(mode="json", include={"tool", "target", "status", "data"})
                for item in evidence
            ],
            "findings": [item.model_dump(include={"code", "message"}) for item in findings],
        }
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
                )
            ),
            HumanMessage(content=json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
        ]
        # Request schema-constrained JSON directly, without a tool-calling round trip.
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "diagnostic_explanation",
                "strict": True,
                "schema": ModelExplanation.model_json_schema(),
            },
        }
        try:
            response = self.model.invoke(messages, response_format=response_format)
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
