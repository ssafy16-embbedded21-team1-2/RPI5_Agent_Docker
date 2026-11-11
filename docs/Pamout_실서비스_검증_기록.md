# Pamout 외부 LLM 실서비스 검증 기록

검증일: 2026-11-11, 약 15:27~15:29 KST.

## 1. 범위와 환경

- 실행 환경: Windows PC의 Python 3.12 / `uv` 프로젝트 환경, FastAPI `TestClient`.
- 외부 경로: 실제 `https://llm.pamout.com/v1`, 요청 모델 `glm-5.3`.
- 진단 입력: 기존 fixture만 사용. 실제 CAN·STM32 데이터가 아님.
- 인증: 로컬 `.env`의 개인 키를 읽어 Bearer 인증에 사용. User-Agent도 전송.
- 원본 설정: `LLM_ENABLED=false`, `EXTERNAL_LLM_ENABLED=true`, 외부 출력 모드 `prompt`, 최대 출력 토큰 4096.
- 전체 LLM은 테스트 프로세스의 설정 복사본에서만 활성화했다. `.env` 파일 해시를 전후 비교해 변경 없음을 확인했다.
- 외부 요청 수: 모델 목록 GET 1건, Chat Completions POST 3건. 실서비스 자동 재시도 없이 수행했다.

키·요청 인증 헤더·서버 오류 본문은 검증 기록에 저장하지 않았다. 실응답을 사용하는 검증이며 모의 HTTP 응답을 성공 결과로 기록하지 않았다.

## 2. 설정·모델 목록 확인

| 확인 항목 | 결과 |
| --- | --- |
| 인증된 `GET /v1/models` | HTTP 200 |
| 설정한 `glm-5.3`의 목록 포함 여부 | 포함 |
| 기존 `.env` 그대로 `use_llm=true` 요청 | HTTP 409, 전체 LLM 스위치가 꺼져 있기 때문 |
| 테스트 프로세스에서 전체 LLM 활성화 | 실제 외부 진단 요청 가능 |

모델 ID 목록 조회는 해당 키의 현재 접근 목록 확인이며, 모델의 내부 가중치나 성능을 검증한 것은 아니다.

## 3. 실제 진단 요청 결과

| 시험 | 요청·조건 | 선택 이유 | 경과시간 | 입력/출력 토큰 | 결과 |
| --- | --- | --- | --- | --- | --- |
| 외부 직접 지정 | `bms_timeout`, `llm_target=external` | `explicit_external` | 13.19초 | 388 / 1192 | HTTP 200, `generated`, `stop` |
| 오류 수준 자동 선택 | `bus_off`, `llm_target=auto` | `error_findings` | 14.38초 | 356 / 1178 | HTTP 200, `generated`, `stop` |
| 로컬 실패 후 외부 전환 | `bms_timeout`, `llm_target=auto`, 테스트용 로컬 주소를 접속 불가 loopback으로 지정 | `local_failure` | 19.34초 | 388 / 1187 | 로컬 `OpenAITimeoutError` 뒤 Pamout 성공 |

경과시간은 PC에서 진단 요청을 실행한 단일 관측값이다. 마지막 시험에는 의도한 로컬 실패 대기와 외부 호출이 모두 포함된다. 토큰 수는 서버가 보고한 값이며 화면에 표시되는 두 문장의 문자 수와 다르다. 반복 성능·품질·부하 시험 결과나 RPi 지연 보장으로 사용하지 않는다.

세 응답 모두 `hypothesis`, `next_check`만 포함하는 JSON을 기존 Pydantic 검증기로 확인했다. 각 값은 1~50자의 단일 행 문자열이며 종료 사유는 `stop`이었다. 형식 통과가 원인 확정이나 진단 정확성 검증을 의미하지 않는다.

직접 외부 지정 응답 예시:

```json
{
  "hypothesis": "BMS와 게이트웨이 간 통신 지연 또는 BMS 내부 처리 지연일 가능성이 있습니다.",
  "next_check": "BMS에서 게이트웨이로 전송되는 CAN 메시지 주기와 에러를 확인하세요."
}
```

오류 수준 자동 선택에서는 `LOCAL_ADAPTER_BUS_OFF`, `REMOTE_STATE_UNKNOWN` 판정이 유지됐다. 응답은 현재 증거만으로 원인을 특정할 수 없다는 설명과 로컬 어댑터 배선·커넥터 점검을 제시했다. 로컬 실패 전환에서는 호출 이력에 로컬 실패와 Pamout 성공이 순서대로 남았고 `GATEWAY_FRESH`, `BMS_STALE` 판정이 유지됐다.

## 4. 남은 범위와 사용 시 설정

- 정상 사용을 시작하려면 `.env`의 `LLM_ENABLED=true`도 필요하다. 이번 검증은 파일을 수정하지 않았으므로 원본 값은 `false`다.
- 이번 실서비스 시험은 `prompt` 모드만 확인했다. Pamout의 `json_object`·`json_schema` 지원과 강제 정도는 검증하지 않았다.
- 로컬 실패는 PC 테스트 설정으로 주입했다. RPi Qwen 서버를 정지하거나 RPi에 코드를 배포하지 않았다.
- RPi Docker에서의 외부 DNS/TLS·키 전달·실제 지연은 별도 배포 검증이 필요하다.
- 외부 429/500·비정상 종료·잘린 출력·잘못된 JSON은 기존 모의 서버 테스트 범위다. 실제 서비스를 고의로 과부하시키거나 인증 실패를 반복하지 않았다.

기계 판독용 결과는 로컬 `data/pamout-live-verification-20261111.json`에 보관했다. `data/`는 Git 추적에서 제외되므로 공유용 근거는 이 문서에 요약했다. 결과 파일에도 키는 포함하지 않았다.
