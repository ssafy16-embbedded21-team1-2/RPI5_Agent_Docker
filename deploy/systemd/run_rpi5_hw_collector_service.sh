#!/usr/bin/env bash
set -euo pipefail

repo_dir="${RPI5_AGENT_DIR:-/home/pi/RPI5_Agent_Docker}"
python_bin="${RPI5_COLLECTOR_PYTHON:-/usr/bin/python3}"
collector_script="${repo_dir}/scripts/run_hw_collector.py"
collector_port="${COLLECTOR_PORT:-8090}"
can_interface="${CAN_INTERFACE:-can0}"
can_id_scheme="${CAN_ID_SCHEME:-auto}"
uart_target="${UART_TARGET:-hw_test_f103}"

if [[ ! -r "$collector_script" || ! -x "$python_bin" ]]; then
    echo "수집기 스크립트 또는 Python 실행 파일을 확인할 수 없습니다: ${collector_script}" >&2
    exit 1
fi

docker_gateway=$(docker --host unix:///var/run/docker.sock network inspect bridge \
    --format '{{(index .IPAM.Config 0).Gateway}}')
if [[ ! "$docker_gateway" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    echo "Docker 기본 브리지의 IPv4 게이트웨이를 확인할 수 없습니다: ${docker_gateway}" >&2
    exit 1
fi

echo "Hardware collector listening on ${docker_gateway}:${collector_port} (can=${can_interface}, scheme=${can_id_scheme}, uart_target=${uart_target})"
exec "$python_bin" "$collector_script" \
    --bind "$docker_gateway" \
    --port "$collector_port" \
    --can-interface "$can_interface" \
    --can-id-scheme "$can_id_scheme" \
    --uart-target "$uart_target"

