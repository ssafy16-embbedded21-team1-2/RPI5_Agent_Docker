#!/usr/bin/env bash
set -euo pipefail

iface="${CAN_INTERFACE:-can0}"
bitrate="${CAN_BITRATE:-500000}"
restart_ms="${CAN_RESTART_MS:-100}"
txqlen="${CAN_TXQUEUELEN:-1024}"

if ! ip link show "$iface" >/dev/null 2>&1; then
    echo "${iface} interface not present; skipping CAN bringup."
    exit 0
fi

ip link set "$iface" down || true
ip link set "$iface" up type can bitrate "$bitrate" restart-ms "$restart_ms"
ip link set "$iface" txqueuelen "$txqlen" || true
ip -details link show "$iface"
