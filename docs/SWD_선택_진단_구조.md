# SWD 없이 진단하는 Agent 구조

후속 범위 결정(2026-11-13): 장비 정보가 충분한 환경의 검증을 우선한다.
[장비 정보 기반 검증](장비_정보_기반_검증.md)에 현재 가상 명세·케이스 비교와 실기기 준비 조건을 정리했다.
아래 SWD 선택·오류 계약은 유지하며 정보 부족 장비 조사나 범용 로그 파싱은 후속 범위다.

작성일: 2026-11-11. 셋업 레포의 장비별 Agent Tool 인터페이스와 CAN 아키텍처 v3.2를 반영한 소프트웨어 구현이다.
실제 CAN·UDS·SWD 수집, ECU 기록 저장, ELF 해석은 아직 구현하지 않았다. 모든 증거의 `source`와 결과의 `mode`는 `fixture`다.

## 1. 흐름과 책임

```text
진단 요청의 scenario
  → 공급자 plan: 필요한 읽기 도구·대상 선택
  → 공통 수집 계층: 프로필·허용 목록 검사
  → 공급자 read: 가상 증거 반환
  → 증거별 검증·규칙 판정
  → 코드 관측 요약 + 선택적 로컬/Pamout 설명
```

- `tools/collection.py`: 수집기 Protocol과 실행 전 허용 목록 검사. 도구별 실패는 다른 증거를 보존하며 재시도하지 않는다.
- `tools/fixtures.py`: 기본 공급자. 물리 CAN이나 USB에 연결하지 않는다.
- `assessment.py`: 도구·대상으로 증거를 찾는다. 순서 변경·누락·중복·잘못된 데이터는 정상값으로 대체하지 않는다.
- `evidence_summary.py`: LLM에 전달할 필드와 크기를 제한한다. 원본 응답의 증거와 모델 입력을 구분한다.
- `workflow.py`: 수집 → 판정 → 설명 흐름. 모델 실패 후에도 코드 판정과 원본 증거를 반환한다.

수집기 교체는 신뢰하는 Python 애플리케이션 코드에서 `create_app(..., provider=...)`로 한다.
공급자는 `plan(scenario)`와 `read(call, scenario)`를 구현한다. 요청 본문으로 공급자·프로필·명령을 선택할 수 없다.
현재 계약은 가상 데이터 전용이다. 실제 공급자 추가 시 `source`·`mode`·외부 전송 정책을 함께 확장해야 한다.

## 2. 제품·벤치 프로필

`.env`의 `DIAGNOSTIC_PROFILE`은 기본 `product`이며 Compose에서 컨테이너로 전달한다.

| 프로필 | 허용하는 읽기 도구 | SWD 접근 불가 처리 |
| --- | --- | --- |
| `product` | CAN 상태·집계·식별·DTC·부팅/크래시 기록, 심벌 분석 계약 | SWD 공급자 호출 전에 차단. `debug_access_unavailable`, `reason=profile_disabled` |
| `bench` | 위 목록과 `swd.read_fault`, `swd.read_sfr` | 공급자가 보고한 접근 불가를 기록하고 확보한 증거로 분석 계속 |

허용 목록은 정책 계약이며 해당 물리 도구가 구현됐다는 뜻이 아니다.
`bench`에서도 halt/reset/resume·플래싱·보호 해제는 허용하지 않는다. HIGH 도구와 승인 계층은 별도 구현 범위다.
기본 공급자의 SWD 실패도 모의 응답이며 실제 RDP 상태를 시험하지 않는다.

`GET /capabilities`에서 `diagnostic_profile`, `allowed_read_tools`, `physical_collection=false`, `control_tools=false`를 확인한다.
`/health`는 기존과 같은 프로세스 생존 확인이며 하드웨어 준비 상태를 보장하지 않는다.

## 3. 증거 계약

기존 `target`, `timestamp`, `status`, `source`, `data`, `error`를 유지하고 다음 메타데이터를 추가했다.

| 필드 | 의미 |
| --- | --- |
| `timestamp` | RPi가 증거를 수집한 시각. 시간대 필수. 설계의 `collected_at`에 해당 |
| `origin_ecu` | 증거가 나타내는 원본 ECU |
| `transport` | `direct`, `gateway_cache`, `can_record`, `bench_swd`. 가상 증거가 모사하는 경로이며 `source=fixture`와 함께 읽음 |
| `boot_id`, `running_build_id` | 조회 시점 실행 문맥. 게이트웨이 자체 문맥과 원본 ECU 문맥을 공급자가 구분해야 함 |
| `record_id`, `record_build_id` | 저장 기록 식별값과 고장 당시 빌드 |
| `capture_boot_id`, `capture_uptime_ms` | 사건 당시 부팅과 그 부팅 내 uptime. 다른 부팅의 uptime과 빼지 않음 |
| `schema_version` | 해석에 사용할 기록 형식 버전. 현재 `fixture-v1`은 예제용이며 CAN 바이트 규약이 아님 |
| `validity`, `field_validity` | 전체·필드별 유효성. 미확인을 정상으로 채우지 않음 |
| `freshness` | 조회한 현재 값의 최신 여부. 오래된 저장 사건을 현재 고장으로 해석하지 않음 |

`status`는 `ok`, `unavailable`, `error`, `unsupported`, `timeout`, `invalid`다.
새 오류는 `error.code`, 선택적 `reason`, `service_id`, `nrc`를 가진 구조다. 이전 문자열 오류도 읽지만 모델에는 전달하지 않는다.
증거 데이터는 유한한 JSON 값이어야 한다. 공급자가 반환한 모델도 재검증해 NaN·직렬화 불가 데이터가 최종 응답을 깨뜨리지 않도록 한다.

- `ok`이고 `data.record_present=false`: 조회 성공·저장 기록 없음. 전체 수명 동안 고장이 없었다는 뜻은 아니다.
- `unsupported`: 기능 미지원. 빈 정상 기록과 다르다.
- `timeout`: 조회 기한 초과. 원격 ECU 크래시·Bus-Off를 확정하지 않는다.
- `invalid`: 기록 손상이나 잘못된 형식. 현재 상태가 정상이라는 뜻은 아니다.
- 유효한 크래시 기록: record ID·기록 빌드·고장 boot ID·uptime이 있어야 확정한다. 프레임 유효성은 별도로 판정한다.

`analyze.symbols`는 분석 결과를 받을 경계만 구현했다. 예제는 심벌 없음·빌드 불일치를 반환한다.
분석 증거는 같은 대상의 `record_id`와 `record_build_id`에 연결해야 하며, 현재 실행 빌드로 대체하지 않는다.
실제 ELF 로딩·Build ID 검증·addr2line 실행 및 함수/줄 번호 반환은 후속 작업이다.

## 4. SWD 없이 확인하는 가상 시나리오

기존 `healthy`, `bms_timeout`, `bus_off`를 유지한다. 추가 시나리오는 다음과 같다.

| 시나리오 | 확인 내용 |
| --- | --- |
| `ecu_unresponsive` | 게이트웨이 조회 시간 초과. ECU 크래시 원인은 미확인 |
| `ecu_rebooted` | 두 부팅 식별값 변경. 특정 함수 고장이나 리셋 원인은 미확인 |
| `crash_recorded` | 현재 값은 정상이어도 과거 저장 크래시를 별도로 보고 |
| `crash_record_absent` | 성공한 조회에 기록 없음 |
| `crash_record_unsupported` | 기록 서비스 미지원 |
| `crash_record_timeout` | 기록 조회 시간 초과 |
| `crash_record_invalid` | 손상·유효하지 않은 기록 |
| `symbols_missing` | 기록은 있고 해당 빌드의 심벌 분석 결과가 없음 |
| `symbol_build_mismatch` | 기록 빌드와 심벌 빌드 불일치 모의 |
| `debug_access_unavailable` | 제품에서는 실행 전 정책 차단, 벤치에서는 프로브 없음 모의 |

컨테이너를 새 이미지로 재생성한 뒤 모델 없이 확인한다.

```bash
docker compose up -d --build agent
curl -sS http://127.0.0.1:8000/capabilities
curl -sS http://127.0.0.1:8000/diagnose \
  -H 'Content-Type: application/json' \
  --data-binary @examples/crash_recorded.json
python scripts/smoke_test.py
```

기존 모델 선택 규칙은 유지한다. 저장 크래시의 `error` finding도 외부 활성 상태의 `auto`에서는 Pamout 선택 대상이다.
단순 수집 실패·SWD 접근 불가만으로 코드가 크래시를 확정하거나 보호 해제를 시도하지 않는다.

## 5. LLM과 운영 범위

모델에는 판정, 출처·유효성·기록 문맥, 도구별 허용 필드와 구조화된 오류를 전달한다.
임의 로그·스택 덤프·ELF 경로와 자유 형식 오류는 제외한다. 입력이 6000 UTF-8 byte를 넘으면 모델 호출 전에 거절한다.
이 값은 크기 보호 장치이며 기존 2048 토큰 컨텍스트에 들어간다는 보장은 아니다. 실제 모델의 토큰·설명 품질은 추가 검증한다.
보호 해제·정지·리셋·플래싱을 제안하지 않도록 프롬프트도 보완했으나 문장 내용의 자동 정확성 검증은 아니다.

한 수집 계획은 최대 16건이며 중복 도구·대상은 실행 전에 거절한다. 수집기는 각 I/O의 시간 제한을 직접 구현해야 한다.
현재 동기식 호출 계층은 무한 대기하는 공급자 함수를 강제 중단하거나 전체 벽시계 기한을 보장하지 않는다.
호스트 수집 서비스는 요청률·전체 기한·인증·대상 허용 목록을 별도로 구현해야 한다.

컨테이너의 일반 사용자·읽기 전용 루트·기존 메모리 제한은 유지한다.
원본 증거의 영구 저장, Build ID별 ELF 보관, 실제 CAN·UDS 호스트 API, 승인된 복구 기능은 아직 미구현이다.
새 코드의 PC 테스트와 과거 배포본의 RPi 검증을 구분한다. 이 변경만으로 운영 RPi가 자동 갱신되지 않는다.

## 6. 이번 변경의 검증 결과

- PC Python 자동 테스트: **112 passed in 6.36s**. 기존 모델 선택·오류 처리와 신규 증거·정책 테스트 포함.
- Ruff 검사·포맷 검사, Compose 설정 검사, 변경 문서의 Markdown 검사 통과.
- PC `linux/amd64` 실행 이미지 빌드 후 **13개 시나리오 smoke test 통과**.
- 실행 컨테이너에서 UID 10001·읽기 전용 루트·805306368 byte 메모리 제한과 `/capabilities`를 확인했다.
- 제품 프로필에서 SWD 오류의 `reason=profile_disabled` 확인. 공급자 실행 전 차단은 spy 기반 자동 테스트로도 검증했다.
- 테스트 컨테이너에는 실제 키를 전달하지 않았고 실제 Qwen·Pamout 요청도 수행하지 않았다. 종료 후 제거했다.
- 원본 점검 메타데이터: `data/swd-optional-pc-verification-20261111.json`(Git 무시 대상).
- 검증 이미지: `sha256:9da98ea319af78481e60c17b926f4a5dc32d9898f900798edb121df341d10daf`.

위 목록은 PC 검증 당시의 결과다. 이후 커밋 `9682de3`를 RPi에 배포해 ARM64 테스트 112개와 진단 13종을 통과했다.
실제 모델에서 출력 거절·Qwen 설명 오류와 87°C 스로틀링도 확인했다.
범위별 결과는 [SWD 선택 진단 실기기 검증](RPi5_SWD_선택_진단_실기기_검증.md)을 참조한다.
