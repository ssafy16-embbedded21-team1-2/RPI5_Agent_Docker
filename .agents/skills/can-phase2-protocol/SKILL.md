---
name: can-phase2-protocol
description: >-
  Use when updating CAN frame codecs, LiveAideEcuSpecification, HttpHardwareProvider,
  deterministic fault rules, or evidence_summary allowlists for Board A (PJT-19/PJT-20)
  and Board B (PJT-15/PJT-16) in RPI5_Agent_Docker.
---

# Phase 2 CAN Protocol Integration Runbook

보드 A·B 본 규약 디코더·명세·판정 로직을 확장하거나 수정할 때 참조하는 절차입니다.

## Code Touchpoints

1. **`src/rpi5_agent/specification.py`**: `LiveAideEcuSpecification` (`aide-ecu-v1`) ID·타임아웃·임계치 정의.
2. **`src/rpi5_agent/hw_collector.py`**: 프레임 파서(`parse_a_fan_cmd_frame`, `parse_a_thermal_status_frame`, `parse_b_fan_status_frame`) 및 `CanCollectorBuffer` 멀티 스트림 누적.
3. **`src/rpi5_agent/config.py` / `.env.example` / `compose.yaml`**: `LIVE_SPECIFICATION`, `AIDE_B_FAULT_ENCODING`, `AIDE_INCLUDE_UART` 환경변수 및 `build_live_specification()`.
4. **`src/rpi5_agent/tools/live_provider.py`**: `HttpHardwareProvider`의 `plan()`, `_build_capture_result()`, `field_validity` 매핑.
5. **`src/rpi5_agent/assessment.py`**: `LiveThermalStatusData` 등에서 무효값이 가능한 필드는 `= None` 기본값 유지, `evaluate_live_aide_ecu()` 및 3-노드 교차 판정 구현.
6. **`src/rpi5_agent/evidence_summary.py`**: `DATA_FIELDS["can.capture"]` 스칼라 화이트리스트 추가 및 6,000B 페이로드 상한 검증.

## Reference Test Frames (`PJT-20`)

* `A_FAN_CMD` (`0x080`):
  * 정상 (`42 °C` → 듀티 `58%`, `alive_cnt=7`): `3A 00 00 00 00 00 00 07`
  * FAILSAFE (`100%`, `alive_cnt=8`): `64 00 00 00 00 00 00 08`
* `A_THERMAL_STATUS` (`0x180`):
  * 정상 (`42.0 °C`, `58%`, `120 mg`, `NORMAL·정상`, 고장 없음): `A4 01 3A 78 00 01 00 07`
  * FAILSAFE (`0x7FFF` 무효, `100%`, `120 mg`, `FAILSAFE·불명`, `TEMP_SENSOR`): `FF 7F 64 78 00 32 01 08`
* `B_FAN_STATUS` (`0x1C0`):
  * 정상 (`1500 rpm`, `60%`, `RUN`, `NONE`): `DC 05 3C 02 00 00 00 07`

## Team-Consensus Gates (`TBD`)

* 아래 항목은 팀 합의가 완료되기 전까지 임의 기본값을 하드코딩하지 않고 보류하거나 설정으로 분리합니다.
  * 능동 진단 요청·응답 (`0x380`/`0x300` UDS+ISO-TP vs 8B 단일 프레임 `DIAG_REQ`/`B_DIAG_RESP`)
  * 패딩 바이트 (`0x00` vs `0xCC`), DID 번호 배정 및 데이터 엔디안, `F-10` 고장 주입 명령 형식
  * 보드 B 최종 고장 형식(단일 코드 vs 비트 플래그), `NO_ROTATION`/`RPM_DEVIATION` 임계치

## Validation

* `uv run --frozen pytest tests/test_live_phase1.py tests/test_live_phase2_aide.py -q`
* `uv run --frozen ruff check . && uv run --frozen ruff format --check .`
