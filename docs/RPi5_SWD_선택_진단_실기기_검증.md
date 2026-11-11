# RPi5 SWD 선택 진단 구조 실기기 검증

검증일: 2026-11-11 KST. 17:28 배포 준비, 실제 모델 요청은 17:31~17:33, 최종 운영 점검은 17:34에 수행했다.

**결과**: ARM64 배포·자동 테스트 112개·가상 진단 13종·제품/벤치 정책은 통과했다. 실제 모델 5건 중 4건은 출력 계약을 통과했고 1건은 검증 실패로 제외됐다. Qwen의 사실과 다른 설명 및 스로틀링이 확인돼 설명 품질·반복 성능은 통과로 판단하지 않는다.

## 1. 배포와 범위

| 항목 | 확인값 |
| --- | --- |
| RPi | `pi32`, `pi@100.92.169.70`, `aarch64` |
| 배포 경로 | `/home/pi/RPI5_Agent_Docker` |
| 배포 커밋 | `9682de33a9a84a278fe6c8ab337b30b33608fe20` |
| 이전 커밋 | `e5b62df601b8fb64eeff99bac577b799e8ab7b6c` |
| 새 ARM64 이미지 | `sha256:0de4c6ff0c4997c413a5cd3036158489914a25faa21fbbe9784f559727ddfd12` |
| 운영 프로필 | `product`, SWD 공급자 실행 전 차단 |
| 로컬·외부 모델 ID | `local-qwen`, Pamout `glm-5.3` |
| 진단 입력 | 모두 `fixture`. 실제 ECU·CAN·UDS·SWD 접근과 ELF 해석은 미검증 |

사용자가 push한 원격 main의 커밋 일치를 확인한 뒤 clean RPi 작업 트리를 fast-forward했다. RPi에서 native ARM64 빌드 후 컨테이너를 재생성했다. RPi 최종 HEAD는 위 커밋이며 작업 트리는 clean이다. 이후 작성한 이 검증 기록은 별도 로컬 변경이다.

- 배포 ID: `20261111T082810Z`.
- 비공개 백업: `/home/pi/.local/state/rpi5-agent-deployments/20261111T082810Z/`.
- 기존 `.env`는 `env-before`, 이전 tracked 소스는 `source-before.tar.gz`, 빌드 로그는 `build.log`로 보관했다.
- 복귀 이미지 태그: `rpi5-agent:rollback-20261111T082810Z`. 보관만 했으며 실제 롤백은 수행하지 않았다.
- RPi `.env`는 전후 해시가 같고 0600 권한을 유지했다. 기존 모델·키 설정을 변경하거나 PC의 키를 다시 전달하지 않았다.

## 2. ARM64 자동 테스트와 실행 조건

**112 passed in 13.68s**. 배포 이미지의 별도 일회성 컨테이너에서 수행했다.

- UID/GID `10001:10001`, 읽기 전용 root, CPU 1코어 상당, 768 MiB 한도를 유지했다.
- 공개 테스트 소스·pyproject만 읽기 전용으로 마운트했다. `.env`나 실제 키는 테스트 컨테이너에 전달하지 않았다.
- lockfile 버전의 pytest 의존성을 tmpfs에 설치했다. 모의 HTTP 서버를 사용하며 실제 Pamout/Qwen 호출은 이 테스트에 포함하지 않는다.
- 테스트 후 일회성 컨테이너를 제거했다. 운영 컨테이너의 `memory.max=805306368`도 확인했다.

주요 자동 검증은 SWD/제어 도구 실행 전 차단, 순서·누락·중복·잘못된 데이터 처리, NaN 재검증, 기록과 심벌의 식별값 대조, LLM 입력 필터·크기 제한과 기존 모델 선택·실패 전환이다.

## 3. 배포된 API와 정책

`python3 scripts/smoke_test.py`로 **13개 시나리오를 모두 통과**했다.

| 범위 | 결과 |
| --- | --- |
| 기존 정상·BMS 지연·로컬 Bus-Off | 기존 finding 유지 |
| ECU 조회 시간 초과·부팅 변경 | 원인 미확인과 부팅 식별값 변경 구분 |
| 크래시 있음/없음·미지원·시간 초과·손상 | 상태와 코드 판정 구분 |
| 심벌 없음·빌드 불일치 | 함수·소스 줄을 채우지 않고 미확인으로 처리 |
| 제품 SWD 접근 | `debug_access_unavailable`, `reason=profile_disabled` |

`/health`, `/scenarios`, `/capabilities`, `/docs`, `/openapi.json`은 모두 HTTP 200이었다. `/capabilities`는 `product`, `physical_collection=false`, `control_tools=false`이며 허용 도구에 SWD가 없었다. 진단 요청으로 `diagnostic_profile=bench`를 넣으면 HTTP 422였다.

벤치 정책은 운영 설정을 바꾸지 않고 같은 이미지 안의 별도 TestClient에서 확인했다. LLM을 끈 `bench` 설정과 가상 공급자는 `debug_access_unavailable`, `reason=probe_missing`을 반환했다. 이는 모의 접근 불가이며 실제 프로브·RDP 시험이 아니다.

## 4. 실제 모델 요청

| 경로·시나리오 | 선택 이유 | 시간 | 입력/출력 토큰 | 출력 계약 결과 |
| --- | --- | --- | --- | --- |
| `local`, `crash_recorded` | `explicit_local` | 42.32초 | 742 / 41 | generated, stop |
| `external`, `symbol_build_mismatch` | `explicit_external` | 14.00초 | 846 / 1253 | generated, stop |
| `auto`, `crash_recorded` | `error_findings` | 13.75초 | 775 / 1316 | unavailable, invalid_model_output |
| `auto`, `healthy` | `local_default` | 11.44초 | 529 / 39 | generated, stop |
| 격리 로컬 접속 실패 후 `auto`, SWD 접근 불가 | `local_failure` | 13.21초 | 674 / 1130 | generated, stop |

Qwen 실제 요청 2건, Pamout 실제 Chat Completions 3건을 수행했다. 마지막 실패 주입은 배포 컨테이너의 분리된 프로세스에서 설정 복사본의 로컬 주소를 접속 불가 loopback으로 바꿨다. 운영 Qwen을 중지하거나 운영 설정을 바꾸지 않았다. 이력은 로컬 `OpenAIConnectionError` 후 Pamout 1회 성공이었다.

실제 추론 중 `/health`는 HTTP 200, 추가 진단은 HTTP 429였으며 완료 후 HTTP 200으로 복귀했다.

### 출력 검증 실패 시 보존

Pamout 자동 선택 요청은 `finish_reason=stop`이었지만 출력 검증에 실패했다. `llm_status=unavailable`, `llm_error=invalid_model_output`, `explanation=null`을 반환했다.
`GATEWAY_FRESH`, `BMS_FRESH`, `CRASH_RECORD_PRESENT`, `CRASH_SOURCE_LOCATION_UNKNOWN`과 원본 증거 3개는 유지했다.
실패한 모델 원문을 별도로 저장하지 않아 정확히 어느 필드가 거절됐는지는 이번 증거로 확인할 수 없다. 길이 초과 등 특정 원인을 추정하지 않는다. 검증을 느슨하게 하거나 성공으로 표시하지 않았다.

### 설명 품질 검토 — 미통과

- Qwen의 `crash_recorded` 설명은 **“현재 ECU 고장이 원인입니다.”**였다. 과거 저장 기록과 현재 고장을 구분하지 못한 근거 없는 단정이다.
- Qwen의 `healthy` 설명은 **“Gateway와 BMS 갱신이 불완전하다.”**였다. 코드의 `GATEWAY_FRESH`, `BMS_FRESH`와 모순된다.
- Pamout의 빌드 불일치 설명은 원인 불확실성을 표시하고 기록 당시 빌드에 맞는 심벌 확보를 제안했다. 해당 응답은 문맥과 부합했지만 일반적인 정확도 보장은 아니다.
- 실패 전환 응답은 원인 불확실성을 표시했지만 다음 확인이 SWD 프로필 점검에 치우쳤다. 제품 운용의 CAN 기록 중심 설명은 추가 개선이 필요하다.

따라서 **JSON·길이·종료 사유의 통과를 진단 의미의 정확성으로 해석하면 안 된다.** 기존 `generated`는 형식 검증을 통과한 생성 상태이며 의미 검증기는 아직 없다. 모델 설명보다 원본 증거와 코드 판정을 우선해서 검토해야 한다.

## 5. 자원과 최종 운영 상태

- 접속 초기 온도 54.9°C·`throttled=0x0`.
- 모델 시험 최고 표본 온도 **87.0°C**, `0xe0006` 표본에서 현재 주파수 제한·스로틀링 발생. 약 1초 간격 표본이며 순간 최대를 보장하지 않는다.
- 종료 직후 70.5°C, 17:34 최종 점검 64.45°C·`0xe0000`이었다. 현재 제한 비트는 해제됐고 이력이 남은 상태다.
- 모델 요청 사이 온도를 확인했으며 장시간·반복 부하 시험은 수행하지 않았다. 물리 쿨러 장착 여부는 이번에도 확인하지 못했다.
- 호스트 최소 가용 메모리 표본 약 5160.75 MiB, swap 사용 표본 0. Agent `memory.events`의 max/oom/oom_kill은 모두 0이었다.
- 요청 종료 후 Agent는 약 98.77 MiB/768 MiB·CPU 0.07%였다. 종료 표본이며 추론 중 최고 사용량이 아니다. 호스트 Qwen은 Agent 한도 밖이다.
- Agent healthy, Qwen active. Docker·Tailscale·Qwen은 모두 enabled/active였다.
- 이번에는 재부팅하지 않았다. 이전 커밋에서 수행한 재부팅 시험을 새 커밋의 시험으로 기록하지 않는다.

이번 수치는 서로 다른 프롬프트·캐시·출력 토큰·온도·네트워크 조건의 단일 관측이다. 모델 간 속도 비교·평균/최악 지연 또는 장기 안정성 보장으로 쓰지 않는다.

## 6. 증거와 후속 작업

RPi 비공개 백업의 `swd-runtime-verification.json`, `arm64-tests.log`, `verify-runtime.py`, `deployment.json`, `build.log`에 근거를 보관했다.
JSON 원본은 PC의 `data/rpi-swd-runtime-20261111.json`으로 회수했다(Git 무시 대상). 실제 키가 포함되지 않았음을 확인했다.

원본 SHA-256: `f31e319ce7fb531178da86c63dc995caba75fff58d2b1fd0beb513294c4f8bad`.

초기 일회성 검증 코드가 HTML `/docs`를 JSON으로 파싱해 실패했으나 실제 모델 호출 전에 수정했다. 애플리케이션 소스 변경 없이 최종 검증을 마쳤다.

남은 작업:

1. 정상·과거 사건에 대한 설명의 근거 일치 검증과 회귀 예제를 보강한다. 프롬프트만으로 의미 정확성을 보장하지 않는다.
2. 출력 거절의 필드·오류 종류·길이를 비밀 없이 기록할 수 있도록 오류 관측성을 보완한다. 전체 오류 입력이나 인증 정보를 로그에 남기지 않는다.
3. 냉각 장착·공기 흐름을 확인하고 스로틀링을 보완한 뒤 동일 조건 반복 성능을 검증한다.
4. 실제 CAN·UDS 공급자, ECU 기록, Build ID별 ELF 해석·영구 저장을 구현한다.
