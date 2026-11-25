# RPI5_Agent_Docker 실측 CAN · UART 연동 설계서 (v0.2)

작성일: 2026-11-17 KST (2026-11-25 `PJT-19`·`PJT-20` 보드 A 명세 및 Phase 2 수동 주기 코덱 반영). 상태: **Phase 1 실측 파일럿(`hw-test-f103-v1`) 완료 + Phase 2 수동 주기 CAN 디코더·판정(`aide-ecu-v1`: `0x080`/`0x180`/`0x1C0`) 구현 및 PC 테스트 완료 (`DIAG_REQ`/UDS·DID 능동 진단은 팀 협의 대기)**.

**주의: 기본 런타임 설정은 `EVIDENCE_PROVIDER=fixture`(`mode="fixture"`, `physical_collection=false`)로 유지됩니다.**
Phase 1(`LIVE_SPECIFICATION=hw_test_f103`)과 Phase 2 보드 A·B 수동 주기 수신(`LIVE_SPECIFICATION=aide_ecu`)은 `EVIDENCE_PROVIDER=http_collector`로 활성화합니다. 현재 `pi32` 실기기는 `can0`·STM32 물리 연결이 분리된 상태이므로, 2-보드 실기기 E2E 검증은 보드 A·B 통합 연결 후 수행합니다. 기존 `fixture-bench-v1` 가상 검증 13종은 그대로 보존됩니다.

## 0. 선행 실기기 검증 근거 (2026-11-17, `PJT-17` 완료)

기존 RPi 5 본체의 RP1 GPIO/SPI 출력단 하드웨어 불량을 교차 시험으로 분리한 뒤, 교체 RPi 5 보드에서 실측 연동에 필요한 물리 경로를 검증했습니다.

| 항목                   | 확인된 실기기 상태                                                                                                                                                           | 원격 근거 (`pi32`)                                                       |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| **호스트 보드 · 커널** | `pi32` (`serial: 6f1e588610c80344`), `6.18.62-v8-16k+`, aarch64                                                                                                              | `/var/backups/rpi5-can/recheck-20261117/board-swap-verification.json`    |
| **냉각 · 스로틀링**    | 액티브 쿨러 `pwmfan` 자동 제어, 최고 53.45°C / 2,768 RPM, `throttled=0x0`                                                                                                    | `cooler-verification.json`                                               |
| **CAN 어댑터**         | Seengreat RS485 Dual CAN I (`can0`=`spi0.1` IRQ 25, `can1`=`spi1.1` IRQ 24), 500 kbit/s `ERROR-ACTIVE`                                                                       | `board-swap-verification.json`                                           |
| **STM32 CAN 양방향**   | NUCLEO-F103RB(`066CFF535551836687112626`) `0x100` DLC 4(`A5 5A 00 <cnt>`, 100ms) 99프레임 무손실 수신 + `cansend can0 123#11223344` → STM32 `rx=1 last=0x123 dlc=4`          | `can-first-rx.log`, `can0-stm32-rx.json`, `can0-bidirectional-test.json` |
| **보조 UART**          | `/dev/serial/by-id/usb-STMicroelectronics_STM32_STLink_066CFF535551836687112626-if02` (`115200 8N1`), `t=... TEC=... txreq=... txok=... errcb=... rx=...` 1초 주기 출력 확인 | `stm32-uart-rx.json`                                                     |

---

## 1. 아키텍처 전환 배경: 가상 게이트웨이 벤치 vs 실물 직결 버스

초기 `RPI5_Agent_Docker` 가상 벤치(`fixture-bench-v1`)와 2026-11-16 확정된 실제 임베디드 토폴로지(`PJT-15`, `PJT-16`, `보드 B CAN 메시지 명세서 (초안)`) 사이에는 구조적 차이가 있습니다.

| 구분               | 기존 가상 벤치 (`fixture-bench-v1`)                         | 실제 프로젝트 토폴로지 (2026-11-16 확정 반영)                                        |
| ------------------ | ----------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| **버스 구성**      | G474RE 게이트웨이(`cell1`) + 하위 BMS(`cell2`)              | **게이트웨이 없음(제외)**. 보드 A(열관리) + 보드 B(냉각팬 ECU) + RPi 5(`can0`) 직결  |
| **MCU · 트랜시버** | G474RE + F103RB 가상 모델                                   | 두 보드 모두 **NUCLEO-F103RB** + **SN65HVD230**, Classic CAN 단일 버스               |
| **수집 대상 증거** | `can.bus_state("rpi_adapter")`, `gateway.snapshot("cell1")` | `can.bus_state("can0")`, `can.capture("can0")`, `serial.read_can_diagnostics(<ecu>)` |
| **판정 기준**      | `gateway_timeout_ms=500`, `bms_timeout_ms=1000`             | Phase 1: `hw_test` F103(`0x100`) / Phase 2: 보드 A·B 명세 및 임계치(미결 `TBD`)      |

### 설계 원칙

1. **기존 13종 가상 회귀 보존**: `FIXTURE_SPECIFICATION`(`fixture-bench-v1`), `FixtureProvider`, `CASE_CATALOG_ID`(`fixture-acceptance-v1`) 및 `POST /verify`의 기존 동작과 145개 자동 테스트는 그대로 유지합니다.
2. **명세-수집-판정 분리**: 실측용 `AssessmentSpecification`과 `EvidenceProvider`를 별도로 추가하고, 미결(`TBD`)인 CAN ID·임계값은 판정 함수에 하드코딩하지 않고 설정/명세 객체로 주입합니다.
3. **LLM 역할 제한 유지**: `can0` 원시 프레임 파싱, 시계/경과시간 계산, 구간 차분(Delta), 상태 판정은 결정적 코드가 수행하며, 모델에는 허용된 스칼라 요약과 findings만 전달합니다.

---

## 2. 호스트 ↔ Docker 컨테이너 수집 경계 설계

`compose.yaml`의 `agent` 컨테이너는 `read_only: true`, `cap_drop: ALL`, `no-new-privileges: true`, 일반 사용자(`10001:10001`), bridge 네트워크로 격리되어 있습니다. `can0`는 `/dev/can0` 캐릭터 디바이스가 아닌 **리눅스 네트워크 인터페이스(`AF_CAN`)**이므로 단순 볼륨 마운트만으로 컨테이너에 노출되지 않습니다.

```text
┌─────────────────────────────────────────────────────────────────────────┐
│ RPi 5 호스트 (pi32)                                                     │
│                                                                         │
│  [STM32 / CAN Bus] ──► can0 (SocketCAN, 500kbit/s) ──┐                  │
│  [ST-LINK VCP]     ──► /dev/serial/by-id/... (115200)┼─► rpi5-hw-collector
│                                                      │   (systemd, 8090) │
│  Docker Bridge (host.docker.internal:8090) ◄─────────┘                  │
│     │                                                                   │
│     ▼ (HTTP GET Read-Only JSON Snapshot)                                │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │ rpi5-agent-agent-1 (read_only, cap_drop=ALL, UID 10001)           │  │
│  │  HttpHardwareProvider ─► collect_evidence ─► assess ─► LLM 요약    │  │
│  └───────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
```

### 방식 비교 및 채택안

| 항목                      | 방식 A (채택 권장): 호스트 상시 수집 데몬 + 로컬 HTTP 브리지                          | 방식 B (대안): `network_mode: host` + 시리얼 장치 마운트                              |
| ------------------------- | ------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| **컨테이너 보안 격리**    | `read_only`, `cap_drop: ALL`, bridge 네트워크 격리 그대로 유지                        | 호스트 네트워크 네임스페이스 공유, `/dev/ttyACM*`·`dialout` GID 권한 노출             |
| **연속 관측 · 구간 차분** | 호스트 데몬이 백그라운드에서 CAN 수신 시각·Sequence 누락·UART 2-표본 차분을 상시 누적 | 요청 시점에만 포트를 열면 ST-LINK VCP 오픈 시 80B 잔여 버퍼 혼입, 이전 표본 비교 불가 |
| **호스트 Qwen 연동**      | 기존 `host.docker.internal:8080` 그대로 유지                                          | `127.0.0.1:8080`으로 변경 필요, 포트 바인딩 격리 해제                                 |
| **실패 격리**             | 수집 데몬 미기동 시 Agent는 `collector_unreachable` 증거를 기록하고 정상 응답         | 컨테이너 내 소켓/시리얼 블록 시 FastAPI 이벤트 루프·슬롯 점유 위험                    |

### 호스트 수집 데몬(`rpi5-hw-collector.service`) 읽기 전용 API 계약 (제안)

호스트 데몬은 Docker bridge 게이트웨이 또는 loopback에만 바인딩하며, 초기 단계에서는 **상태 변경·CAN 송신(`cansend`)·SWD 제어 엔드포인트를 제공하지 않는 순수 읽기 전용**으로 둡니다.

1. **`GET /health`**
   - 데몬 생존 여부, `can0` 인터페이스 존재 여부, 연결된 시리얼 장치 경로 목록 반환.
2. **`GET /snapshot/can?interface=can0&window_ms=5000`**
   - `ip -j -details -statistics link show <interface>` 기반 어댑터 상태(`UP`/`DOWN`, `ERROR-ACTIVE`/`ERROR-PASSIVE`/`BUS-OFF`, `bitrate`, `tx_packets`, `rx_packets`, `bus_errors`, `rx_dropped`).
   - 슬라이딩 윈도우 내 CAN ID별 집계(`last_rx_utc`, `age_ms`, `frame_count`, 최근 디코딩 필드, `sequence_gap_count`).
3. **`GET /snapshot/uart?target=hw_test_f103`**
   - `/dev/serial/by-id/` 고정 심볼릭 링크로 매핑된 보드의 최근 2개 스냅샷(`prev`, `curr`)과 **동일 부팅(`t_ms` 단조 증가) 기반 구간 차분(`delta`)** 반환.
   - 직렬 포트 미연결·미수신 시 `status="unavailable"` 또는 `"timeout"` 반환(정상 `0`으로 대체 금지).

#### `GET /snapshot/can` 응답 구조 예시 (Phase 1 기준 형식)

```json
{
  "collected_at": "2026-11-17T05:10:00.120000+00:00",
  "interface": "can0",
  "adapter": {
    "operstate": "UP",
    "can_state": "ERROR-ACTIVE",
    "bitrate_bps": 500000,
    "rx_packets": 2995,
    "tx_packets": 1,
    "rx_errors": 0,
    "tx_errors": 0,
    "bus_errors": 0,
    "rx_dropped": 1,
    "rx_missed": 0
  },
  "window_ms": 5000,
  "streams": {
    "0x100": {
      "dlc": 4,
      "frame_count_in_window": 50,
      "last_rx_utc": "2026-11-17T05:10:00.090000+00:00",
      "age_ms": 30.0,
      "last_alive_counter": 240,
      "sequence_gap_count": 0,
      "decoded": {
        "magic_ok": true,
        "alive_counter": 240
      }
    }
  }
}
```

#### `GET /snapshot/uart` 응답 구조 예시 (Phase 1 기준 형식)

```json
{
  "collected_at": "2026-11-17T05:10:00.125000+00:00",
  "target": "hw_test_f103",
  "device_by_id": "/dev/serial/by-id/usb-STMicroelectronics_STM32_STLink_066CFF535551836687112626-if02",
  "status": "ok",
  "sample_age_ms": 116.0,
  "latest": {
    "t_ms": 407009,
    "tec": 0,
    "rec": 0,
    "lec": 0,
    "ewg": 0,
    "epv": 0,
    "boff": 0,
    "txreq": 4071,
    "txok": 2984,
    "txabort": 0,
    "txbusy": 0,
    "errcb": 1086,
    "errcode": "0x00001000",
    "rx": 0,
    "last_rx_id": "0x000",
    "last_rx_dlc": 0
  },
  "delta": {
    "interval_ms": 3000,
    "epoch_continuous": true,
    "txreq_delta": 30,
    "txok_delta": 30,
    "txabort_delta": 0,
    "txbusy_delta": 0,
    "errcb_delta": 0,
    "rx_delta": 0
  }
}
```

---

## 3. 단계별 실측 연동 로드맵

### Phase 1: 현재 실물(`hw_test CAN normal v1`, F103 `0x100` + `/dev/ttyACM0`) 파일럿 연동

보드 A/B 최종 ID 합의 전이라도, 현재 책상 위에서 양방향 동작이 검증된 `STM32F103RB` 1대를 대상으로 실측 파이프라인을 우선 검증할 수 있습니다.

1. **CAN 프레임 파서 (`0x100`, 100ms 주기)**
   - 기대 형식: 표준 ID `0x100`, `DLC == 4`, `data[0:2] == [0xA5, 0x5A]`, `data[2] == 0x00`, `data[3] == alive_counter` (`uint8`, 매 송신 시 1 증가 후 255 다음 0 wrap).
   - 유효성 검사: 길이 4B와 고정 매직 헤더(`A5 5A 00`) 불일치 시 `validity="invalid"`로 기록하고 Sequence 갱신으로 취급하지 않음.
2. **텍스트 UART 파서 (`115200 8N1`)**
   - `hw_test CAN normal v1`(`main.c`) 출력 라인 정규식 파싱:
     `t=(\d+) ms TEC=(\d+) REC=(\d+) LEC=(\d+) EWG=(\d+) EPV=(\d+) BOFF=(\d+) \| txreq=(\d+) txok=(\d+) txabort=(\d+) txbusy=(\d+) errcb=(\d+) errcode=(0x[0-9A-Fa-f]+) \| rx=(\d+) last=(0x[0-9A-Fa-f]+) dlc=(\d+)`
   - 초기 포트 오픈 시 유입되는 불완전 첫 줄/비ASCII 잔여 바이트는 버리고 완전한 개행(`\r\n`) 이후 행만 채택.
3. **핵심 판정 규칙 — 누적 오류와 구간 차분(Delta) 분리**
   - 오늘 실측(`/dev/ttyACM0`)에서 `can0`이 올라오기 전 구간 동안 누적된 `errcb=1086` (`errcode=0x00001000`)이 `can0` 정상 수신 중에도 그대로 출력됨을 확인했습니다.
   - 따라서 **`errcb > 0` 단독으로 현재 고장을 판정하지 않고**, 최근 구간 차분에서 `errcb_delta == 0`, `txreq_delta == txok_delta > 0`, `TEC == 0`, `REC == 0`이면 `UART_PAST_ERROR_INACTIVE`(과거 오류 이력 존재, 현재 통신 정상)로 구분합니다.
   - 반대로 RPi `can0`가 `ERROR-ACTIVE`인데 `0x100` 수신이 없고(`CAN_TELEMETRY_MISSING`), UART상 `txreq_delta > 0`, `txok_delta == 0`, `errcb_delta > 0`이면 송신측 ACK 부재·물리 배선 단선 후보(`UART_TX_ACTIVE_NO_CAN_RX`)로 분리합니다.

### Phase 2: 보드 A(열관리 제어기) · 보드 B(스마트 냉각팬 ECU) 본 규약 연동

`PJT-19`(`보드A_기능명세서.pdf`, 2026-11-18 초안 r1) 및 `PJT-20`(`board_a_can_packet.pdf`, 2026-11-24 완료)에 정의된 **수동 주기 수신 프레임 3종과 상태·고장 규약**을 [`aide-ecu-v1`](../src/rpi5_agent/specification.py) 명세로 구현했습니다. 능동 진단 프레임(`DIAG_REQ` / `A_DIAG_RESP` / `B_DIAG_RESP`)만 팀 협의 확정 후 추가합니다.

#### 1) 보드 A 하드웨어·제어·상태·고장 명세 (`PJT-19` 반영)

- **센서·주변장치 구성 (`aide-ecu/board_a/board_a.ioc` 핀맵 일치)**:
  - 주변 온도 `BMP280` (주소 `0x76`, `chip id 0x58`) + 팬 진동 `MPU6050` 호환 칩 (주소 `0x68`, `WHO_AM_I 0x72`, 가속도 RMS만 사용) — `I2C1` 리맵 `PB8(SCL)`/`PB9(SDA)` 공유, SDA 분리 시 F103 I2C 잠김 복구 절차 포함(실측 `3.1 s`).
  - `bxCAN`: `PA11(RX)`/`PA12(TX)` (`500 kbps`, `Prescaler=4, BS1=15TQ, BS2=2TQ`, 샘플 포인트 `88.9%`), `USART2`: `PA2/PA3` (`115200 bps` VCP), 상태 LED `LD2(PA5)`.
- **온도별 목표 듀티 표 (예시) 및 제어/감시 상태 분리**:
  - `35 °C` 미만 `0%`(정상) / `35~45 °C` `30→70%`(정상) / `45~55 °C` `70→100%`(경고) / `55 °C` 이상 `100%`(과온 — 고장이 아닌 열관리 상태로만 보고).
  - 제어 모드(`a_state`: `0=INIT`, `1=NORMAL`, `2=FAILSAFE` 온도 불명 시 팬 `100%` 명령, `3=TEST`)와 냉각팬 감시(`OK`, `COMM_LOST`, `RESP_FAULT`)를 독립 판정합니다.

#### 2) CAN 주기 메시지 디코더 (`PJT-20` 반영, Standard 11-bit, DLC 8, Little Endian)

- **`A_FAN_CMD` (`0x080`, A → B, 100 ms, 수신 타임아웃 300 ms)**:
  - `B0`: `duty_target` (`uint8`, `0~100%`, `>100` 또는 `DLC!=8` 무효 처리), `B1~6`: 예약(`0`), `B7`: `alive_cnt` (`uint8`).
- **`A_THERMAL_STATUS` (`0x180`, A → B·RPi, 100 ms, stale 기준 500 ms)**:
  - `B0~1`: `temp_ambient` (`sint16 LE`, `0.1 °C`, 무효 `0x7FFF`), `B2`: `duty_target` (`uint8`, `0~100%`), `B3~4`: `vib_rms` (`uint16 LE`, `1 mg`, 무효 `0xFFFF`), `B5[3:0]`: `a_state` (`0~3`), `B5[7:4]`: `thermal_state` (`0=정상`, `1=경고`, `2=과온`, `3=불명`), `B6`: `fault_flags` (비트 `n-1` = 고장 코드 `n`: `0x01 TEMP_SENSOR`, `0x02 VIB_SENSOR`, `0x03 FAN_STATUS_TIMEOUT`, `0x04 FAN_FAULT_REPORTED`, `0x05 FAN_DUTY_MISMATCH`, 후보 `0x06 CAN_BUS_OFF`, `0x07 TASK_OVERRUN`), `B7`: `alive_cnt`.
- **`B_FAN_STATUS` (`0x1C0`, B → A·RPi, 100 ms, stale 기준 500 ms)**:
  - `B0~1`: `fan_rpm` (`uint16 LE`), `B2`: `duty_actual` (`uint8`, `0~100%`), `B3`: `fan_state` (`0=INIT`, `1=STOP`, `2=RUN`, `3=FAULT`, `4=TEST`), `B4`: `fault_code` (`0x00=NONE`, `0x01=NO_ROTATION`, `0x02=RPM_DEVIATION`, `0x03=CMD_TIMEOUT`; `AIDE_B_FAULT_ENCODING=code|bitmask` 설정으로 양쪽 형식 대응), `B7`: `alive_cnt`.
- **`alive_cnt` 누락 vs 정지 분리 (`hw_collector.py`)**:
  - 값이 건너뛰면 `sequence_gap_count`를 누적(`warning`)하고, 연속 동일 값 반복은 `consecutive_frozen_count`로 별도 집계해 기준(`3회`) 이상 시 `<STREAM>_ALIVE_FROZEN` (`error`, 송신 ECU 정지 의심)으로 판정합니다.
- **3-노드 버스 교차 판정 (`evaluate_live_aide_ecu`)**:
  - 로컬 어댑터가 `ERROR-ACTIVE`·`UP`이 아니면 `REMOTE_STATE_UNKNOWN`을 기록하고 원격 미수신 오탐을 차단합니다.
  - 보드 B `CMD_TIMEOUT` 수신 시 RPi의 `A_FAN_CMD` 정상 수신 여부에 따라 `CROSS_BOARD_A_CMD_ABSENT_CONFIRMED` vs `CROSS_BOARD_B_RX_PATH_SUSPECTED`로, 보드 A `FAN_STATUS_TIMEOUT` 수신 시 RPi의 `B_FAN_STATUS` 수신 여부에 따라 `CROSS_BOARD_B_STATUS_ABSENT_CONFIRMED` vs `CROSS_BOARD_A_RX_PATH_SUSPECTED`로 경로를 분리합니다.
  - `a_state == 3` / `fan_state == 4` 수신 시 시험 모드(`BOARD_A_TEST_MODE_ACTIVE`, `FAN_TEST_MODE_ACTIVE`, `info`)로 분리하며, 모든 Finding이 `info`인 정상 운전 시 `RoutedSummarizer(auto)`는 0토큰 `deterministic_template`을 반환합니다.

---

## 4. `RPI5_Agent_Docker` 모듈별 확장 설계

기존 [`src/rpi5_agent/`](../src/rpi5_agent) 구조를 유지하면서 아래 경계만 확장합니다.

| 파일                                                                              | 현재 상태                                                              | 실측 연동 시 변경 설계                                                                                                                                                                                                                                                                                             |
| --------------------------------------------------------------------------------- | ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| [`models.py`](../src/rpi5_agent/models.py)                                        | `source="fixture"`, `mode="fixture"`, 고정 13종 `Scenario`             | • `ToolResult.source`: `"fixture" \| "live_can" \| "live_uart" \| "manual_measurement"`<br>• `ToolResult.transport`: 기존 + `"socketcan" \| "uart_text" \| "uart_json"`<br>• `Diagnosis.mode`: `"fixture" \| "live"`<br>• 실측용 `DiagnoseRequest`에 대상 프로파일/모드 구분(또는 별도 `/diagnose/live` 분리 검토) |
| [`tools/collection.py`](../src/rpi5_agent/tools/collection.py)                    | `PRODUCT_READ_TOOLS`에 `can.*` 6종 + `analyze.symbols`                 | • 실측 읽기 도구 추가: `can.capture`, `serial.read_can_diagnostics`, `serial.get_identity`<br>• `HttpHardwareProvider` 구현 (단일 타임아웃, 재시도 없음, 연결 실패 시 `collector_unreachable` / `serial_unavailable` 반환)                                                                                         |
| [`specification.py`](../src/rpi5_agent/specification.py) | `FIXTURE_SPECIFICATION` · `HW_TEST_SPECIFICATION` · `AIDE_ECU_SPECIFICATION` | • `LIVE_SPECIFICATION=aide_ecu` (`aide-ecu-v1`) 및 `AIDE_B_FAULT_ENCODING` 설정 지원 |
| [`assessment.py`](../src/rpi5_agent/assessment.py) | `evaluate_fixture`, `evaluate_live_hw_test`, `evaluate_live_aide_ecu` 분기 | • Phase 2 `0x080`/`0x180`/`0x1C0` 상태·고장·3-노드 교차 판정 구현 (아래 Finding 표 참고) |
| [`evidence_summary.py`](../src/rpi5_agent/evidence_summary.py) | `can.capture`·`serial.read_can_diagnostics` 허용 스칼라 화이트리스트 | • 보드 A·B 디코딩 스칼라 필드(`temp_degc`, `vib_rms_mg`, `a_state`, `fault_flags`, `fan_rpm`, `duty_actual`, `fan_state`, `fault_code`, `consecutive_frozen_count`) 및 6,000B 가드 유지 |
| [`config.py`](../src/rpi5_agent/config.py) & [`api.py`](../src/rpi5_agent/api.py) | `EVIDENCE_PROVIDER=fixture\|http_collector`, `LIVE_SPECIFICATION=hw_test_f103\|aide_ecu` | • `/health`, `/capabilities`, `/diagnose`가 선택된 Phase 1/2 명세 ID와 `mode="live"` 반환 (`/verify`는 409 유지) |

### 실측용 Finding 코드 초안

| Finding 코드                                              | Severity  | 판정 조건 (결정적 코드)                                                                    |
| --------------------------------------------------------- | --------- | ------------------------------------------------------------------------------------------ |
| `LOCAL_ADAPTER_ACTIVE`                                    | `info`    | `can0` 상태 `UP` 및 `ERROR-ACTIVE`, `bus_errors` 미증가                                    |
| `LOCAL_ADAPTER_ERROR_PASSIVE`                             | `warning` | `can0` 상태 `ERROR-PASSIVE` 또는 `rx_errors`/`tx_errors` 증가                              |
| `LOCAL_ADAPTER_BUS_OFF`                                   | `error`   | `can0` 상태 `BUS-OFF`                                                                      |
| `COLLECTOR_UNREACHABLE`                                   | `error`   | 호스트 수집 데몬 HTTP 연결 실패 또는 시간 초과                                             |
| `CAN_TELEMETRY_FRESH`                                     | `info`    | 대상 CAN ID가 `age_ms <= timeout_ms`이고 `magic/schema` 유효                               |
| `CAN_TELEMETRY_STALE`                                     | `warning` | 과거 수신 후 `age_ms > timeout_ms`                                                         |
| `CAN_TELEMETRY_MISSING`                                   | `error`   | 관측 윈도우 내 대상 CAN ID 0건 수신                                                        |
| `CAN_SEQUENCE_GAP`                                        | `warning` | `sequence_gap_count > 0` (부팅 변경 제외)                                                  |
| `UART_TX_ACTIVE_NO_CAN_RX`                                | `error`   | `CAN_TELEMETRY_MISSING`이면서 UART `txreq_delta > 0`, `txok_delta == 0`, `errcb_delta > 0` |
| `UART_TX_STALLED`                                         | `error`   | UART 응답은 정상이지만 `txreq_delta == 0`                                                  |
| `UART_PAST_ERROR_INACTIVE`                                | `info`    | `errcb > 0`이지만 `errcb_delta == 0` 및 `txok_delta > 0`, `TEC == 0`, `REC == 0`           |
| `FAN_FAULT_NO_ROTATION` / `RPM_DEVIATION` / `CMD_TIMEOUT` | `error`   | Phase 2 보드 B `fault_code` (`0x01`/`0x02`/`0x03`, 또는 bitmask) 수신 |
| `BOARD_A_FAILSAFE_ACTIVE` / `BOARD_A_FAULT_*`             | `error` / `warning` | Phase 2 보드 A `a_state=2` 또는 `fault_flags` (`0x01`\~`0x07`) 수신 |
| `THERMAL_STATE_WARNING` / `THERMAL_STATE_OVER_TEMP`       | `warning` | 보드 A `thermal_state` 경고(`45~55°C`)/과온(`55°C` 이상) — 센서 고장과 분리 |
| `<STREAM>_ALIVE_FROZEN`                                   | `error`   | `0x080`/`0x180`/`0x1C0` `alive_cnt` 3회 이상 연속 동일 값 정지 |
| `CROSS_BOARD_*_ABSENT_CONFIRMED` / `*_RX_PATH_SUSPECTED`  | `error`   | RPi 직접 CAN 수신 여부와 상대 보드의 타임아웃 보고(`0x03`/`0x04`) 교차 판정 |
| `BOARD_A_TEST_MODE_ACTIVE` / `FAN_TEST_MODE_ACTIVE`       | `info`    | Phase 2 `a_state == 3` 또는 `fan_state == 4 (TEST)` 수신 |

---

## 5. 임베디드 파트 협의 안건 및 보드 B `DIAG_REQ` 제안안

`보드 B(스마트 냉각팬 ECU) CAN 메시지 명세서 (초안)`에서 AI팀과 협의로 열어둔 항목에 대한 제안안입니다.

### 5-1. `DIAG_REQ` (`RPi → B`) / `B_DIAG_RESP` (`B → RPi`) 진단 항목 제안

`DIAG_REQ`는 Byte 0=`diag_item`, Byte 1\~7=`0x00` 예약으로 두고, `B_DIAG_RESP`는 Byte 0=`diag_item`, Byte 1=`result`(`0=정상`, `1=이상`, `2=지원 안 함`), Byte 2\~7=상세값(리틀 엔디안)으로 맞춥니다.

| `diag_item` | 이름               | `B_DIAG_RESP` Byte 2\~7 제안 배치                                                                                                                    | Agent 진단 활용 목적                                                  |
| ----------- | ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| `0x01`      | `IDENTITY_BOOT`    | B2\~3: `boot_counter` (`uint16`), B4: `reset_reason_flags` (`uint8`), B5\~6: `fw_build_tag` (`uint16`), B7: `capabilities` (`uint8`)                 | 재부팅 여부(와치독/전원 차단)와 Sequence 초기화 구분                  |
| `0x02`      | `CAN_HEALTH`       | B2: `tec` (`uint8`), B3: `rec` (`uint8`), B4: `lec_last` (`uint8`), B5: `tx_err_cnt_sat` (`uint8`), B6\~7: `last_cmd_age_10ms` (`uint16`, 10ms 단위) | UART 없이 CAN 버스 상에서 보드 B의 TEC/REC·명령 수신 경과시간 조회    |
| `0x03`      | `FAN_HW_SELFCHECK` | B2\~3: `target_rpm_or_duty` (`uint16`), B4\~5: `measured_rpm` (`uint16`), B6: `hall_pulse_ok` (`uint8`), B7: `driver_enable` (`uint8`)               | `NO_ROTATION`/`RPM_DEVIATION` 발생 시 구동 출력 vs 홀센서 피드백 분리 (`target_rpm_or_duty` 단위는 `PJT-20` `A_FAN_CMD` 목표 듀티 기준 통일 여부 협의) |

### 5-2. `PJT-16` · `PJT-19` · `PJT-20` 차기 회의 확정 체크리스트 (2026-11-25 갱신)

`PJT-20`에서 보드 A 제안 ID(`0x080`/`0x180`/`0x1C0`, `500 kbit/s`)와 주기 메시지 바이트 배치가 정의되어 AI 측 수동 디코더는 선반영 완료했습니다. 아래 항목만 팀 합의 후 능동 진단 계층에 추가 반영합니다.

1. **보드 B 주기 메시지 고장 형식 통일**: 보드 A(`fault_flags` 비트 플래그)와 보드 B(`fault_code` 단일 코드) 간 형식 통일 여부 및 `B_FAN_STATUS` stale(`500 ms`), 무회전/RPM 오차 임계치 확정 (`AIDE_B_FAULT_ENCODING`으로 양쪽 지원).
2. **진단 프로토콜 방식 및 패딩·엔디안 통일**: `PJT-16`(`10011`)의 8B 단일 프레임 `DIAG_REQ`/`B_DIAG_RESP`(`0x01~0x03`)와 `PJT-20`의 `0x380/0x300 (UDS + ISO-TP)` 중 최종 채택 방식, ISO-TP 패딩 바이트(`0x00`/`0xCC`), DID 데이터 엔디안(리틀 vs 빅) 확정.
3. **진단 항목(DID) 번호 배정 및 F-10 고장 주입 모드**: `보드A_기능명세서.pdf` 8·11절 기준 DID 목록·번호 배정(현재 온도/진동 RMS/요구 듀티·상태/팬 세기 표/히스테리시스/통신 카운터/버전)과 `a_state = TEST` 고장 주입 명령 형식 확정.
4. **보조 UART 유지 여부 및 저장소 동기화**: 양산형 보드 A/B 펌웨어의 `/dev/ttyACM*` 디버그 출력 유지 여부와 [`aide-ecu`](https://github.com/ssafy16-embbedded21-team1-2/aide-ecu) `common/` DBC/YAML 동기화 방식 결정.

---

## 6. 검증 계획 (구현 착수 시 완료 기준)

1. **PC 단위 · 통합 테스트 (`pytest`, `ruff`)**:
   - 기존 145개 `fixture` 테스트 100% 통과 유지.
   - `hw_test` 4B 프레임/`t=... TEC=...` 텍스트 UART 파서, 구간 차분(`errcb_delta == 0` 과거 오류 분리), `B_FAN_STATUS`/`B_FAN_FAULT` 리틀 엔디안 디코더, `HttpHardwareProvider` 타임아웃·500·비유한 JSON 방어 테스트 추가.
   - `evidence_summary.py`에서 원시 UART 로그/프레임 덤프가 LLM 입력에 포함되지 않음을 테스트로 검증.
2. **가상 CAN (`vcan0`) 사전 검증**:
   - 임베디드 보드 B 완성 전, RPi 또는 Linux 환경에서 `vcan0`에 보드 B 명세서 초안의 예제 프레임(`DC 05 3C 02 00 00 00 07` 등)과 모의 고장 프레임을 주입해 디코더/판정 검증.
3. **실기기(`pi32`) Phase 1 검증**:
   - 호스트 `rpi5-hw-collector.service` + 컨테이너 `rpi5-agent-agent-1` 연동 상태에서 `STM32F103RB`(`0x100` + `/dev/ttyACM0`) 연결 시 `/diagnose`가 `mode="live"`, `CAN_TELEMETRY_FRESH`, `UART_PAST_ERROR_INACTIVE`를 반환하고, STM32 CAN 선 분리 시 `CAN_TELEMETRY_MISSING` + `UART_TX_ACTIVE_NO_CAN_RX`로 정확히 분리되는지 확인.
