# SPDX-License-Identifier: GPL-2.0-or-later
# Background poller for the local RA proxy's /_ui/state endpoint.

import json
import threading
import urllib.error
import urllib.request


class RAStatePoller:
    def __init__(self, url: str, interval_sec: float = 0.5):
        self.url = url
        self.interval = interval_sec
        self._state: dict = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: str | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="ra-state-poller", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with urllib.request.urlopen(self.url, timeout=2) as r:
                    data = json.loads(r.read().decode("utf-8"))
                with self._lock:
                    self._state = data
                    self._error = None
            except (urllib.error.URLError, OSError, ValueError) as e:
                with self._lock:
                    self._error = str(e)
            self._stop.wait(self.interval)

    def get(self) -> dict:
        with self._lock:
            return dict(self._state) if self._state else {}

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error
