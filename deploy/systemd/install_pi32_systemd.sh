#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run as root (e.g. sudo bash $0)" >&2
    exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

install -d -m 0755 /usr/local/libexec
install -m 0755 "${script_dir}/setup_rpi5_can0.sh" /usr/local/libexec/setup_rpi5_can0.sh
install -m 0755 "${script_dir}/run_rpi5_hw_collector_service.sh" /usr/local/libexec/run_rpi5_hw_collector_service.sh
install -m 0644 "${script_dir}/rpi5-can0.service" /etc/systemd/system/rpi5-can0.service
install -m 0644 "${script_dir}/rpi5-hw-collector.service" /etc/systemd/system/rpi5-hw-collector.service

systemctl daemon-reload
systemctl enable --now rpi5-can0.service rpi5-hw-collector.service
systemctl status --no-pager rpi5-can0.service rpi5-hw-collector.service
