# SPDX-License-Identifier: GPL-2.0-or-later
# Local proxy between RetroArch and retroachievements.org.

import argparse
import http.server
import json
import os
import queue
import socketserver
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.abspath(__file__))


# ---------- config ----------

DEFAULTS = {
    "listen_host": "127.0.0.1",
    "listen_port": 8080,
    "upstream": "https://retroachievements.org",
    "badges_dir": "/storage/thumbnails/cheevos/badges",
    "retroarch_process_match": "/usr/bin/retroarch",
}


def load_config(config_path):
    cfg = dict(DEFAULTS)
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            top = json.load(f)
        section = top.get("ra_proxy") or {}
        cfg.update({k: v for k, v in section.items() if k in DEFAULTS})
    except (OSError, ValueError) as e:
        print(f"warning: {config_path}: {e}; using defaults", file=sys.stderr)

    local = os.path.join(os.path.dirname(config_path), "config.local.json")
    if os.path.exists(local):
        try:
            with open(local, "r", encoding="utf-8") as f:
                top = json.load(f)
            section = top.get("ra_proxy") or {}
            cfg.update({k: v for k, v in section.items() if k in DEFAULTS})
        except (OSError, ValueError) as e:
            print(f"warning: ignoring {local}: {e}", file=sys.stderr)
    return cfg


# ---------- shared state (in-memory only) ----------

class State:

    def __init__(self):
        self.lock = threading.Lock()
        self.user = {}                       # {name, score, softcore_score, token, avatar_url}
        self.game = {}                       # {id, title, icon_url, console_id}
        self.achievements = []               # list[dict] in API order
        self.unlocks = {}                    # {ach_id: {"when": int, "hardcore": bool}}
        self.login_status = "unknown"        # "ok" | "failed" | "unknown"
        self.upstream_status = "unknown"     # "ok" | "down" | "unknown"
        self.updated_at = 0                  # epoch seconds

    def snapshot(self):
        with self.lock:
            return {
                "user": dict(self.user),
                "game": dict(self.game),
                "achievements": list(self.achievements),
                "unlocks": {str(k): v for k, v in self.unlocks.items()},
                "login_status": self.login_status,
                "upstream_status": self.upstream_status,
                "updated_at": self.updated_at,
            }

    def touch(self):
        self.updated_at = int(time.time())


STATE = State()


# ---------- badge downloader ----------

_dl_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
_dl_seen: set[str] = set()
_dl_seen_lock = threading.Lock()


def queue_download(url, dest_path):
    if not url or not dest_path:
        return
    if os.path.exists(dest_path):
        return
    with _dl_seen_lock:
        if dest_path in _dl_seen:
            return
        _dl_seen.add(dest_path)
    _dl_queue.put((url, dest_path))


def _download_worker():
    ctx = ssl.create_default_context()
    while True:
        url, dest = _dl_queue.get()
        try:
            tmp = dest + ".part"
            req = urllib.request.Request(url, headers={"User-Agent": "ra-proxy/1"})
            with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
                data = r.read()
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, dest)
        except Exception as e:
            print(f"warning: badge download {url}: {e!r}", file=sys.stderr)
            with _dl_seen_lock:
                _dl_seen.discard(dest)
        finally:
            _dl_queue.task_done()


# ---------- response interceptors ----------

def _badge_paths(badges_dir, badge_name):
    if not badge_name:
        return None, None
    return (
        os.path.join(badges_dir, f"{badge_name}.png"),
        os.path.join(badges_dir, f"{badge_name}_lock.png"),
    )


def on_login2(_req_form, body, _cfg):
    with STATE.lock:
        if body.get("Success"):
            STATE.user = {
                "name": body.get("User"),
                "token": body.get("Token"),
                "score": body.get("Score", 0),
                "softcore_score": body.get("SoftcoreScore", 0),
                "avatar_url": body.get("AvatarUrl"),
            }
            STATE.login_status = "ok"
        else:
            STATE.user = {}
            STATE.login_status = "failed"
        STATE.touch()


def on_achievementsets(_req_form, body, cfg):
    if not body.get("Success"):
        return
    achievements = []
    for set_obj in body.get("Sets") or []:
        for a in set_obj.get("Achievements") or []:
            badge = str(a.get("BadgeName") or "")
            achievements.append({
                "id": a.get("ID"),
                "set_id": set_obj.get("AchievementSetId"),
                "set_type": set_obj.get("Type"),
                "title": a.get("Title", ""),
                "desc": a.get("Description", ""),
                "points": a.get("Points", 0),
                "badge": badge,
                "badge_url": a.get("BadgeURL"),
                "badge_locked_url": a.get("BadgeLockedURL"),
                "type": a.get("Type"),
                "rarity": a.get("Rarity"),
                "rarity_hardcore": a.get("RarityHardcore"),
                "flags": a.get("Flags"),
            })
            unl, lck = _badge_paths(cfg["badges_dir"], badge)
            queue_download(a.get("BadgeURL"), unl)
            queue_download(a.get("BadgeLockedURL"), lck)

    with STATE.lock:
        STATE.game = {
            "id": body.get("GameId"),
            "title": body.get("Title", ""),
            "icon_url": body.get("ImageIconUrl") or "",
            "console_id": body.get("ConsoleId"),
        }
        STATE.achievements = achievements
        STATE.unlocks = {}
        STATE.touch()


def on_startsession(_req_form, body, _cfg):
    if not body.get("Success"):
        return
    unlocks = {}
    for u in body.get("HardcoreUnlocks") or []:
        unlocks[u["ID"]] = {"when": u.get("When"), "hardcore": True}
    for u in body.get("Unlocks") or []:
        unlocks.setdefault(u["ID"], {"when": u.get("When"), "hardcore": False})
    with STATE.lock:
        STATE.unlocks = unlocks
        STATE.touch()


def on_awardachievement(req_form, body, _cfg):
    if not body.get("Success"):
        return
    ach_id = body.get("AchievementID")
    if ach_id is None:
        try:
            ach_id = int(req_form.get("a", [""])[0])
        except (ValueError, TypeError):
            return
    hardcore = (req_form.get("h", ["0"])[0] == "1")
    with STATE.lock:
        STATE.unlocks[ach_id] = {"when": int(time.time()), "hardcore": hardcore}
        if "Score" in body:
            STATE.user["score"] = body["Score"]
        if "SoftcoreScore" in body:
            STATE.user["softcore_score"] = body["SoftcoreScore"]
        STATE.touch()


INTERCEPTORS = {
    "login2": on_login2,
    "achievementsets": on_achievementsets,
    "startsession": on_startsession,
    "awardachievement": on_awardachievement,
}


def intercept(req_body_bytes, resp_body_bytes, resp_ctype, cfg):
    if "json" not in resp_ctype:
        return
    try:
        form = urllib.parse.parse_qs(req_body_bytes.decode("utf-8", "replace"))
    except Exception:
        return
    r = form.get("r", [""])[0]
    handler = INTERCEPTORS.get(r)
    if not handler:
        return
    try:
        body = json.loads(resp_body_bytes.decode("utf-8", "replace"))
    except Exception as e:
        print(f"warning: intercept {r}: bad json: {e!r}", file=sys.stderr)
        return
    try:
        handler(form, body, cfg)
    except Exception as e:
        print(f"warning: intercept {r}: {e!r}", file=sys.stderr)


# ---------- HTTP server (proxy + UI read endpoint) ----------

HOP_HEADERS = {"transfer-encoding", "connection", "keep-alive",
               "proxy-authenticate", "proxy-authorization", "te",
               "trailers", "upgrade", "content-length"}


def make_handler(cfg):

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send_json(self, payload, status=200):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _serve_internal(self):
            if self.command == "GET" and self.path == "/_ui/state":
                self._send_json(STATE.snapshot())
                return True
            if self.command == "GET" and self.path == "/_ui/health":
                self._send_json({"ok": True, "ts": int(time.time())})
                return True
            return False

        def _handle(self):
            if self.path.startswith("/_ui/"):
                if self._serve_internal():
                    return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            method = self.command
            path = self.path
            length = int(self.headers.get("Content-Length", "0") or "0")
            req_body = self.rfile.read(length) if length else b""
            fwd_headers = {k: v for k, v in self.headers.items() if k.lower() != "host"}
            fwd_headers.setdefault("User-Agent", "ra-proxy/1")

            upstream_url = cfg["upstream"] + path
            req = urllib.request.Request(upstream_url, data=req_body if req_body else None, headers=fwd_headers,
                                         method=method)
            status = 0
            resp_headers = {}
            resp_body = b""
            err = None
            try:
                ctx = ssl.create_default_context()
                with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
                    status = r.status
                    resp_headers = dict(r.headers.items())
                    resp_body = r.read()
            except urllib.error.HTTPError as e:
                status = e.code
                resp_headers = dict(e.headers.items()) if e.headers else {}
                resp_body = e.read() or b""
            except Exception as e:
                err = repr(e)

            if path.startswith("/dorequest.php"):
                with STATE.lock:
                    STATE.upstream_status = "down" if err else "ok"

            if err:
                print(f"warning: upstream {method} {path}: {err}", file=sys.stderr)
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if path.startswith("/dorequest.php") and method == "POST":
                intercept(req_body, resp_body, resp_headers.get("Content-Type", ""), cfg)

            self.send_response(status)
            for k, v in resp_headers.items():
                if k.lower() in HOP_HEADERS:
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            if resp_body:
                self.wfile.write(resp_body)

        def do_GET(self):    self._handle()
        def do_POST(self):   self._handle()
        def do_PUT(self):    self._handle()
        def do_DELETE(self): self._handle()
        def do_HEAD(self):   self._handle()

        def log_message(self, *a, **k):
            pass

    return Handler


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------- entry point ----------

RETROARCH_GRACE_SEC = 3.0  # quit if retroarch is missing this long


def _retroarch_running(match: bytes) -> bool:
    try:
        entries = os.listdir("/proc")
    except OSError:
        return False
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                if match in f.read():
                    return True
        except OSError:
            continue
    return False


def _watch_retroarch(srv: "ThreadingServer", match: str) -> None:
    if not match or not os.path.isdir("/proc"):
        return
    needle = match.encode("utf-8")
    last_seen = time.monotonic()
    while True:
        if _retroarch_running(needle):
            last_seen = time.monotonic()
        elif time.monotonic() - last_seen >= RETROARCH_GRACE_SEC:
            print(f"retroarch gone for {RETROARCH_GRACE_SEC:.0f}s; shutting down", file=sys.stderr)
            srv.shutdown()
            return
        time.sleep(0.5)


def parse_args(argv):
    p = argparse.ArgumentParser(description="RetroAchievements local proxy")
    p.add_argument("--config", default=os.path.join(ROOT, "config.json"),
                   help="path to config.json (default: alongside ra_proxy.py)")
    p.add_argument("--listen-host", help="override config.ra_proxy.listen_host")
    p.add_argument("--listen-port", type=int, help="override config.ra_proxy.listen_port")
    p.add_argument("--upstream", help="override config.ra_proxy.upstream")
    p.add_argument("--badges-dir", help="override config.ra_proxy.badges_dir")
    p.add_argument("--retroarch-process-match",
                   help="cmdline substring used to detect retroarch (empty disables watch)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    cfg = load_config(args.config)
    for k_cli in ("listen_host", "listen_port", "upstream", "badges_dir", "retroarch_process_match"):
        v = getattr(args, k_cli)
        if v is not None:
            cfg[k_cli] = v

    os.makedirs(cfg["badges_dir"], exist_ok=True)
    for i in range(2):
        threading.Thread(target=_download_worker, name=f"badge-dl-{i}", daemon=True).start()

    addr = (cfg["listen_host"], int(cfg["listen_port"]))
    srv = ThreadingServer(addr, make_handler(cfg))
    print(f"listening on http://{addr[0]}:{addr[1]} -> {cfg['upstream']}")
    print(f"badges -> {cfg['badges_dir']}")
    threading.Thread(target=_watch_retroarch, args=(srv, cfg.get("retroarch_process_match", "")),
                     name="retroarch-watcher", daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
