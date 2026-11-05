# Docker 검증 기록

검증일: 2026-11-05 (한국 시간)

## 실행 환경

| 항목              | 확인값                                    |
| ----------------- | ----------------------------------------- |
| 호스트            | Windows, Docker Desktop Linux 엔진 / WSL2 |
| Docker Desktop    | 4.98.0                                    |
| Docker Engine     | 29.9.2                                    |
| PC 컨테이너       | Linux AMD64, Python 3.12.16               |
| 기본 이미지       | `rpi5-agent:local`                        |
| ARM64 검사 이미지 | `rpi5-agent:arm64-check`                  |

## PC 컨테이너 검증

다음 항목이 통과했습니다.

- `docker compose up -d --build`: 잠금 파일을 사용한 이미지 빌드와 실행
- `docker compose up -d --wait --wait-timeout 90`: 컨테이너 `healthy` 확인
- `python scripts/smoke_test.py`: 정상, BMS 갱신 지연, 로컬 어댑터 Bus-Off 시나리오
- `/docs`, `/openapi.json`: HTTP 200
- 모델 비활성화 상태에서 모델 설명 요청: HTTP 409
- 존재하지 않는 시나리오 또는 임의 제어 필드: HTTP 422
- 일반 사용자 UID 10001, 읽기 전용 루트 파일시스템, capability 제거 확인
- 메모리 768MiB와 CPU 최대 1코어 상당의 제한 적용 확인

소스 수정 없이 Dockerfile과 Compose 설정이 동작했습니다.
검증 후 기본 컨테이너는 `http://127.0.0.1:8000`에서 실행 상태로 유지했습니다.

## 컨테이너에서 호스트 모델 서버로 연결

호스트에 임시 HTTP 모형 서버를 띄우고, 일회용 Agent 컨테이너에 다음 설정을 주입했습니다.

```dotenv
LLM_ENABLED=true
LLM_BASE_URL=http://host.docker.internal:<임시포트>/v1
LLM_MODEL=docker-stub
LLM_TIMEOUT_SECONDS=10
```

컨테이너 안에서 실제 LangChain 클라이언트가 호스트의 `/v1/chat/completions`를 호출했고,
모형 응답으로 `llm_status=generated`가 반환되는 것을 확인했습니다.
규칙이 판정한 `BMS_STALE`도 결과에 유지됐습니다.
검사용 컨테이너와 모형 서버는 종료했습니다. 기본 컨테이너의 모델 설정은 비활성화 상태입니다.

이 검사는 Docker 네트워크와 모델 API 연동을 확인하며, 실제 Qwen의 추론 품질은 검증하지 않습니다.

## ARM64 이미지 빌드

다음 명령으로 Linux ARM64 이미지 빌드가 통과했습니다.

```bash
docker buildx build --platform linux/arm64 \
  --tag rpi5-agent:arm64-check --load .
```

PC용 `rpi5-agent:local`과 다른 태그를 사용했으므로 기본 Compose 서비스는 AMD64 이미지를 계속 사용합니다.

ARM64 이미지도 PC의 에뮬레이션 환경에서 실행해 `platform.machine()=aarch64`를 확인했고,
HTTP 상태 조회와 세 가지 가상 진단이 모두 통과했습니다. 검사용 ARM64 컨테이너는 종료·제거했습니다.

최초의 짧은 기동 대기에서는 시간 초과가 발생했습니다.
이후 import 로그를 확인하며 CPU 상한 없이 최대 150초 대기하는 조건으로 검사했고,
약 64초 후 API가 응답했습니다. 이 시간은 PC의 ARM 에뮬레이션 관측값이며 RPi 기동 시간으로 해석하지 않습니다.
ARM64 에뮬레이션에서는 기본 Compose의 CPU 제한 조건에 대한 성능 검증을 수행하지 않았습니다.

## 자원 관측

모델을 사용하지 않는 AMD64 Agent에서 `docker stats --no-stream`으로 관측한 메모리는 약 92MiB였습니다.
이는 Docker Engine·WSL 가상머신·호스트 모델 서버를 제외한 컨테이너 관측값이며,
일시적인 단일 관측값으로 RPi 5 사용량이나 운영 중 최대 사용량을 보장하지 않습니다.
AMD64 이미지의 Docker 보고 크기는 287,865,543바이트였습니다.

## 별도 검증이 필요한 항목

- RPi 5와 실제 Qwen의 단기 연결·실행 결과는 [RPi 5 검증 기록](RPi5_검증_기록.md)에 추가함
- 장시간 동작, 한국어 진단 설명의 정확성과 다양한 입력의 응답 시간
- 실제 CAN·UDS·SWD와 STM32 연결: 현재 코드에는 가상 데이터 공급자만 구현됨
