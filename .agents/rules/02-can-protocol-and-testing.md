# CAN 통신 규약 및 테스트 불변식

1. **기본값 및 하위 호환성 유지**
   * 기본 런타임 설정은 `EVIDENCE_PROVIDER=fixture` (`fixture-bench-v1`, 13종 가상 케이스)로 유지하고 `POST /verify` 회귀 계약을 깨뜨리지 않습니다.
   * `http_collector`의 기본 `LIVE_SPECIFICATION`은 `hw_test_f103` (`hw-test-f103-v1`, `0x100` DLC 4 + UART 차분)으로 유지하고, Phase 2는 `LIVE_SPECIFICATION=aide_ecu` (`aide-ecu-v1`)로 opt-in 선택합니다.

2. **Phase 2 (`aide-ecu-v1`) 수동 주기 프레임 기준**
   * **`A_FAN_CMD` (`0x080`, 100 ms, 타임아웃 300 ms)**: `B0` `duty_target`(`uint8`, `0~100%`), `B7` `alive_cnt`(`uint8`). `DLC != 8` 또는 `duty_target > 100`은 무효.
   * **`A_THERMAL_STATUS` (`0x180`, 100 ms, stale 500 ms)**: `B0~1` `temp_ambient`(`sint16 LE`, `0.1 °C`, 무효 `0x7FFF`), `B2` `duty_target`, `B3~4` `vib_rms`(`uint16 LE`, `1 mg`, 무효 `0xFFFF`), `B5[3:0]` `a_state`(`0..3`), `B5[7:4]` `thermal_state`(`0..3`), `B6` `fault_flags`(비트 `n-1` = 고장 코드 `n`, `0x01~0x07`), `B7` `alive_cnt`.
   * **`B_FAN_STATUS` (`0x1C0`, 100 ms, stale 500 ms)**: `B0~1` `fan_rpm`(`uint16 LE`), `B2` `duty_actual`(`0~100%`), `B3` `fan_state`(`0..4`), `B4` `fault_code`, `B7` `alive_cnt`. `AIDE_B_FAULT_ENCODING`으로 `code` / `bitmask` 모두 지원.
   * **`alive_cnt` 누락 vs 정지 분리**: 값 건너뜀은 `sequence_gap_count`(`warning`), 연속 동일 값 반복은 `consecutive_frozen_count`(기준 이상 시 `*_ALIVE_FROZEN` `error`)로 분리합니다.
   * **로컬 Bus-Off 보호**: 로컬 `can0`이 `ERROR-ACTIVE`/`UP`이 아니면 `REMOTE_STATE_UNKNOWN`을 기록하고 원격 ECU 미수신 오탐을 차단합니다.

3. **커밋 전 필수 검증**
   * 코드/테스트 수정 후 반드시 `uv run --frozen ruff check .`, `uv run --frozen ruff format --check .`, `uv run --frozen pytest -q`를 모두 통과해야 합니다.
   * 마크다운 문서 수정 시 GFM 취소선 방지를 위해 본문 물결표(`~`)는 백슬래시와 함께(\~) 이스케이프하고, 백틱(`...`) 내부는 원형(`~`) 그대로 둡니다.
