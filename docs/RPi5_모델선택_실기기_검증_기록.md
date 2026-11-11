# RPi5 로컬·Pamout 모델 선택 실기기 검증

검증일: 2026-11-11 KST. 배포 준비 약 16:02부터, 모델 경로·재부팅 검증은 16:10~16:17에 수행했다.

> 이 문서는 `e5b62df` 배포 당시 기록이다. 이후 `9682de3`의 SWD 정책·진단 기록·실제 모델 검증은 [후속 실기기 기록](RPi5_SWD_선택_진단_실기기_검증.md)을 참조한다. 새 요청에서 설명 오류·출력 거절·스로틀링이 관측됐으므로 이전의 형식 성공을 새 코드의 의미 품질 통과로 해석하지 않는다.

## 1. 범위와 배포 기준

| 항목           | 확인값                                                                    |
| -------------- | ------------------------------------------------------------------------- |
| 호스트·접속    | `pi32`, `pi@100.92.169.70` (Tailscale)                                    |
| 실행 환경      | RPi5 ARM64, Docker의 Python 3.12 Agent                                    |
| 배포 레포      | `/home/pi/RPI5_Agent_Docker`, `main`                                      |
| 검증·배포 커밋 | `e5b62df601b8fb64eeff99bac577b799e8ab7b6c`                                |
| 이전 커밋      | `dc166c358f23bbc14188b460b6d5914eb5548394`                                |
| Agent 이미지   | `sha256:f7cdcadb92ce14d4b9f1ac13f28a48276287c45979397cf5f3cb8b4d4e466b2e` |
| 로컬 모델      | 호스트 `llama-server`, alias `local-qwen`, Qwen 2.5 3B                    |
| 외부 모델      | Pamout `https://llm.pamout.com/v1`, 요청 ID `glm-5.3`, `prompt` 모드      |
| 진단 입력      | 모든 요청은 `fixture`. CAN·STM32 실측·제어 검증이 아님                    |

PC의 원격 main과 커밋 일치를 확인한 뒤 RPi의 clean 작업 트리를 fast-forward했다. RPi에서 새 ARM64 이미지를 빌드하고 컨테이너를 재생성했다. 검증 후 RPi Git HEAD도 위 커밋이며 작업 트리는 clean이다. 이 문서 등 이후 작성한 로컬 기록은 검증 대상 커밋과 구분한다.

접속 초기에 PC Tailscale이 NoState여서 SSH가 실패했다. 사용자가 연결을 복구한 뒤 동일 주소로 진행했다. 이후 실행·재부팅 시험에서 Tailscale 접속을 확인했다.

## 2. 설정 보존과 복귀 준비

- 배포 ID: `20261111T070315Z`.
- RPi 백업: `/home/pi/.local/state/rpi5-agent-deployments/20261111T070315Z/`.
- 기존 `.env`는 `env-before`, 기존 tracked 소스는 `source-before.tar.gz`, 메타데이터는 `deployment.json`에 보관했다.
- 기존 이미지 태그: `rpi5-agent:rollback-20261111T070315Z`.
- 백업 상위 디렉터리 0700, `.env` 및 비밀 백업 0600. 테스트용 공개 소스 복사본은 이 비공개 상위 디렉터리 안에 분리했다.
- 기존 RPi의 로컬 URL·모델 등 설정을 유지하고 PC의 외부 설정만 SSH stdin으로 전달했다. 실제 키를 명령 인수·로그·문서에 넣지 않았다.
- RPi는 `LLM_ENABLED=true`, `EXTERNAL_LLM_ENABLED=true`. 로컬 주소는 `http://host.docker.internal:8080/v1`이다.
- PC `.env`는 변경하지 않았다. RPi에서 기존 로컬 모델 설정 보존과 재부팅 후 `.env` 0600 유지를 확인했다.

복귀 이미지는 보관만 했으며 실제 롤백을 실행한 것은 아니다. 필요하면 이 태그와 비공개 설정/소스 백업을 기준으로 별도 복귀 절차를 수행한다. 비밀 백업을 Git에 추가하지 않는다.

## 3. 컨테이너·모의 서버 자동 테스트

| 항목                     | 결과                                                                                                                              |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------------- |
| 이미지·프로세스 아키텍처 | `linux/arm64`, `aarch64`                                                                                                          |
| 사용자·파일시스템        | UID/GID `10001:10001`, read-only root                                                                                             |
| CPU 한도                 | `NanoCpus=1000000000`                                                                                                             |
| 메모리 한도              | 설정과 `memory.max` 모두 `805306368` byte (768 MiB)                                                                               |
| 외부 모델 목록 조회      | **Agent 컨테이너 내부**의 인증된 GET HTTP 200, `glm-5.3` 포함                                                                     |
| 기본 API                 | `/health`, `/scenarios`, `/docs`, `/openapi.json` 모두 HTTP 200, OpenAPI의 `llm_target` 확인, fixture 세 시나리오 smoke test 통과 |
| ARM64 자동 테스트        | **62 passed in 12.57s**                                                                                                           |

자동 테스트는 배포한 이미지와 별도의 일회성 컨테이너에서 수행했다. 같은 768 MiB·CPU 1코어 한도, 일반 사용자·read-only root를 유지하고 `.env`나 실제 키는 마운트하지 않았다. `uv.lock` 버전의 pytest 관련 패키지를 tmpfs에 준비하고 테스트 소스만 읽기 전용으로 제공했다. 실행 후 테스트 컨테이너는 제거됐다.

초기 테스트 준비에서 가상환경에 pip가 없고 호스트의 0600 테스트 파일을 UID 10001이 읽지 못했다. base Python의 pip로 tmpfs에 의존성을 설치하고, 공개 테스트 파일의 읽기 가능한 복사본을 사용해 해결했다. Agent 실행 권한이나 소스 기능을 바꾼 것은 아니다.

62개 테스트는 명시 local·외부 비활성·fallback 비활성, 외부/양쪽 오류·timeout·429/500·형식/종료 사유 오류 시 판정 보존과 재호출 제한 등을 모의 서버로 검증한다. 이 결과와 아래 실제 모델 요청을 구분한다.

## 4. 재부팅 전 실제 모델 요청

| 경로                            | 시나리오      | 선택 결과·이유               | 경과시간 | 입력/출력 토큰 |
| ------------------------------- | ------------- | ---------------------------- | -------- | -------------- |
| `llm_target=local`              | `bms_timeout` | local / `explicit_local`     | 21.98초  | 366 / 37       |
| `llm_target=external`           | `bms_timeout` | pamout / `explicit_external` | 11.91초  | 388 / 1170     |
| `llm_target=auto`               | `bus_off`     | pamout / `error_findings`    | 13.29초  | 356 / 1308     |
| `llm_target=auto`               | `healthy`     | local / `local_default`      | 12.88초  | 354 / 46       |
| 격리한 로컬 접속 실패 후 `auto` | `bms_timeout` | pamout / `local_failure`     | 17.41초  | 388 / 1685     |

다섯 경로 모두 HTTP 200·`llm_status=generated`·`finish_reason=stop`, 두 필드의 JSON·길이·단일 행 검증을 통과했다. 예상 finding 코드와 선택 이유를 대조했다. 실패 전환은 배포된 컨테이너 안의 별도 프로세스에서 loopback 접속 불가 주소를 설정 복사본에 주입했다. 운영 중인 Qwen을 중지하거나 실서비스 설정을 장애 주소로 바꾸지 않았다. 호출 이력에는 로컬 `OpenAIConnectionError`와 Pamout 성공이 순서대로 남았다.

로컬 실제 추론 중 `/health`는 HTTP 200, 추가 진단은 HTTP 429였고 완료 후 진단 요청은 HTTP 200으로 복귀했다.

로컬 응답은 `BMS의 갱신이 지연됨` / `BMS 데이터의 갱신 시간 확인` 등 짧은 구절이었다. `healthy`에서는 `게이트웨이와 BMS 갱신 속도가 빠르다`처럼 관측을 반복하거나 최신 여부를 속도로 표현한 한계도 있었다. 형식 성공과 별개로 문법·원인 후보·정상 설명의 의미 품질 평가는 남아 있다.

## 5. 실제 재부팅 후 자동 복구

기존 설정과 복귀 이미지를 보존하고 Docker·Tailscale·Qwen 서비스의 enabled/active를 확인한 뒤 16:15 KST에 재부팅했다. 이후 서비스 수동 start 명령 없이 다음을 확인했다.

| 항목                    | 결과                                   |
| ----------------------- | -------------------------------------- |
| 부팅 전 boot ID         | `aad8cd75-54fb-4261-b43c-34d9049cf510` |
| 부팅 후 boot ID         | `61087baf-1104-46a9-8ad8-6c9e5c832e99` |
| Docker·Tailscale·Qwen   | 모두 enabled/active                    |
| Agent·모델 health       | 정상, Agent 컨테이너 healthy           |
| 배포 이미지·커밋        | 재부팅 전과 동일, RPi 작업 트리 clean  |
| 메모리 한도             | `memory.max=805306368` 유지            |
| Qwen 서비스 재시작 횟수 | 점검 시 0                              |
| fixture smoke test      | 세 시나리오 재통과                     |

재부팅 후 실제 요청도 확인했다.

| 요청                      | 경과시간 | 입력/출력 토큰 | 결과            |
| ------------------------- | -------- | -------------- | --------------- |
| `bms_timeout`, `local`    | 21.68초  | 366 / 33       | generated, stop |
| `bms_timeout`, `external` | 16.20초  | 388 / 1573     | generated, stop |

전체 실기기 과정에서 실제 Qwen 요청 3건, Pamout Chat Completions 4건과 컨테이너의 모델 목록 GET 1건을 수행했다. 격리 실패 주입의 로컬 요청은 실제 Qwen 요청 수에 포함하지 않는다.

## 6. 자원·냉각 관측과 한계

- 최초 호스트 점검은 54°C, 기능 시험 시작 표본은 58.4°C·`throttled=0x0`이었다.
- 재부팅 전 모델 경로 시험의 최고 표본 온도는 **85.9°C**, 재부팅 후 두 요청의 최고 표본은 **84.8°C**였다. 약 1초 간격 표본의 최고값이며 순간 최대를 보장하지 않는다.
- `0x80008`, `0xe0008` 외에 **`0xe0006`**도 관측했다. bit1(주파수 제한)·bit2(현재 스로틀링)가 설정된 표본이 있어 현재 제한도 실제 발생했다. 종료 때의 `0xe0000`은 해당 현재 비트가 해제되고 이력이 남은 상태다. [Raspberry Pi get_throttled 비트 설명](https://www.raspberrypi.com/documentation/computers/os.html#get_throttled)
- 재부팅 전 모델 경로 시험 중 호스트 최소 가용 메모리는 약 5196.2 MiB였고 swap 사용량은 표본상 0이었다.
- 재부팅 후 요청 종료 시 Agent `docker stats`는 약 `130.2 MiB / 768 MiB`, CPU 0.05%였다. 이는 종료 시점 표본이며 모델 추론 중 최대 사용량이 아니다. 호스트 Qwen은 Agent 컨테이너 메모리 제한 밖이다.
- 종료 직후 온도는 71.05°C, 현재 제한 비트는 해제 상태였다. 물리 쿨러 모델·장착 상태는 원격으로 확인하지 못했고 `fan1_input`도 검출되지 않았다. 이것만으로 팬이 없다고 확정하지 않는다.
- 16:22 KST 최종 점검은 58.95°C·`throttled=0xe0000`, Agent healthy·Qwen active였다. Agent의 `memory.events`는 `max=0`, `oom=0`, `oom_kill=0`이었다. 이는 재부팅 후 해당 컨테이너의 집계이며 호스트 Qwen의 메모리 사용량이나 전체 운용 기간의 무장애를 보장하지 않는다.

측정 시간은 각기 다른 프롬프트·모델·토큰 수·캐시·온도·네트워크 조건을 포함한다. 로컬/외부의 공정한 속도 비교나 평균/최악 응답 시간 보장이 아니다. 냉각 장착·공기 흐름을 확인한 뒤 동일 조건의 반복 성능 시험이 필요하다. `json_object`/`json_schema` 외부 모드는 이번에도 실서비스 검증 범위가 아니다.

## 7. 증거와 남은 작업

RPi 비공개 배포 디렉터리에 다음을 보관했다.

- `runtime-verification.json`: 원본 진단 응답·선택 이력·토큰·자원 표본·재부팅 검증.
- `arm64-tests.log`: 최종 ARM64 테스트 결과.
- `verify-runtime.py`: 배포 후 기능 경로 검증에 사용한 일회성 코드.
- `deployment.json`, `build.log`, 기존 소스/설정 및 복귀 이미지 식별값.

PC에는 비밀을 제외한 원본을 `data/rpi-pamout-runtime-20261111.json`으로 회수했다. `data/`는 Git 무시 대상이다. 키·`.env` 원문은 공유용 증거로 복사하지 않았다.

회수한 최종 JSON의 SHA-256은 `0c1efa9c52fc3bc49d26e4cafa74ed6d3f2219d88d5aeac37ca7ac9d63fee993`이다. RPi 원본 바이트를 그대로 저장했으며 최종 API·운영 상태 점검도 포함한다.

남은 항목은 냉각 장치의 실제 장착 확인·스로틀링 보완 후 반복 성능, 설명의 의미 품질, 실제 CAN·UDS·센서 증거 공급자 연결이다. 이번 완료 항목을 이 후속 작업의 완료로 확대하지 않는다.
