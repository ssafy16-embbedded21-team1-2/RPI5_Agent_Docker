# RPI5_Agent_Docker 실측 CAN · UART 연동 설계서 (v0.1)

작성일: 2026-11-17 KST. 상태: **구현 전 설계 (Design v0.1)**.

**주의: 현재 `RPI5_Agent_Docker` 런타임은 `mode="fixture"`, `physical_collection=false`입니다.**
본 문서 작성만으로 실측 수집기가 구현·배포·활성화된 것으로 간주하지 않으며, 기존 `fixture-bench-v1` 가상 검증과 구분합니다.

## 0. 선행 실기기 검증 근거 (2026-11-17, `PJT-17` 완료)

기존 RPi 5 본체의 RP1 GPIO/SPI 출력단 하드웨어 불량을 교차 시험으로 분리한 뒤, 교체 RPi 5 보드에서 실측 연동에 필요한 물리 경로를 검증했습니다.

| 항목 | 확인된 실기기 상태 | 원격 근거 (`pi32`) |
| --- | --- | --- |
| **호스트 보드 · 커널** | `pi32` (`serial: 6f1e588610c80344`), `6.18.62-v8-16k+`, aarch64 | `/var/backups/rpi5-can/recheck-20261117/board-swap-verification.json` |
| **냉각 · 스로틀링** | 액티브 쿨러 `pwmfan` 자동 제어, 최고 53.45°C / 2,768 RPM, `throttled=0x0` | `cooler-verification.json` |
| **CAN 어댑터** | Seengreat RS485 Dual CAN I (`can0`=`spi0.1` IRQ 25, `can1`=`spi1.1` IRQ 24), 500 kbit/s `ERROR-ACTIVE` | `board-swap-verification.json` |
| **STM32 CAN 양방향** | NUCLEO-F103RB(`066CFF535551836687112626`) `0x100` DLC 4(`A5 5A 00 <cnt>`, 100ms) 99프레임 무손실 수신 + `cansend can0 123#11223344` → STM32 `rx=1 last=0x123 dlc=4` | `can-first-rx.log`, `can0-stm32-rx.json`, `can0-bidirectional-test.json` |
| **보조 UART** | `/dev/serial/by-id/usb-STMicroelectronics_STM32_STLink_066CFF535551836687112626-if02` (`115200 8N1`), `t=... TEC=... txreq=... txok=... errcb=... rx=...` 1초 주기 출력 확인 | `stm32-uart-rx.json` |

---

## 1. 아키텍처 전환 배경: 가상 게이트웨이 벤치 vs 실물 직결 버스

초기 `RPI5_Agent_Docker` 가상 벤치(`fixture-bench-v1`)와 2026-11-16 확정된 실제 임베디드 토폴로지(`PJT-15`, `PJT-16`, `보드 B CAN 메시지 명세서 (초안)`) 사이에는 구조적 차이가 있습니다.

| 구분 | 기존 가상 벤치 (`fixture-bench-v1`) | 실제 프로젝트 토폴로지 (2026-11-16 확정 반영) |
| --- | --- | --- |
| **버스 구성** | G474RE 게이트웨이(`cell1`) + 하위 BMS(`cell2`) | **게이트웨이 없음(제외)**. 보드 A(열관리) + 보드 B(냉각팬 ECU) + RPi 5(`can0`) 직결 |
| **MCU · 트랜시버** | G474RE + F103RB 가상 모델 | 두 보드 모두 **NUCLEO-F103RB** + **SN65HVD230**, Classic CAN 단일 버스 |
| **수집 대상 증거** | `can.bus_state("rpi_adapter")`, `gateway.snapshot("cell1")` | `can.bus_state("can0")`, `can.capture("can0")`, `serial.read_can_diagnostics(<ecu>)` |
| **판정 기준** | `gateway_timeout_ms=500`, `bms_timeout_ms=1000` | Phase 1: `hw_test` F103(`0x100`) / Phase 2: 보드 A·B 명세 및 임계치(미결 `TBD`) |

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

| 항목 | 방식 A (채택 권장): 호스트 상시 수집 데몬 + 로컬 HTTP 브리지 | 방식 B (대안): `network_mode: host` + 시리얼 장치 마운트 |
| --- | --- | --- |
| **컨테이너 보안 격리** | `read_only`, `cap_drop: ALL`, bridge 네트워크 격리 그대로 유지 | 호스트 네트워크 네임스페이스 공유, `/dev/ttyACM*`·`dialout` GID 권한 노출 |
| **연속 관측 · 구간 차분** | 호스트 데몬이 백그라운드에서 CAN 수신 시각·Sequence 누락·UART 2-표본 차분을 상시 누적 | 요청 시점에만 포트를 열면 ST-LINK VCP 오픈 시 80B 잔여 버퍼 혼입, 이전 표본 비교 불가 |
| **호스트 Qwen 연동** | 기존 `host.docker.internal:8080` 그대로 유지 | `127.0.0.1:8080`으로 변경 필요, 포트 바인딩 격리 해제 |
| **실패 격리** | 수집 데몬 미기동 시 Agent는 `collector_unreachable` 증거를 기록하고 정상 응답 | 컨테이너 내 소켓/시리얼 블록 시 FastAPI 이벤트 루프·슬롯 점유 위험 |

### 호스트 수집 데몬(`rpi5-hw-collector.service`) 읽기 전용 API 계약 (제안)

호스트 데몬은 Docker bridge 게이트웨이 또는 loopback에만 바인딩하며, 초기 단계에서는 **상태 변경·CAN 송신(`cansend`)·SWD 제어 엔드포인트를 제공하지 않는 순수 읽기 전용**으로 둡니다.

1. **`GET /health`**
   * 데몬 생존 여부, `can0` 인터페이스 존재 여부, 연결된 시리얼 장치 경로 목록 반환.
2. **`GET /snapshot/can?interface=can0&window_ms=5000`**
   * `ip -j -details -statistics link show <interface>` 기반 어댑터 상태(`UP`/`DOWN`, `ERROR-ACTIVE`/`ERROR-PASSIVE`/`BUS-OFF`, `bitrate`, `tx_packets`, `rx_packets`, `bus_errors`, `rx_dropped`).
   * 슬라이딩 윈도우 내 CAN ID별 집계(`last_rx_utc`, `age_ms`, `frame_count`, 최근 디코딩 필드, `sequence_gap_count`).
3. **`GET /snapshot/uart?target=hw_test_f103`**
   * `/dev/serial/by-id/` 고정 심볼릭 링크로 매핑된 보드의 최근 2개 스냅샷(`prev`, `curr`)과 **동일 부팅(`t_ms` 단조 증가) 기반 구간 차분(`delta`)** 반환.
   * 직렬 포트 미연결·미수신 시 `status="unavailable"` 또는 `"timeout"` 반환(정상 `0`으로 대체 금지).

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
   * 기대 형식: 표준 ID `0x100`, `DLC == 4`, `data[0:2] == [0xA5, 0x5A]`, `data[2] == 0x00`, `data[3] == alive_counter` (`uint8`, 매 송신 시 1 증가 후 255 다음 0 wrap).
   * 유효성 검사: 길이 4B와 고정 매직 헤더(`A5 5A 00`) 불일치 시 `validity="invalid"`로 기록하고 Sequence 갱신으로 취급하지 않음.
2. **텍스트 UART 파서 (`115200 8N1`)**
   * `hw_test CAN normal v1`(`main.c`) 출력 라인 정규식 파싱:
     `t=(\d+) ms TEC=(\d+) REC=(\d+) LEC=(\d+) EWG=(\d+) EPV=(\d+) BOFF=(\d+) \| txreq=(\d+) txok=(\d+) txabort=(\d+) txbusy=(\d+) errcb=(\d+) errcode=(0x[0-9A-Fa-f]+) \| rx=(\d+) last=(0x[0-9A-Fa-f]+) dlc=(\d+)`
   * 초기 포트 오픈 시 유입되는 불완전 첫 줄/비ASCII 잔여 바이트는 버리고 완전한 개행(`\r\n`) 이후 행만 채택.
3. **핵심 판정 규칙 — 누적 오류와 구간 차분(Delta) 분리**
   * 오늘 실측(`/dev/ttyACM0`)에서 `can0`이 올라오기 전 구간 동안 누적된 `errcb=1086` (`errcode=0x00001000`)이 `can0` 정상 수신 중에도 그대로 출력됨을 확인했습니다.
   * 따라서 **`errcb > 0` 단독으로 현재 고장을 판정하지 않고**, 최근 구간 차분에서 `errcb_delta == 0`, `txreq_delta == txok_delta > 0`, `TEC == 0`, `REC == 0`이면 `UART_PAST_ERROR_INACTIVE`(과거 오류 이력 존재, 현재 통신 정상)로 구분합니다.
   * 반대로 RPi `can0`가 `ERROR-ACTIVE`인데 `0x100` 수신이 없고(`CAN_TELEMETRY_MISSING`), UART상 `txreq_delta > 0`, `txok_delta == 0`, `errcb_delta > 0`이면 송신측 ACK 부재·물리 배선 단선 후보(`UART_TX_ACTIVE_NO_CAN_RX`)로 분리합니다.

### Phase 2: 보드 A(열관리 제어기) · 보드 B(스마트 냉각팬 ECU) 본 규약 연동

임베디드 파트가 `보드 B(스마트 냉각팬 ECU) CAN 메시지 명세서 (초안)`과 보드 A 명세를 확정한 뒤, 설정 기반 코덱을 연결합니다.

* **보드 B 수신 메시지 디코더 초안** (리틀 엔디안, 최종 CAN ID는 설정 주입):
  * `B_FAN_STATUS` (100ms, DLC 8): `fan_rpm` (`uint16`, B0~1), `duty_actual` (`uint8`, B2, 0~100%), `fan_state` (`uint8`, B3: `0=INIT`, `1=STOP`, `2=RUN`, `3=FAULT`, `4=TEST`), `fault_code` (`uint8`, B4: `0x00=NONE`, `0x01=NO_ROTATION`, `0x02=RPM_DEVIATION`, `0x03=CMD_TIMEOUT`), `alive_cnt` (`uint8`, B7).
  * `B_FAN_FAULT` (이벤트, DLC 8): `fault_code` (`uint8`, B0), `rpm_at_fault` (`uint16`, B1~2), `duty_at_fault` (`uint8`, B3), `fault_count` (`uint8`, B4).
  * `B_DIAG_RESP` (응답, DLC 8): `diag_item` (`uint8`, B0), `result` (`uint8`, B1: `0=정상`, `1=이상`, `2=지원 안 함`), 상세값(B2~7).
* **고장 시나리오 연동**:
  * `B_FAN_STATUS` 미수신 또는 `alive_cnt` 정체 시 보드 B 생존/통신 단절 판정(종전 `gateway_timeout` 대체).
  * `fan_state == 4 (TEST)` 수신 시 실제 현장 고장이 아닌 고장 주입 시험 모드(`FAN_TEST_MODE_ACTIVE`)로 명시 구분.
  * 보드 A 메시지(`A_FAN_CMD` 등)가 확정되면 "보드 A 명령 송신 유무 vs 보드 B `fault_code=0x03 (CMD_TIMEOUT)`" 교차 판정을 추가합니다.

---

## 4. `RPI5_Agent_Docker` 모듈별 확장 설계

기존 [`src/rpi5_agent/`](../src/rpi5_agent) 구조를 유지하면서 아래 경계만 확장합니다.

| 파일 | 현재 상태 | 실측 연동 시 변경 설계 |
| --- | --- | --- |
| [`models.py`](../src/rpi5_agent/models.py) | `source="fixture"`, `mode="fixture"`, 고정 13종 `Scenario` | • `ToolResult.source`: `"fixture" \| "live_can" \| "live_uart" \| "manual_measurement"`<br>• `ToolResult.transport`: 기존 + `"socketcan" \| "uart_text" \| "uart_json"`<br>• `Diagnosis.mode`: `"fixture" \| "live"`<br>• 실측용 `DiagnoseRequest`에 대상 프로파일/모드 구분(또는 별도 `/diagnose/live` 분리 검토) |
| [`tools/collection.py`](../src/rpi5_agent/tools/collection.py) | `PRODUCT_READ_TOOLS`에 `can.*` 6종 + `analyze.symbols` | • 실측 읽기 도구 추가: `can.capture`, `serial.read_can_diagnostics`, `serial.get_identity`<br>• `HttpHardwareProvider` 구현 (단일 타임아웃, 재시도 없음, 연결 실패 시 `collector_unreachable` / `serial_unavailable` 반환) |
| [`specification.py`](../src/rpi5_agent/specification.py) | `FIXTURE_SPECIFICATION` (`fixture-bench-v1`) 단일 상수 | • `FIXTURE_SPECIFICATION` 보존<br>• `HW_TEST_SPECIFICATION` (`hw-test-f103-v1`: `0x100` stale 500ms, UART stale 2500ms) 및 `AIDE_ECU_SPECIFICATION` (`aide-ecu-v1`, 외부 YAML/JSON 설정 로드) 추가 |
| [`assessment.py`](../src/rpi5_agent/assessment.py) | `can.bus_state("rpi_adapter")` + `gateway.snapshot("cell1")` 고정 판정 | • 기존 `evaluate()`는 `fixture` 전용 또는 `evaluate_fixture()`로 보존<br>• 실측용 `evaluate_live_hw_test()` / `evaluate_live_aide()` 분기 (명세 ID별 판정 함수 분리, 아래 Finding 표 참고) |
| [`evidence_summary.py`](../src/rpi5_agent/evidence_summary.py) | `DATA_FIELDS`에 4개 가상 도구만 허용 | • `can.bus_state`에 `rx_errors`, `tx_errors`, `bus_errors` 추가<br>• `can.capture`, `serial.read_can_diagnostics`의 허용 스칼라 필드만 화이트리스트 등록 (원시 로그 문자열·hex 덤프 차단, 6,000B 가드 유지) |
| [`config.py`](../src/rpi5_agent/config.py) & [`api.py`](../src/rpi5_agent/api.py) | `provider=None` 고정, `mode="fixture"` 하드코딩 | • `EVIDENCE_PROVIDER=fixture\|http_collector`, `COLLECTOR_BASE_URL`, `COLLECTOR_TIMEOUT_SECONDS` 추가<br>• `/health`, `/capabilities`가 `mode`와 `physical_collection`을 실제 provider에 맞춰 반환 (실측 모드에서 `/verify`는 409 유지) |

### 실측용 Finding 코드 초안

| Finding 코드 | Severity | 판정 조건 (결정적 코드) |
| --- | --- | --- |
| `LOCAL_ADAPTER_ACTIVE` | `info` | `can0` 상태 `UP` 및 `ERROR-ACTIVE`, `bus_errors` 미증가 |
| `LOCAL_ADAPTER_ERROR_PASSIVE` | `warning` | `can0` 상태 `ERROR-PASSIVE` 또는 `rx_errors`/`tx_errors` 증가 |
| `LOCAL_ADAPTER_BUS_OFF` | `error` | `can0` 상태 `BUS-OFF` |
| `COLLECTOR_UNREACHABLE` | `error` | 호스트 수집 데몬 HTTP 연결 실패 또는 시간 초과 |
| `CAN_TELEMETRY_FRESH` | `info` | 대상 CAN ID가 `age_ms <= timeout_ms`이고 `magic/schema` 유효 |
| `CAN_TELEMETRY_STALE` | `warning` | 과거 수신 후 `age_ms > timeout_ms` |
| `CAN_TELEMETRY_MISSING` | `error` | 관측 윈도우 내 대상 CAN ID 0건 수신 |
| `CAN_SEQUENCE_GAP` | `warning` | `sequence_gap_count > 0` (부팅 변경 제외) |
| `UART_TX_ACTIVE_NO_CAN_RX` | `error` | `CAN_TELEMETRY_MISSING`이면서 UART `txreq_delta > 0`, `txok_delta == 0`, `errcb_delta > 0` |
| `UART_TX_STALLED` | `error` | UART 응답은 정상이지만 `txreq_delta == 0` |
| `UART_PAST_ERROR_INACTIVE` | `info` | `errcb > 0`이지만 `errcb_delta == 0` 및 `txok_delta > 0`, `TEC == 0`, `REC == 0` |
| `FAN_FAULT_NO_ROTATION` / `RPM_DEVIATION` / `CMD_TIMEOUT` | `error` | Phase 2 보드 B `fault_code` (`0x01`/`0x02`/`0x03`) 수신 |
| `FAN_TEST_MODE_ACTIVE` | `info` | Phase 2 보드 B `fan_state == 4 (TEST)` 수신 |

---

## 5. 임베디드 파트 협의 안건 및 보드 B `DIAG_REQ` 제안안

`보드 B(스마트 냉각팬 ECU) CAN 메시지 명세서 (초안)`에서 AI팀과 협의로 열어둔 항목에 대한 제안안입니다.

### 5-1. `DIAG_REQ` (`RPi → B`) / `B_DIAG_RESP` (`B → RPi`) 진단 항목 제안

`DIAG_REQ`는 Byte 0=`diag_item`, Byte 1~7=`0x00` 예약으로 두고, `B_DIAG_RESP`는 Byte 0=`diag_item`, Byte 1=`result`(`0=정상`, `1=이상`, `2=지원 안 함`), Byte 2~7=상세값(리틀 엔디안)으로 맞춥니다.

| `diag_item` | 이름 | `B_DIAG_RESP` Byte 2~7 제안 배치 | Agent 진단 활용 목적 |
| --- | --- | --- | --- |
| `0x01` | `IDENTITY_BOOT` | B2~3: `boot_counter` (`uint16`), B4: `reset_reason_flags` (`uint8`), B5~6: `fw_build_tag` (`uint16`), B7: `capabilities` (`uint8`) | 재부팅 여부(와치독/전원 차단)와 Sequence 초기화 구분 |
| `0x02` | `CAN_HEALTH` | B2: `tec` (`uint8`), B3: `rec` (`uint8`), B4: `lec_last` (`uint8`), B5: `tx_err_cnt_sat` (`uint8`), B6~7: `last_cmd_age_10ms` (`uint16`, 10ms 단위) | UART 없이 CAN 버스 상에서 보드 B의 TEC/REC·명령 수신 경과시간 조회 |
| `0x03` | `FAN_HW_SELFCHECK` | B2~3: `target_rpm_or_duty` (`uint16`), B4~5: `measured_rpm` (`uint16`), B6: `hall_pulse_ok` (`uint8`), B7: `driver_enable` (`uint8`) | `NO_ROTATION`/`RPM_DEVIATION` 발생 시 구동 출력 vs 홀센서 피드백 분리 |

### 5-2. `PJT-16` / 차기 회의에서 확정할 체크리스트

1. **CAN ID 배정**: `0x1~`/`0x2~` 규칙 vs 우선순위(3)/장치(2)/기능(6) 비트 규칙 확정 및 `B_FAN_STATUS`, `B_FAN_FAULT`, `B_DIAG_RESP`, `A_FAN_CMD`, `DIAG_REQ` 최종 ID 표 작성.
2. **통신 속도 · 임계치**: Classic CAN `500 kbit/s` 공식 확정, `B_FAN_STATUS` stale 기준(예: 500ms), `NO_ROTATION` 판정 시간, `RPM_DEVIATION` 허용 오차(%).
3. **보드 A(열관리 제어기) 명세**: 보드 A가 송신하는 온도/제어 명령 메시지 ID 및 바이트 배치.
4. **보조 UART 유지 여부**: 양산형 보드 A/B 펌웨어에서도 디버그용 UART 상태 출력(텍스트 또는 셋업 레포 `docs/CAN_현장_통신불량_진단_시나리오_설계.md` 5절 JSON)을 유지할지 결정.
5. **저장소 연계**: `aide-ecu` 레포의 `common/` 메시지 정의(또는 DBC/YAML)를 `RPI5_Agent_Docker`가 어떤 버전 단위로 동기화할지 결정 (`edge_ai/` 합병 vs 별도 레포 + 명세 파일 공유).

---

## 6. 검증 계획 (구현 착수 시 완료 기준)

1. **PC 단위 · 통합 테스트 (`pytest`, `ruff`)**:
   * 기존 145개 `fixture` 테스트 100% 통과 유지.
   * `hw_test` 4B 프레임/`t=... TEC=...` 텍스트 UART 파서, 구간 차분(`errcb_delta == 0` 과거 오류 분리), `B_FAN_STATUS`/`B_FAN_FAULT` 리틀 엔디안 디코더, `HttpHardwareProvider` 타임아웃·500·비유한 JSON 방어 테스트 추가.
   * `evidence_summary.py`에서 원시 UART 로그/프레임 덤프가 LLM 입력에 포함되지 않음을 테스트로 검증.
2. **가상 CAN (`vcan0`) 사전 검증**:
   * 임베디드 보드 B 완성 전, RPi 또는 Linux 환경에서 `vcan0`에 보드 B 명세서 초안의 예제 프레임(`DC 05 3C 02 00 00 00 07` 등)과 모의 고장 프레임을 주입해 디코더/판정 검증.
3. **실기기(`pi32`) Phase 1 검증**:
   * 호스트 `rpi5-hw-collector.service` + 컨테이너 `rpi5-agent-agent-1` 연동 상태에서 `STM32F103RB`(`0x100` + `/dev/ttyACM0`) 연결 시 `/diagnose`가 `mode="live"`, `CAN_TELEMETRY_FRESH`, `UART_PAST_ERROR_INACTIVE`를 반환하고, STM32 CAN 선 분리 시 `CAN_TELEMETRY_MISSING` + `UART_TX_ACTIVE_NO_CAN_RX`로 정확히 분리되는지 확인.
