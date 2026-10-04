# SPDX-License-Identifier: GPL-2.0-or-later
# RetroArch Network Control Interface client (UDP).
# See: https://docs.libretro.com/development/retroarch/network-control-interface/

import socket
import time


class RAClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 55355, timeout: float = 0.25):
        self._addr = (host, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._timeout = timeout
        self._sock.settimeout(timeout)

    def _send(self, cmd: str) -> None:
        self._sock.sendto(cmd.encode("ascii"), self._addr)

    def _query(self, cmd: str) -> str | None:
        self._send(cmd)
        try:
            data, _ = self._sock.recvfrom(1024)
            return data.decode("ascii", errors="replace").strip()
        except socket.timeout:
            return None

    def menu_toggle(self) -> None:
        self._send("MENU_TOGGLE")

    def save_state(self) -> None:
        self._send("SAVE_STATE")

    def load_state(self) -> None:
        self._send("LOAD_STATE")

    def save_state_slot(self, slot: int) -> None:
        self._query(f"SAVE_STATE_SLOT {int(slot)}")

    def load_state_slot(self, slot: int) -> None:
        self._query(f"LOAD_STATE_SLOT {int(slot)}")

    def get_config_param(self, param: str) -> str | None:
        # All queries share one socket, so a reply that arrived after an earlier query timed out can still be
        # queued. Drop those, then only accept the reply for this param.
        self._drain()
        self._send(f"GET_CONFIG_PARAM {param}")
        deadline = time.monotonic() + self._timeout
        try:
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._sock.settimeout(left)
                try:
                    data, _ = self._sock.recvfrom(1024)
                except (socket.timeout, OSError):
                    return None
                parts = data.decode("ascii", errors="replace").strip().split(" ", 2)
                if len(parts) > 2 and parts[0] == "GET_CONFIG_PARAM" and parts[1] == param:
                    return parts[2].strip()
        finally:
            self._sock.settimeout(self._timeout)

    def _drain(self) -> None:
        self._sock.setblocking(False)
        try:
            while True:
                self._sock.recvfrom(1024)
        except (BlockingIOError, OSError):
            pass
        finally:
            self._sock.settimeout(self._timeout)

    def get_savestate_directory(self) -> str | None:
        return self.get_config_param("savestate_directory")

    def get_menu_active(self) -> bool | None:
        res = self.get_config_param("menu_active")
        if res is None:
            return None
        return res.strip().lower() == "true"

    def get_cheevos_enable(self) -> bool | None:
        res = self.get_config_param("cheevos_enable")
        if res is None:
            return None
        return res.strip().lower() == "true"
