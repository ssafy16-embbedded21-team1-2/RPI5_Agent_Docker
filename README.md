# RPi 5 Diagnostic Agent

Raspberry Pi 5에서 실행할 로컬 진단 Agent의 Docker 기반 시작 프로젝트입니다.
LangGraph가 증거 수집 → 규칙 판정 → 결과 작성 순서를 실행하고,
LangChain이 호스트의 `llama-server`에 설명 생성을 요청합니다.

**현재 모든 진단 입력은 가상 데이터입니다.** CAN·UDS·SWD의 실제 수집과 제어는 아직 구현하지 않았습니다.
트랜시버와 모델 없이도 API와 진단 흐름을 실행할 수 있습니다.

PC Docker에서 확인한 결과는 [Docker 검증 기록](docs/Docker_검증_기록.md),
RPi 5와 실제 Qwen 연결 결과는 [RPi 5 검증 기록](docs/RPi5_검증_기록.md)을 참고하세요.

## 1. 실행 구성

```text
RPi 5 호스트 (64-bit Linux)
├── Docker Engine + Compose
│   └── agent: FastAPI + LangGraph + LangChain
├── llama-server: Qwen GGUF 모델 (선택)
└── CAN 설정 / OpenOCD / 하드웨어 서비스 (향후 연결)
```

Agent 컨테이너는 호스트의 모델 서버를 HTTP로 호출합니다.
모델 파일, CUDA/PyTorch, 컴파일러, Docker 데몬을 Agent 이미지 안에 넣지 않습니다.
이미지는 Python 3.12 기반이며 RPi에서 빌드하면 ARM64로, 일반 x64 PC에서 빌드하면 AMD64로 생성됩니다.

## 2. 빠른 시작: Docker

필수 조건은 실행 중인 Docker Engine과 `docker compose`입니다.
RPi에는 64-bit Raspberry Pi OS/Debian과 Docker Engine을 사용합니다.
Docker 설치는 [공식 Debian 설치 안내](https://docs.docker.com/engine/install/debian/)를 따릅니다.
Windows에서는 Docker Desktop의 Linux 엔진을 먼저 실행합니다.

레포 루트에서, `.env`가 없는 최초 실행에만 설정 파일을 복사합니다.

Linux/RPi:

```bash
cp .env.example .env
docker compose config --quiet
docker compose up -d --build
docker compose ps
python3 scripts/smoke_test.py
```

Windows PowerShell:

```powershell
Copy-Item .env.example .env
docker compose config --quiet
docker compose up -d --build
docker compose ps
python scripts/smoke_test.py
```

최초 빌드에는 인터넷이 필요합니다. 이미지를 빌드하고 모델을 준비한 뒤에는 기본 실행에 외부 LLM API가 필요하지 않습니다.
Pi에서 이미지를 빌드하는 동안 모델 추론을 함께 실행하면 CPU·메모리 경쟁이 생길 수 있습니다.

실행 후 확인할 주소:

- `http://127.0.0.1:8000/health`: Agent 프로세스 상태
- `http://127.0.0.1:8000/docs`: API 문서와 요청 실행 화면
- `http://127.0.0.1:8000/scenarios`: 사용할 수 있는 가상 시나리오

`/health`의 성공은 모델 연결이나 실제 CAN·STM32 연결 성공을 의미하지 않습니다.

진단 요청 예제:

```bash
curl -sS http://127.0.0.1:8000/diagnose \
  -H 'Content-Type: application/json' \
  --data-binary @examples/bms_timeout.json
```

PowerShell에서는 같은 명령의 `curl` 대신 `curl.exe`를 사용하거나 다음을 실행합니다.

```powershell
Invoke-RestMethod http://127.0.0.1:8000/diagnose -Method Post `
  -ContentType application/json -Body (Get-Content -Raw examples/bms_timeout.json)
```

## 3. RPi에서 실행하고 PC로 접속

이 디렉터리의 소스와 `uv.lock`을 Pi에 전달하고, Pi의 레포 루트에서 2절 명령을 실행합니다.
Windows의 `.venv`, 빌드 캐시, `.env`는 복사하지 않고 Pi에서 새로 준비합니다.
Git 원격 저장소를 만들면 소스를 push한 뒤 Pi에서 clone하는 방식으로 공유할 수 있습니다.

기본 API는 Pi의 `127.0.0.1:8000`에만 공개됩니다. PC에서 SSH 터널을 연결하면 접속할 수 있습니다.

```bash
ssh -N -L 18000:127.0.0.1:8000 pi@<RPi주소>
```

PC 브라우저에서 `http://127.0.0.1:18000/docs`를 엽니다.
LAN에 직접 공개하려면 `.env`의 `AGENT_BIND`를 변경해야 합니다.
현재 API에는 인증이 없으므로 기본 설정은 로컬 접속과 SSH 터널을 전제로 합니다.

## 4. 호스트 Qwen / llama-server 연결

기존 STM32 설정 레포의 `scripts/run_qwen3b_server.sh`를 그대로 호스트에서 실행할 수 있습니다.
이 서버 스크립트는 `8080` 포트와 `0.0.0.0` 바인딩을 사용합니다.
호스트에서 실행한 서버가 Docker bridge 네트워크에서도 접근 가능한 주소에 바인딩되어 있어야 합니다.
`127.0.0.1`에만 바인딩된 호스트 서버에는 이 bridge 구성이 접속하지 못합니다.

먼저 **호스트에서** 모델 ID를 확인합니다.

```bash
curl -sS http://127.0.0.1:8080/v1/models
```

응답 `data[].id`를 `.env`의 `LLM_MODEL`로 설정합니다.
서버를 `--alias local-qwen`으로 실행한다면 예제의 이름을 그대로 사용할 수 있습니다.

```dotenv
LLM_ENABLED=true
LLM_BASE_URL=http://host.docker.internal:8080/v1
LLM_MODEL=local-qwen
LLM_API_KEY=not-required
```

`not-required`는 인증하지 않는 로컬 서버용 자리표시자입니다.
서버에 API 키를 설정했다면 실제 키를 `.env`에 입력합니다. `.env`는 Git에 포함하지 않습니다.
환경변수 변경 후에는 컨테이너를 재생성합니다.

```bash
docker compose up -d --force-recreate
curl -sS http://127.0.0.1:8000/diagnose \
  -H 'Content-Type: application/json' \
  --data-binary @examples/bms_timeout_llm.json
```

Compose의 `extra_hosts`가 `host.docker.internal`을 Docker 호스트로 연결합니다.
컨테이너 내부의 `localhost`는 컨테이너 자신이므로 호스트 모델 주소로 쓰지 않습니다.
방화벽이 있는 환경에서는 Docker 네트워크에서 호스트 모델 포트로 접근할 수 있는지도 확인합니다.

이 단계는 **모델의 설명 생성만** 사용하며 native tool calling에 의존하지 않습니다.
모델은 도구 선택이나 하드웨어 명령을 실행하지 않습니다.
모델 서버가 Chat Completions의 `response_format` / JSON Schema 기반 출력을 지원해야 합니다.
미지원 서버의 오류는 `llm_status=unavailable`로 반환하며 자유 형식으로 재요청하지 않습니다.
기존 2048 토큰 컨텍스트를 고려해 짧은 증거와 최대 256 토큰의 출력을 기본으로 사용합니다.
설정을 늘리기 전 실제 모델의 토큰 사용량과 메모리를 측정합니다.

## 5. 구현된 범위

| 항목 | 현재 동작 |
| --- | --- |
| 진단 흐름 | LangGraph로 가상 증거 수집 → 규칙 판정 → 설명 작성 |
| `healthy` | 게이트웨이와 BMS 데이터가 모두 최신인 예제 |
| `bms_timeout` | 게이트웨이는 최신이고 BMS 원본만 오래된 예제 |
| `bus_off` | RPi 로컬 어댑터 Bus-Off와 원격 상태 미확인을 구분 |
| LLM 설명 | JSON Schema로 원인 후보와 추가 확인을 받아 Pydantic으로 검증 |
| 모델 오류 | 연결 실패·출력 잘림·형식 오류 시 코드 기반 요약 유지 |
| 실제 CAN·UDS·SWD | 미구현. 현재 가상 데이터만 사용 |
| 승인·제어·복구 | 미구현. 제어 API와 임의 명령 실행 경로 없음 |
| 이력·RAG·대화 | 미구현. 요청별 처리이며 결과는 응답으로 반환 |

공통 증거 응답은 `target`, `timestamp`, `status`, `data`, `error`를 포함합니다.
`source=fixture`와 결과의 `mode=fixture`로 실제 측정과 구분합니다.
오류 카운터 미확인은 `null`이며 0으로 대체하지 않습니다.
예제의 타임아웃 500/1000ms는 테스트용 값으로 실제 통신 규약의 확정값이 아닙니다.

모델 사용이 꺼져 있는데 `use_llm=true`로 요청하면 HTTP 409를 반환합니다.
동시에 진단을 여러 번 요청하면 진행 중인 요청 외에는 HTTP 429를 반환합니다.
모델 호출은 자동 재시도하지 않으며 기본 제한 시간은 120초입니다.
모델 실패는 HTTP 200 응답 안의 `llm_status=unavailable`로 구분하므로 호출자가 이 필드를 확인해야 합니다.

### 설명 출력과 종료 사유

관측 문장은 코드의 `findings`에서 조립합니다. 모델은 `hypothesis`와 `next_check` 두 필드만 작성하며,
각 필드는 공백 정리 후 1~50자의 단일 행 문자열이어야 합니다. 추가 필드·빈 값·잘못된 타입은 거절합니다.
한국어 한 문장씩 작성하도록 프롬프트에서 지시합니다.
정상 종료(`finish_reason=stop`)와 형식 검증을 모두 통과한 경우에만 `generated`로 처리합니다.
이는 진단 정확성이나 원인 확정을 의미하지 않습니다.

표시 형식 예시:

```text
[가상 데이터]
관측: 게이트웨이 집계 데이터는 최근에 갱신되었습니다. BMS 원본 데이터의 갱신이 지연되었습니다. 게이트웨이 생존과 구분합니다.
원인 후보: BMS 송신 또는 중계 경로에 문제가 있을 수 있습니다.
추가 확인: BMS 송신 기록과 게이트웨이 수신 기록을 비교하세요.
```

API에는 기존 `summary`와 함께 구조화된 `explanation` 및 `llm_metadata`를 제공합니다.
`llm_metadata`의 `finish_reason`, `input_tokens`, `output_tokens`는 서버가 제공한 값이며,
제공되지 않은 토큰 수는 `null`입니다.

| 상황 | `llm_status` | `llm_error` | 설명 표시 |
| --- | --- | --- | --- |
| 정상 종료와 형식 검증 통과 | `generated` | `null` | 관측 + 모델의 두 필드 |
| 출력 토큰 한도 도달 | `unavailable` | `output_truncated` | 코드 기반 관측만 |
| JSON·필드·길이 검증 실패 | `unavailable` | `invalid_model_output` | 코드 기반 관측만 |
| 종료 사유 누락 또는 비정상 종료 | `unavailable` | `unexpected_finish_reason` | 코드 기반 관측만 |
| 연결·서버 오류 | `unavailable` | 오류 종류 | 코드 기반 관측만 |
| 모델 미사용 | `disabled` | `null` | 코드 기반 관측만 |

실패한 모델 설명은 `explanation=null`로 반환합니다.
유효한 JSON이라도 종료 사유가 `length`이면 잘린 출력으로 처리합니다.
출력 글자 수 제한과 토큰 수 제한은 서로 다르므로 토큰 상한은 256으로 유지했습니다.

## 6. 개발과 테스트

Docker 없이 Python 코드만 개발할 때는 [uv](https://docs.astral.sh/uv/)를 사용합니다.

```bash
uv sync --frozen
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pytest -q
uv run --frozen uvicorn rpi5_agent.api:app --host 127.0.0.1 --port 8000
```

`uv`가 Python 3.12 환경과 프로젝트 전용 `.venv`를 준비합니다.
`uv.lock`은 Git에 포함하며 Docker 빌드는 이 파일로 의존성 버전을 고정합니다.
의존성 변경 시 `uv lock` 후 테스트하고 `pyproject.toml`과 `uv.lock`을 함께 반영합니다.

네이티브 실행에서 모델을 연결할 때는 `.env`의 `LLM_BASE_URL`을
`http://127.0.0.1:8080/v1`로 변경합니다. 기본 `.env.example`의 주소는 Docker용입니다.

테스트는 가상 시나리오 판정, 입력 검증, 모델 오류 시 증거 유지, 동시 요청 제한,
로컬 HTTP 모형 서버를 통한 실제 LangChain API 호출과 구조화 출력을 검증합니다.
출력 잘림, 비정상 종료, JSON 오류, 초과 길이·추가 필드·줄바꿈 등도 검사합니다.
테스트 통과만으로 실제 Qwen 추론이나 RPi·STM32 연결이 검증되는 것은 아닙니다.

```text
src/rpi5_agent/
├── api.py              HTTP 진입점과 동시 요청 제한
├── config.py           환경변수와 로컬 모델 설정
├── models.py           입력·증거·진단 결과 형식
├── errors.py           사용 불가능한 모델 응답의 오류 코드와 메타데이터
├── workflow.py         LangGraph 흐름과 규칙 판정
├── llm.py              LangChain 모델 설명 생성
└── tools/fixtures.py   가상 증거 공급자
```

## 7. 운영 명령과 자원 확인

```bash
docker compose logs --tail=100 -f
docker stats --no-stream
docker compose stop
docker compose down
```

기본 Agent 제한은 RAM 768MiB, CPU 최대 1코어 상당입니다. 초기 설정값이므로 실제 사용량에 맞춰 조정합니다.
이 제한은 호스트에서 실행하는 `llama-server`에는 적용되지 않습니다.
`docker stats`와 함께 호스트의 `free -h`도 확인해 모델과 Docker 데몬을 포함한 전체 여유 메모리를 봅니다.
컨테이너 로그는 파일당 5MB, 최대 3개로 순환합니다.
컨테이너는 일반 사용자로 실행되며 기본 파일시스템은 읽기 전용입니다.
기록 저장을 추가할 때는 `data/` 등에 필요한 볼륨과 쓰기 권한을 별도로 설정합니다.

## 8. 다음 개발 단계

1. 통신 규약 합의 후 DBC·코덱과 ECU 주소표를 버전 관리합니다.
2. 호스트에서 수집한 CAN·UDS 증거를 `ToolResult`로 변환하는 공급자를 추가합니다.
3. `gateway.snapshot`에 원본 BMS의 유효 여부와 경과시간을 연결합니다.
4. 가상 데이터와 실측 데이터의 출처를 결과에서 계속 구분합니다.
5. 승인 정보와 대상·명령·파라미터를 검증하는 공통 계층을 만든 뒤 제어 기능을 추가합니다.

Agent가 직접 SocketCAN을 사용하도록 확장한다면 `can0`은 `/dev/can0` 파일이 아닌 네트워크 인터페이스임에 유의합니다.
현재 bridge 구성에 장치를 마운트하는 것만으로 `can0`가 나타나지는 않습니다.
호스트 네트워크 공유 또는 호스트 수집 서비스 중 방식을 정하고, CAN 설정은 호스트에서 관리합니다.

참고 문서:

- [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)
- [LangChain ChatOpenAI](https://docs.langchain.com/oss/python/integrations/chat/openai)
- [uv와 Docker](https://docs.astral.sh/uv/guides/integration/docker/)
- [Docker 자원 제한](https://docs.docker.com/engine/containers/resource_constraints/)
