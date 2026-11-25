---
name: rpi5-ops-and-deploy
description: >-
  Use when deploying RPI5_Agent_Docker on Raspberry Pi 5, checking systemd services
  (rpi5-can0, rpi5-hw-collector, rpi5-agent-llama), inspecting SocketCAN can0 state,
  switching runtime specifications in .env, or running smoke tests on RPi 5.
---

# RPi 5 Operations and Deployment Runbook

Raspberry Pi 5 호스트에서 `RPI5_Agent_Docker`와 호스트 수집·추론 서비스를 점검하거나 배포할 때 따르는 절차입니다. (호스트 접속 정보·비밀번호·API 키는 각자 로컬 환경에서 관리하며 파일에 기록하지 않습니다.)

## Steps

1. **`systemd` 상시 서비스 및 Docker 컨테이너 확인**
   ```bash
   systemctl is-enabled rpi5-can0.service rpi5-hw-collector.service rpi5-agent-llama.service docker.service
   systemctl status rpi5-can0.service rpi5-hw-collector.service rpi5-agent-llama.service --no-pager
   docker compose ps
   ```
   * 새 RPi 5에 `systemd` 유닛을 설치/갱신할 때는 `deploy/systemd/install_pi32_systemd.sh`를 검토한 뒤 실행합니다.

2. **SocketCAN `can0` 상태 점검**
   ```bash
   ip -details -statistics link show can0
   candump -e -ta can0
   ```
   * 정상 기준: `UP`, `ERROR-ACTIVE`, `bitrate 500000`, `bus-error 0`.

3. **`.env` 모드 전환 및 컨테이너 재기동**
   * 가상 검증 모드: `EVIDENCE_PROVIDER=fixture`
   * Phase 1 (`hw_test` `0x100`): `EVIDENCE_PROVIDER=http_collector`, `LIVE_SPECIFICATION=hw_test_f103`
   * Phase 2 (`0x080`/`0x180`/`0x1C0`): `EVIDENCE_PROVIDER=http_collector`, `LIVE_SPECIFICATION=aide_ecu`
   ```bash
   docker compose config --quiet
   docker compose up -d --build --force-recreate
   ```

4. **헬스체크 및 스모크 테스트**
   ```bash
   curl -sS http://127.0.0.1:8000/health
   curl -sS http://127.0.0.1:8000/capabilities
   uv run python scripts/smoke_test.py
   ```
   * `scripts/smoke_test.py`는 `httpx`에 의존하므로 시스템 `python3`에 패키지가 없으면 `uv run python scripts/smoke_test.py`로 실행합니다.
   * `/health`의 `200 OK`는 API 프로세스 생존 여부만 의미하며 모델·CAN 연결 성공을 보장하지 않습니다. 실물 CAN 상태는 `/capabilities`와 `POST /diagnose` 결과의 `findings`로 확인합니다.
