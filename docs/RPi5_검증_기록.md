# RPi 5 실기기 검증 기록

검증일: 2026-11-05 (한국 시간)

## 배포 환경

| 항목           | 확인값                                                                 |
| -------------- | ---------------------------------------------------------------------- |
| SSH            | `pi@100.92.169.70` (Tailscale)                                         |
| 호스트         | `pi32`, ARM64, RAM 약 8GB                                              |
| OS             | Debian 13.7 / trixie                                                   |
| 배포 경로      | `/home/pi/RPI5_Agent_Docker`                                           |
| Git 원격       | `https://github.com/ssafy16-embbedded21-team1-2/RPI5_Agent_Docker.git` |
| 검증한 커밋    | `8045c5263b3d480311164510d63e73e343641cc8`                             |
| Docker Engine  | 29.9.3                                                                 |
| Docker Compose | 5.7.0                                                                  |
| Agent 런타임   | ARM64, Python 3.12.16, UID 10001                                       |
| llama.cpp      | 0.8.0-dev / build 12005 / commit `e0f142d5d`                           |
| 모델           | Qwen 2.5 3B Instruct, Q4_K_M GGUF                                      |

Docker가 설치되어 있지 않아 공식 Debian 저장소로 Engine·CLI·containerd·Buildx·Compose를 설치했습니다.
Docker 서비스 자동 시작을 설정했고 `pi`를 Docker 그룹에 추가했습니다.
레포는 GitHub에서 직접 clone했으며 검증 시 애플리케이션 코드는 수정하지 않았습니다.

## 통과한 항목

- RPi 자체에서 ARM64 Docker 이미지 빌드
- Compose 실행과 컨테이너 `healthy` 확인
- `healthy`, `bms_timeout`, `bus_off` 세 가지 가상 진단
- 실제 Qwen 모델 서버의 `/health`와 `/v1/models` 응답
- Agent → Docker 호스트 주소 → llama-server → LangChain 설명 생성
- 모델 추론 중 Agent `/health` 응답 유지
- 모델 추론 중 추가 진단 요청에 HTTP 429 반환
- CPU 최대 1코어 상당의 제한 적용 (`NanoCPUs=1000000000`)

실제 모델을 사용했지만 진단 입력은 계속 가상 데이터입니다.
CAN·UDS·SWD·STM32 실측과 제어는 검증 범위에 포함되지 않습니다.

## 실제 모델 요청 결과

요청:

```json
{
  "scenario": "bms_timeout",
  "use_llm": true
}
```

| 항목           | 관측값                                 |
| -------------- | -------------------------------------- |
| API 전체 시간  | 77.91초                                |
| 입력 평가      | 443토큰 / 18.40초                      |
| 출력 생성      | 256토큰 / 59.46초                      |
| 출력 생성 속도 | 약 4.29토큰/초                         |
| 결과 상태      | `llm_status=generated`, `mode=fixture` |
| 코드 판정      | `GATEWAY_FRESH`, `BMS_STALE` 유지      |

단일 최초 요청의 관측값이며 평균·최대 지연시간이나 지속 성능을 보장하지 않습니다.
응답 원본과 전체 소요시간은 RPi와 PC의 `data/rpi-qwen-bms-timeout.json`에 저장했습니다.
이 파일은 Git 무시 대상입니다.

모델은 게이트웨이와 BMS 데이터 갱신을 구분했지만 원인 설명에 반복과 모호한 문장이 있었습니다.
출력은 설정된 256토큰을 사용했고 마지막 문장이 중간에 끝났습니다.
현재 `llm_status=generated`는 텍스트 생성 성공만 나타내며 문장 완결성이나 설명 정확성을 보장하지 않습니다.
다음 개선 대상은 짧은 출력 형식, 프롬프트 조정, 종료 사유 전달과 응답 품질 평가입니다.

## 자원과 확인된 제한

### 메모리 cgroup 미활성화

현재 커널 명령행에 `cgroup_disable=memory`가 있으며 cgroup v2 컨트롤러는
`cpuset cpu io pids`만 제공했습니다.
Docker는 `No memory limit support`, `No swap limit support`를 보고했습니다.

따라서 **Compose의 768MiB 메모리 제한은 현재 RPi에서 적용되지 않습니다.**
실제 컨테이너 설정도 `MemoryBytes=0`으로 확인됐습니다.
이 환경의 `docker stats` 메모리 표시는 `0B / 0B`이므로 메모리를 사용하지 않는다는 뜻으로 해석하면 안 됩니다.

프로세스 RSS로 관측한 Agent `uvicorn` 사용량은 약 110~111MiB였습니다.
Qwen 요청 완료 후 호스트의 가용 메모리는 약 5.1GiB, 스왑 사용량은 0이었습니다.
프로세스 RSS와 호스트 가용 메모리는 서로 다른 지표이며 Docker 데몬 등의 사용량도 별도 존재합니다.

메모리 제한을 활성화하려면 실제 부팅 설정의 memory cgroup 설정을 점검하고,
설정 변경·재부팅 후 컨트롤러와 Docker 제한 적용을 다시 확인해야 합니다.
이번 검증에서는 부팅 설정 변경이나 재부팅을 수행하지 않았습니다.
관련 전제는 [Docker 자원 제한 문서](https://docs.docker.com/engine/containers/resource_constraints/)를 참고합니다.

### 온도

추론 중 SoC 온도 84.5°C와 `get_throttled=0xe0008`이 관측됐습니다.
이 플래그에는 soft temperature limit 활성 상태와 주파수 제한·스로틀링 발생 이력이 포함됩니다.
현재 스로틀링 비트와 과거 발생 비트는 구분해야 합니다.
비트 정의는 [Raspberry Pi 공식 문서](https://www.raspberrypi.com/documentation/computers/os.html#get_throttled)를 참고합니다.

냉각 상태를 점검한 뒤 장시간 추론과 반복 요청의 성능을 추가 측정해야 합니다.

## 검증 후 실행 상태와 접속

- Agent 컨테이너: `rpi5-agent-agent-1`, `127.0.0.1:8000`
- 모델 서버: 사용자 임시 systemd 서비스 `rpi5-agent-llama.service`
- 모델 바인딩: `127.0.0.1:8080`, `172.17.0.1:8080` (Docker 기본 bridge 게이트웨이)
- 모델 별칭: `local-qwen`
- RPi의 `.env`: `LLM_ENABLED=true`, 최대 출력 256토큰, 제한 시간 120초

PC에서 다음 SSH 터널을 연결합니다. PC의 기존 Agent 8000 포트와 겹치지 않게 18000을 사용합니다.

```bash
ssh -N -L 18000:127.0.0.1:8000 pi@100.92.169.70
```

브라우저에서 `http://127.0.0.1:18000/docs`를 열어 진단을 실행합니다.
`use_llm=false`는 빠른 가상 진단, `use_llm=true`는 실제 Qwen 설명 생성을 추가합니다.

RPi에서 상태 확인과 중지:

```bash
cd /home/pi/RPI5_Agent_Docker
docker compose ps
docker compose logs --tail=50
systemctl --user status rpi5-agent-llama.service

# 테스트 서비스를 중지하려는 경우
docker compose stop
systemctl --user stop rpi5-agent-llama.service
```

모델 서비스는 이번 검증을 위한 임시 서비스입니다. 재부팅 후 자동 기동은 구성하지 않았습니다.
재부팅하거나 사용자 서비스 관리자가 종료되어 단위가 사라진 뒤에는 다음처럼 다시 생성합니다.

```bash
gateway=$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')
systemd-run --user --unit=rpi5-agent-llama --collect \
  --property=Restart=on-failure \
  --property=WorkingDirectory=/home/pi/RPI5_Agent_Docker \
  /usr/local/bin/llama-server \
  -m /home/pi/models/qwen2.5-3b-instruct-q4_k_m.gguf \
  --host "127.0.0.1,$gateway" --port 8080 --alias local-qwen \
  -t 4 -c 2048 --parallel 1 --temp 0.2
```

## 출력 형식 개선 후 모델 연동 확인

2026-11-05, 수정한 PC 작업 트리의 Agent를 SSH 터널로 RPi의 기존 Qwen 서버에 연결했습니다.
RPi에서 실행 중인 Agent 컨테이너를 업데이트한 검사는 아닙니다.
PC Docker 이미지는 수정본으로 재빌드했고 가상 진단 3종이 통과했습니다.

수정본은 관측 문장을 코드에서 조립하고, 모델에는 `hypothesis`와 `next_check`만 요청합니다.
JSON Schema로 출력을 제한하고 각 필드의 타입·길이·줄바꿈·추가 필드를 검증합니다.
종료 사유가 `stop`이고 검증을 통과한 경우에만 모델 설명을 사용합니다.
출력 잘림이나 형식 오류에서는 코드 기반 관측을 유지하며 종료 사유와 토큰 사용량을 반환합니다.
해당 경로와 기존 동작을 포함한 테스트 25개가 통과했습니다.

동일한 `bms_timeout` 가상 시나리오의 실제 모델 연동 관측값:

| 항목 | 관측값 |
| --- | --- |
| API 시간 | 20.53초 |
| 입력 토큰 | 366 |
| 출력 토큰 | 33 |
| 종료 사유 | `stop` |
| 상태 | `generated` |
| `hypothesis` | BMS 데이터 갱신 지연 |
| `next_check` | BMS 갱신 시간 확인 |

원본은 PC의 `data/rpi-structured-output.json`에 저장했습니다(Git 무시 대상).
두 필드 형식과 정상 종료는 확인했으나, 원인 후보가 관측을 반복하고 완성된 문장 대신 짧은 구절로 나왔습니다.
한국어 한 문장 작성은 프롬프트 지시이며 현재 검증기는 문장의 의미·문법을 판정하지 않습니다.
후속 설명 품질 평가는 계속 필요합니다.
기존 77.91초 결과와는 애플리케이션 실행 위치·프롬프트·출력 길이·모델 캐시·온도 조건이 달라
동일 품질의 성능 향상 비율로 해석하지 않습니다.

## 구조화 출력 수정본 RPi 배포

2026-11-05, 배포 ID `20261105T060800Z`로 구조화 출력 수정본을 RPi에도 반영했습니다.
로컬 기준 커밋 `51c0213`에 미커밋 변경을 포함한 작업 파일 스냅샷을 전송했습니다.
RPi의 Git HEAD는 기존 `8045c52`이며, 배포한 변경은 작업 트리에 남아 있습니다.

- 소스 해시를 검증하고 RPi에서 ARM64 이미지를 빌드한 뒤 Compose 컨테이너를 교체했습니다.
- 가상 진단 3종과 새 API의 `explanation`, `llm_metadata` 필드를 확인했습니다.
- RPi Docker Agent → 호스트 Qwen의 실제 요청에서 정상 종료 `stop`을 확인했습니다.
- 단일 요청은 6.98초, 입력 366토큰·출력 35토큰이었습니다.
- 직전 요청과 같은 프롬프트를 사용했으므로 모델 캐시와 온도 등의 영향을 포함한 관측값입니다.
- 모델 출력은 `BMS 원본 데이터 갱신 지연` / `BMS 데이터 갱신 확인`으로, 의미적 설명 품질 보완은 계속 필요합니다.
- 검증 후 컨테이너는 `healthy`, 모델 서비스는 `active`였고 SoC 온도는 77.4°C였습니다.

배포 파일과 결과는 RPi의 `data/deployments/20261105T060800Z/`에 보관했습니다.
`DEPLOYMENT_MANIFEST.json`은 해당 디렉터리의 `source/` 안에 있으며,
검증 응답은 `verification.json`, 이전 소스는 `source-before.tar.gz`에 있습니다.
이전 이미지는 `rpi5-agent:rollback-20261105T060800Z` 태그로 보관했습니다.
RPi의 `.env`를 유지해 기존 모델 연결 설정을 사용했습니다.
