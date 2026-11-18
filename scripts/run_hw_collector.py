"""Run the host-side read-only Phase 1 CAN + UART collector HTTP server."""

from __future__ import annotations

import argparse
import os
import signal
import socket
import struct
import sys
import threading
import time
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from rpi5_agent.hw_collector import (  # noqa: E402
    DEFAULT_UART_DEVICE_BY_ID,
    CanCollectorBuffer,
    UartCollectorBuffer,
    create_collector_http_server,
)

CAN_FRAME_FMT = "=IB3x8s"
CAN_FRAME_SIZE = struct.calcsize(CAN_FRAME_FMT)
CAN_SFF_MASK = 0x000007FF
CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000


def _can_reader_loop(
    interface: str, can_buffer: CanCollectorBuffer, stop_event: threading.Event
) -> None:
    af_can = getattr(socket, "AF_CAN", None)
    can_raw = getattr(socket, "CAN_RAW", None)
    if af_can is None or can_raw is None:
        return
    while not stop_event.is_set():
        sock = None
        try:
            sock = socket.socket(af_can, socket.SOCK_RAW, can_raw)
            sock.settimeout(0.5)
            sock.bind((interface,))
            while not stop_event.is_set():
                try:
                    raw = sock.recv(CAN_FRAME_SIZE)
                except TimeoutError:
                    continue
                if len(raw) != CAN_FRAME_SIZE:
                    continue
                raw_can_id, dlc, data = struct.unpack(CAN_FRAME_FMT, raw)
                if raw_can_id & (CAN_EFF_FLAG | CAN_RTR_FLAG | CAN_ERR_FLAG):
                    continue
                arb_id = raw_can_id & CAN_SFF_MASK
                can_buffer.record_frame(arb_id, data[:dlc], dlc=dlc)
        except OSError:
            stop_event.wait(1.0)
        finally:
            if sock is not None:
                sock.close()


def _configure_serial_115200_8n1(fd: int) -> None:
    import termios  # POSIX-only runtime import

    attrs = termios.tcgetattr(fd)
    attrs[0] = 0  # iflag: raw input
    attrs[1] = 0  # oflag: raw output
    attrs[2] = termios.CS8 | termios.CLOCAL | termios.CREAD
    attrs[3] = 0  # lflag: non-canonical, no echo/signals
    attrs[4] = termios.B115200
    attrs[5] = termios.B115200
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 5  # 0.5s read timeout for clean shutdown
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


def _uart_reader_loop(
    device_path: str, uart_buffer: UartCollectorBuffer, stop_event: threading.Event
) -> None:
    while not stop_event.is_set():
        fd: int | None = None
        try:
            fd = os.open(device_path, os.O_RDONLY | os.O_NOCTTY)
            _configure_serial_115200_8n1(fd)
            uart_buffer.reset_stream()
            while not stop_event.is_set():
                chunk = os.read(fd, 512)
                if chunk:
                    uart_buffer.feed_bytes(chunk)
        except OSError:
            stop_event.wait(1.0)
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--can-interface", default="can0")
    parser.add_argument("--uart-target", default="hw_test_f103")
    parser.add_argument("--uart-device", default=DEFAULT_UART_DEVICE_BY_ID)
    args = parser.parse_args()

    can_buffer = CanCollectorBuffer()
    uart_buffer = UartCollectorBuffer()
    stop_event = threading.Event()

    can_thread = threading.Thread(
        target=_can_reader_loop,
        args=(args.can_interface, can_buffer, stop_event),
        daemon=True,
    )
    uart_thread = threading.Thread(
        target=_uart_reader_loop,
        args=(args.uart_device, uart_buffer, stop_event),
        daemon=True,
    )
    can_thread.start()
    uart_thread.start()

    server = create_collector_http_server(
        args.bind,
        args.port,
        can_buffer=can_buffer,
        uart_buffer=uart_buffer,
        can_interface=args.can_interface,
        uart_target=args.uart_target,
        uart_device_path=args.uart_device,
    )

    def _handle_signal(_signum: int, _frame: object) -> None:
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        stop_event.set()
        server.server_close()
        can_thread.join(timeout=2.0)
        uart_thread.join(timeout=2.0)
        time.sleep(0.05)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
