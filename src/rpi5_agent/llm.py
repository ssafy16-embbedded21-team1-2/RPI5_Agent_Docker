import json

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from rpi5_agent.config import Settings
from rpi5_agent.models import Finding, ToolResult


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

    def summarize(self, evidence: list[ToolResult], findings: list[Finding]) -> str:
        # Keep the request small for the existing 2048-token llama-server configuration.
        payload = {
            "mode": "fixture",
            "evidence": [item.model_dump(mode="json") for item in evidence],
            "findings": [item.model_dump() for item in findings],
        }
        response = self.model.invoke(
            [
                SystemMessage(
                    content=(
                        "임베디드 진단 결과를 한국어로 짧게 설명하세요. "
                        "입력은 가상 실습 데이터입니다. "
                        "실제 장치를 측정했다고 표현하지 마세요. 코드가 판정한 findings를 근거로 "
                        "관측 사실, 원인 후보, 추가 확인 사항을 구분하세요. "
                        "증거에 없는 DTC나 수치를 "
                        "만들지 마세요. 로컬 어댑터의 Bus-Off를 원격 ECU 상태로 해석하지 마세요. "
                        "통신 두절만으로 펌웨어 손상이나 OTA 필요성을 단정하지 마세요."
                    )
                ),
                HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
            ]
        )
        if not isinstance(response.content, str) or not response.content.strip():
            raise ValueError("The model returned no text summary")
        return response.content.strip()
