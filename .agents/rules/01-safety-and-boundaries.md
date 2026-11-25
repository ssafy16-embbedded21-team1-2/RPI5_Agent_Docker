# 안전 경계 및 코드 불변식

1. **결정적 코드 판정 / LLM 설명 분리**
   * CAN/UART 프레임 파싱, 경과시간(`age_ms`), 구간 차분(`delta`), 상태·고장 판정은 반드시 Python 결정적 코드(`hw_collector.py`, `assessment.py`)가 수행합니다.
   * LLM은 `ModelExplanation` (`hypothesis`, `next_check`, 각 1\~50자 단일 행)만 생성하며 하드웨어 제어·리셋·플래싱을 호출하거나 승인하지 않습니다.

2. **호스트 수집기 · 컨테이너 보안 격리**
   * `scripts/run_hw_collector.py` 및 `src/rpi5_agent/hw_collector.py`는 **순수 읽기 전용(`GET` 전용)**으로 유지합니다. 협의 확정 전까지 능동 송신(`cansend`, ISO-TP 쓰기) 엔드포인트를 추가하지 않습니다.
   * `compose.yaml`의 `agent` 컨테이너는 `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges: true`, 일반 사용자 권한을 유지합니다.

3. **SWD 접근 제한**
   * 기본 `DIAGNOSTIC_PROFILE=product`에서는 `swd.*` 도구 실행을 공급자 호출 전에 차단합니다 (`src/rpi5_agent/tools/collection.py`).
   * `swd.halt`, `swd.reset_halt`, `fw.st_flash`, `swd.unlock` 등은 어느 프로필에서도 자동 실행하지 않습니다.

4. **구현 시 주의할 코드 함정**
   * **`assessment.py` `decode()`**: `field_validity[field] is False`인 경우 **필수 필드(`field.is_required()`)이면 모델 전체를 `None`으로 탈락**시킵니다. 무효값(`0x7FFF`, `0xFFFF`)이 올 수 있는 `temp_degc`, `vib_rms_mg` 같은 선택 필드는 반드시 기본값(`= None`)을 두어야 합니다.
   * **`tools/collection.py`**: `plan()`은 최대 16개 호출(`MAX_TOOL_CALLS`)만 허용하며 `(tool, target)` 쌍이 중복되면 계획을 거부합니다.
   * **`evidence_summary.py`**: LLM 입력은 `DATA_FIELDS` 화이트리스트와 스칼라 길이 제한(`<= 64`)을 통과한 항목만 포함하며 직렬화 크기는 `6,000`바이트를 넘길 수 없습니다. 원시 CAN hex 덤프나 UART 원문 로그 문자열을 전달하지 마세요.
   * **`llm.py` 한글 정규식**: Python 3 `\b`는 한글을 `\w`로 취급하므로 한글 텍스트와 영문 소스 경로 경계 매칭 시 `(?:^|[^A-Za-z0-9_])` 형태를 사용합니다.

5. **비밀 정보 및 개인 환경 격리**
   * `.env`, API 키, 개인 PC 절대 경로, SSH 비밀번호, 호스트 개인 IP는 어떤 코드·문서·커밋에도 포함하지 않습니다.
