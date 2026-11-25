# RPi 5 Diagnostic Agent (`RPI5_Agent_Docker`) — Project Context & Agent Guide

> 이 파일은 **모든 AI 코딩 에이전트(Antigravity, Gemini CLI, Claude Code, Codex, Cursor, Copilot, Windsurf)와 사람 개발자가 공통으로 참조하는 단일 기준 문서(Single Source of Truth)**입니다.
> 다른 PC로 이동하거나 팀원이 새 환경에서 작업할 때도 이 문서와 `.agents/`만 읽으면 프로젝트 맥락·아키텍처·검증 기준을 그대로 이어갈 수 있습니다.

---

## 1. 시스템 아키텍처 & 3-노드 토폴로지

* **단일 Classic CAN 버스 확정 (2026-11-17)**: 초기 검토안이던 `NUCLEO-G474RE` 도메인 게이트웨이와 CAN FD 백본은 제외했습니다.
* **버스 구성**: **보드 A (`NUCLEO-F103RB`, 열관리 제어기 ECU)** + **보드 B (`NUCLEO-F103RB`, 스마트 냉각팬 ECU)** + **RPi 5 (`can0`, Seengreat RS485 Dual CAN I HAT, `MCP2515`)**가 단일 **Classic CAN (`500 kbit/s`, Standard 11-bit ID, 최대 8B)** 버스에 직결됩니다.
* **보조 UART**: 벤치 환경에서는 ST-LINK VCP (`/dev/serial/by-id/usb-STMicroelectronics_STM32_STLink_*-if02`, `115200 8N1`)를 보조 진단 소스로 함께 수집합니다.

---

## 2. 멀티 레포 · 협업 맵

| 구성 요소 | 위치 / 저장소 | 역할 |
| :--- | :--- | :--- |
| **AI 진단 Agent (본 레포)** | `RPI5_Agent_Docker` (`main`) | FastAPI + LangGraph + LangChain 진단 Agent, 호스트 읽기 전용 CAN/UART 수집 데몬 |
| **환경 구축 · 아키텍처 문서** | `ssafy_embedded_pjt_setup` (`main`) | RPi 5 / STM32 셋업 스크립트, `docs/` 카테고리별 설계·HW 검증·학습 자료 |
| **ECU 펌웨어 (임베디드 파트)** | `aide-ecu` (`main`) | 보드 A(`board_a/`), 보드 B(`board_b/`), 공통 스택(`common/`) |
| **팀 문서 · 이슈 트래킹** | GitHub Wiki · Jira `PJT` | `PJT-25`(AI 에픽), `PJT-18`(보드 A), `PJT-8`(보드 B) |

---

## 3. 현재 구현 현황 및 런타임 모드

| 모드 (`EVIDENCE_PROVIDER` / `LIVE_SPECIFICATION`) | 명세 ID | 설명 및 상태 |
| :--- | :--- | :--- |
| **`fixture`** (기본값) | `fixture-bench-v1` | 가상 벤치 시나리오 13종 (`POST /verify` 통과). 실물 CAN/UART 없이 PC/CI에서 항상 실행 가능 |
| **`http_collector` + `hw_test_f103`** (Phase 1) | `hw-test-f103-v1` | `0x100` (DLC 4, `A5 5A 00 <cnt>`) + ST-LINK VCP 구간 차분(`errcb_delta`). 구현·RPi 5 실기기 검증 완료 (`PJT-12`, `PJT-26`) |
| **`http_collector` + `aide_ecu`** (Phase 2-A) | `aide-ecu-v1` | 보드 A·B 주기 프레임(`A_FAN_CMD 0x080`, `A_THERMAL_STATUS 0x180`, `B_FAN_STATUS 0x1C0`) 디코더 + 고장·3-노드 교차 판정 구현 및 PC 테스트 완료 (`PJT-27`). `AIDE_B_FAULT_ENCODING=code|bitmask`, `AIDE_INCLUDE_UART=false|true` 지원 |

### 하이브리드 LLM 라우팅 (`PJT-14`, `src/rpi5_agent/llm.py`)
* **`use_llm=true`, `llm_target="auto"` + 모든 finding이 `info` (정상)**: 모델 호출 없이 0토큰 `deterministic_template` 즉시 반환.
* **`warning` 또는 `error` finding 존재**: `EXTERNAL_LLM_ENABLED=true`일 때 외부 Pamout API(`glm-5.3` 등) 우선 호출, 오류 시 로컬 Qwen으로 순환 폴백 금지.
* **의미 가드레일 (`validate_explanation_semantics`)**: 금지 제어 동작·정상→고장 모순·과거 크래시 현재 단정·미검증 소스 위치(`file.c:123`) 추측을 코드에서 차단.

### 팀 협의 · 실물 통합 대기 항목 (`TBD`)
* 능동 진단 프레임 `DIAG_REQ` / `A_DIAG_RESP` / `B_DIAG_RESP` 방식 통일(`0x380`/`0x300` `UDS + ISO-TP` vs 8B 단일 프레임), 패딩(`0x00`/`0xCC`), DID 번호 배정·엔디안, `F-10` 고장 주입 명령.
* 보드 B 주기 메시지 최종 ID·고장 형식(단일 `fault_code` vs 비트 플래그) 및 임계치(`NO_ROTATION`, `RPM_DEVIATION`) 확정.
* RPi 5 실기기에서의 보드 A·B 2-보드 물리 연결 E2E 검증 (`PJT-27`).

---

## 4. 개발 · 테스트 · 검증 표준 명령어

```bash
uv sync --frozen
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pytest -q
docker compose config --quiet
uv run python scripts/smoke_test.py
```

* Python 3.12 + `uv` 기준이며 CI(`.github/workflows/ci.yml`)도 `ruff check`, `ruff format --check`, `pytest -q`를 실행합니다.
* 호스트에 `httpx`가 설치되어 있지 않을 때는 `python3 scripts/smoke_test.py` 대신 `uv run python scripts/smoke_test.py`를 사용합니다.

---

## 5. 핵심 코드 · 문서 지도

* `src/rpi5_agent/api.py`: FastAPI 엔드포인트 (`/health`, `/scenarios`, `/capabilities`, `/diagnose`, `/verify`) 및 동시 요청 1건 제한 (`BoundedSemaphore(1)`).
* `src/rpi5_agent/config.py`: `Settings` 및 `build_live_specification()` (`hw_test_f103` / `aide_ecu` 분기).
* `src/rpi5_agent/specification.py`: `FIXTURE_SPECIFICATION`, `HW_TEST_SPECIFICATION`, `AIDE_ECU_SPECIFICATION`.
* `src/rpi5_agent/hw_collector.py` & `scripts/run_hw_collector.py`: 호스트 읽기 전용 CAN/UART 수집 엔진 및 HTTP 브리지 (`:8090`).
* `src/rpi5_agent/tools/collection.py` & `live_provider.py`: 도구 화이트리스트, `product`/`bench` 프로필 검사, `HttpHardwareProvider`.
* `src/rpi5_agent/assessment.py`: 결정적 판정 (`evaluate`, `evaluate_live_hw_test`, `evaluate_live_aide_ecu`).
* `src/rpi5_agent/evidence_summary.py`: LLM 전달용 스칼라 화이트리스트(`DATA_FIELDS`) 및 6,000B 페이로드 가드.
* `deploy/systemd/`: RPi 5 `can0`·`rpi5-hw-collector`·`llama-server` 상시 기동 스크립트.
* [docs/실측_CAN_UART_연동_설계.md](docs/실측_CAN_UART_연동_설계.md): 실측 파이프라인 및 Phase 2 본 규약 연동 상세 설계서.
* `.agents/rules/` · `.agents/skills/`: 세부 안전 규칙 및 작업별 런북(모든 에이전트/사람 공용).

---

## 6. 팀 협업 경계 & 개인 PC 설정

* **다른 파트 소유 자산 보호**: 임베디드 팀원의 저장소(`aide-ecu`)나 팀원 담당 Jira 이슈(`PJT-15`, `PJT-16`, `PJT-19`, `PJT-20`, `PJT-21`\~`PJT-24`)는 명시적 요청 없이 수정하지 않습니다.
* **비밀·개인 환경 격리**: `.env`, API 키, 개인 PC 절대 경로, SSH/Tailscale 호스트 주소 및 비밀번호는 Git·문서·Jira·Wiki에 절대 커밋하지 않습니다.
* **개인 PC 전용 메모/오버라이드**: 개인 환경에만 적용할 지침은 `.gitignore`에 등록된 `AGENTS.local.md`, `CLAUDE.local.md` 또는 `.gemini/settings.local.json`에 작성하세요.
