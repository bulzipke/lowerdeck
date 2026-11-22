# SPDX-License-Identifier: GPL-2.0-or-later
# RetroArch Network Control Interface client (UDP).
# See: https://docs.libretro.com/development/retroarch/network-control-interface/

import socket
import time

_RECV_SIZE = 65535

MENU_API_COMMAND = "command"
MENU_API_CONFIG = "config"
MENU_API_NONE = "none"


class RAClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 55355, timeout: float = 0.25):
        self._addr = (host, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._timeout = timeout
        self._sock.settimeout(timeout)
        self._menu_api: str | None = None

    def _send(self, cmd: str) -> None:
        self._sock.sendto(cmd.encode("ascii"), self._addr)

    def _query(self, cmd: str) -> str | None:
        self._send(cmd)
        try:
            data, _ = self._sock.recvfrom(1024)
            return data.decode("ascii", errors="replace").strip()
        except socket.timeout:
            return None

    def _request(self, cmd: str, accept) -> str | None:
        # All queries share one socket, so a reply that arrived after an earlier query timed out can still be
        # queued. Drop those, then only return a reply accept() recognizes as the answer to cmd.
        self._drain()
        self._send(cmd)
        deadline = time.monotonic() + self._timeout
        try:
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._sock.settimeout(left)
                try:
                    data, _ = self._sock.recvfrom(_RECV_SIZE)
                except (socket.timeout, OSError):
                    return None
                text = data.decode("ascii", errors="replace").strip()
                if accept(text):
                    return text
        finally:
            self._sock.settimeout(self._timeout)

    def _drain(self) -> None:
        self._sock.setblocking(False)
        try:
            while True:
                self._sock.recvfrom(_RECV_SIZE)
        except (BlockingIOError, OSError):
            pass
        finally:
            self._sock.settimeout(self._timeout)

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
        def ours(text: str) -> bool:
            parts = text.split(" ", 2)
            return len(parts) > 2 and parts[0] == "GET_CONFIG_PARAM" and parts[1] == param

        res = self._request(f"GET_CONFIG_PARAM {param}", ours)
        if res is None:
            return None
        return res.split(" ", 2)[2].strip()

    def get_savestate_directory(self) -> str | None:
        return self.get_config_param("savestate_directory")

    def _menu_command(self, action: str) -> str | None:
        def ours(text: str) -> bool:
            line = text.split("\n", 1)[0]
            if line.startswith("MENU ERROR replaced"):
                return False
            return line == "CLOSED" or line.startswith("MENU ")

        res = self._request(f"MENU {action}", ours)
        if res is None:
            return None
        return res.split("\n", 1)[0]

    def _menu_config_active(self) -> bool | None:
        res = self.get_config_param("menu_active")
        if res is None or res == "unsupported":
            return None
        return res.lower() == "true"

    def _detect_menu_api(self) -> bool | None:
        res = self.get_config_param("menu_active")
        if res is None:
            return None
        if res != "unsupported":
            self._menu_api = MENU_API_CONFIG
            return res.lower() == "true"
        line = self._menu_command("show")
        if line is None:
            self._menu_api = MENU_API_NONE
            return None
        self._menu_api = MENU_API_COMMAND
        if line.startswith("MENU ERROR"):
            return None
        return line != "CLOSED"

    def get_menu_active(self) -> bool | None:
        if self._menu_api is None:
            return self._detect_menu_api()
        if self._menu_api == MENU_API_COMMAND:
            line = self._menu_command("show")
            if line is None or line.startswith("MENU ERROR"):
                return None
            return line != "CLOSED"
        if self._menu_api == MENU_API_CONFIG:
            return self._menu_config_active()
        return None

    def set_menu_open(self, want_open: bool) -> bool | None:
        actual = self._detect_menu_api() if self._menu_api is None else None
        if self._menu_api == MENU_API_COMMAND:
            line = self._menu_command("open" if want_open else "close")
            if line is None:
                return None
            if line.startswith("MENU ERROR"):
                return not want_open
            return line != "CLOSED"
        if self._menu_api != MENU_API_CONFIG:
            return None
        if actual is None:
            actual = self._menu_config_active()
            if actual is None:
                return None
        if actual != want_open:
            self.menu_toggle()
        return want_open

    def get_cheevos_enable(self) -> bool | None:
        res = self.get_config_param("cheevos_enable")
        if res is None:
            return None
        return res.strip().lower() == "true"
