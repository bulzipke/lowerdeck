# SPDX-License-Identifier: GPL-2.0-or-later
# Bottom-screen UI for non-vertical games on dual-screen devices.

import argparse
import collections
import ctypes
import functools
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import sdl2 as sdl
from achievements_view import AchievementsView
from brightness import BrightnessControl, BottomPanelBacklight
from layout import Rect, build as build_layout
from ra_client import RAClient
from ra_state import RAStatePoller


HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
ICONS_DIR = HERE / "assets" / "icons"

DIM_AWAKE   = 0
DIM_DIMMING = 1
DIM_DIMMED  = 2
DIM_WAKING  = 3

TEXT_CACHE_MAX = 256
ROUND_RECT_CACHE_MAX = 32


def load_config() -> dict:
    with CONFIG_PATH.open() as f:
        cfg = json.load(f)
    local = HERE / "config.local.json"
    if local.exists():
        try:
            with local.open() as f:
                overrides = json.load(f)
            if isinstance(overrides, dict):
                cfg.update(overrides)
        except (OSError, ValueError) as e:
            print(f"warning: ignoring config.local.json: {e}", file=sys.stderr)
    return cfg


class _SdlError(RuntimeError):
    pass


def _check(rc: int, what: str) -> None:
    if rc != 0:
        raise _SdlError(f"{what}: {sdl.error()}")


def _color(rgb) -> sdl.Color:
    a = rgb[3] if len(rgb) == 4 else 255
    return sdl.Color(rgb[0], rgb[1], rgb[2], a)


class UI:
    def __init__(self, rom: str, ra_pid: int | None):
        self.rom = rom
        self.ra_pid = ra_pid
        self.cfg = load_config()
        backlight_root = self.cfg.get("backlight_root", "/sys/class/backlight")
        self.ra = RAClient()
        self.brightness = BrightnessControl(backlight_root=backlight_root)
        self._brightness_pct: int = self.brightness.get_percent() or 50
        self._dragging_brightness: bool = False
        self._brightness_last_apply_ts: float = 0.0
        self.bottom_panel = BottomPanelBacklight(
            self.cfg.get("bottom_backlight_node", ""),
            backlight_root=backlight_root,
        )

        self.window = None
        self.renderer = None
        self.font_path: str = ""
        self.font_size_label = 0
        self.font_size_date = 0
        self._fonts: dict[int, int] = {}

        self.icons: dict[str, tuple] = {}

        # Optional full-screen background. Loaded from cfg["bg_image"] if the
        # file exists; falls back to cfg["bg"] solid color.
        self.bg_tex = None

        self.thumb_tex = None
        self.thumb_w = 0
        self.thumb_h = 0
        self._thumb_path: Path | None = None
        self._thumb_mtime: float = 0.0
        self._thumb_next_check: float = 0.0
        self._has_state: bool = False
        self._slot: int = 0
        self._cheevos_enabled_cache: bool | None = None

        self._states_root: Path | None = None
        self._states_dir: Path | None = None

        self._pressed: str | None = None
        self._running = False

        # Click sound — best-effort; missing tools or assets just disable audio.
        self._click_cmd: str | None = None
        self._click_path: str | None = None
        self._last_click_ms = 0
        self._click_jobs: collections.deque = collections.deque(maxlen=4)

        self._dim_state = DIM_DIMMED
        self._dim_alpha = float(self.cfg["dim_alpha"])
        self._dim_anim_start = 0.0
        self._dim_anim_from = 0.0
        self._last_input_ts = time.monotonic()
        self._swallow_next_release = False

        self._text_tex: collections.OrderedDict = collections.OrderedDict()
        self._round_rect_tex: collections.OrderedDict = collections.OrderedDict()

        self._ra_menu_open = False
        self._menu_poll_next: float = 0.0
        self._action_cooldown_until = 0.0
        self._pending: list[tuple[float, object]] = []

        self._view_mode = "vc"

        ra_cfg = self.cfg.get("ra_proxy") or {}
        proxy_host = ra_cfg.get("listen_host", "127.0.0.1")
        proxy_port = int(ra_cfg.get("listen_port", 8080))
        self._proxy_state = RAStatePoller(f"http://{proxy_host}:{proxy_port}/_ui/state", interval_sec=0.5)
        self.ach_view: AchievementsView | None = None
        self._last_tick_ts = 0.0

    # ---------- lifecycle ----------
    def run(self) -> int:
        app_id = self.cfg.get("wayland_app_id") or "lowerdeck"
        app_id_b = app_id.encode("utf-8")
        os.environ["SDL_VIDEO_WAYLAND_WMCLASS"] = app_id
        os.environ["SDL_APP_ID"] = app_id
        sdl.SetHint(sdl.HINT_APP_NAME, app_id_b)
        sdl.SetHint(sdl.HINT_VIDEO_WAYLAND_WMCLASS, app_id_b)

        _check(sdl.Init(sdl.INIT_VIDEO | sdl.INIT_EVENTS), "SDL_Init")
        if sdl.IMG_Init(sdl.IMG_INIT_PNG | sdl.IMG_INIT_JPG) == 0:
            print(f"warning: IMG_Init failed: {sdl.error()}", file=sys.stderr)
        if sdl.TTF_Init() != 0:
            print(f"warning: TTF_Init failed: {sdl.error()}", file=sys.stderr)

        self.window = sdl.CreateWindow(
            app_id_b,
            sdl.WINDOWPOS_UNDEFINED, sdl.WINDOWPOS_UNDEFINED,
            0, 0,
            sdl.WINDOW_FULLSCREEN_DESKTOP | sdl.WINDOW_BORDERLESS,
        )
        if not self.window:
            raise _SdlError(f"SDL_CreateWindow: {sdl.error()}")

        self.renderer = sdl.CreateRenderer(
            self.window, -1,
            sdl.RENDERER_ACCELERATED | sdl.RENDERER_PRESENTVSYNC,
        )
        if not self.renderer:
            raise _SdlError(f"SDL_CreateRenderer: {sdl.error()}")
        sdl.SetRenderDrawBlendMode(self.renderer, sdl.BLENDMODE_BLEND)

        sdl.SetRenderDrawColor(self.renderer, 0, 0, 0, 255)
        sdl.RenderClear(self.renderer)
        sdl.RenderPresent(self.renderer)
        sdl.Delay(50)
        ev = sdl.Event()
        while sdl.PollEvent(ctypes.byref(ev)):
            pass

        if self.bottom_panel.available:
            self.bottom_panel.power_off()

        w, h = ctypes.c_int(0), ctypes.c_int(0)
        sdl.GetWindowSize(self.window, ctypes.byref(w), ctypes.byref(h))
        self.size = (w.value, h.value)
        self.layout = build_layout(*self.size)

        self._states_root = self._resolve_states_root()
        if self._states_root is None:
            print("error: could not resolve savestate_directory from RetroArch", file=sys.stderr)
            return 2

        self._load_fonts()
        self._load_icons()
        self._load_background()
        self._refresh_thumbnail(force=True)
        self._init_audio()

        ra_cfg = self.cfg.get("ra_proxy") or {}
        self.ach_view = AchievementsView(
            renderer=self.renderer,
            cfg=self.cfg,
            font_provider=self._font,
            badges_dir=ra_cfg.get("badges_dir", ""),
            poller=self._proxy_state,
            cheevos_enabled_fn=self._cheevos_enabled,
        )
        self.ach_view.configure(
            self.layout.achievements_rect,
            self.font_size_label,
            self.font_size_date,
        )
        self._proxy_state.start()
        self._last_tick_ts = time.monotonic()

        signal.signal(signal.SIGTERM, lambda *_: self._quit())
        signal.signal(signal.SIGINT, lambda *_: self._quit())

        self._running = True
        ev = sdl.Event()
        try:
            while self._running:
                self._handle_events(ev)
                if self.ra_pid and not self._ra_alive():
                    break
                if time.monotonic() >= self._thumb_next_check:
                    self._refresh_thumbnail()
                if time.monotonic() >= self._menu_poll_next:
                    self._menu_poll_next = (time.monotonic() + float(self.cfg["menu_poll_sec"]))
                    self._poll_menu_state()
                self._tick_pending()
                self._tick_dim()
                now = time.monotonic()
                dt = max(0.0, now - self._last_tick_ts)
                self._last_tick_ts = now
                if self.ach_view is not None:
                    self.ach_view.tick(dt)
                self._draw()
        finally:
            self._teardown()
        return 0

    def _quit(self) -> None:
        self._running = False

    def _ra_alive(self) -> bool:
        try:
            os.kill(self.ra_pid, 0)
            return True
        except OSError:
            return False

    def _teardown(self) -> None:
        self._proxy_state.stop()
        if self.ach_view is not None:
            self.ach_view.teardown()
        if self.bottom_panel.available:
            self.bottom_panel.restore(self._brightness_pct)
        if self.thumb_tex:
            sdl.DestroyTexture(self.thumb_tex)
            self.thumb_tex = None
        if self.bg_tex:
            sdl.DestroyTexture(self.bg_tex)
            self.bg_tex = None
        for tex, _, _ in self.icons.values():
            sdl.DestroyTexture(tex)
        self.icons.clear()
        for entry in self._text_tex.values():
            sdl.DestroyTexture(entry["tex"])
        self._text_tex.clear()
        for tex in self._round_rect_tex.values():
            sdl.DestroyTexture(tex)
        self._round_rect_tex.clear()
        for f in self._fonts.values():
            sdl.TTF_CloseFont(f)
        self._fonts.clear()
        if self.renderer:
            sdl.DestroyRenderer(self.renderer)
        if self.window:
            sdl.DestroyWindow(self.window)
        sdl.TTF_Quit()
        sdl.IMG_Quit()
        sdl.Quit()

    # ---------- assets ----------
    def _load_fonts(self) -> None:
        fp = self.cfg.get("font_path") or ""
        if not fp or not os.path.exists(fp):
            print(f"warning: font not found at {fp!r}; text rendering disabled", file=sys.stderr)
            return
        self.font_path = fp
        h = self.size[1]
        fmin = int(self.cfg["font_min"])
        fmax = int(self.cfg["font_max"])

        def from_ratio(key: str) -> int:
            return max(fmin, min(fmax, int(h * float(self.cfg[key]))))

        self.font_size_label = from_ratio("font_ratio_label")
        self.font_size_date  = from_ratio("font_ratio_date")
        for sz in (self.font_size_label, self.font_size_date):
            self._font(sz)

    def _font(self, size: int) -> int | None:
        if not self.font_path:
            return None
        if size <= 0:
            return None
        f = self._fonts.get(size)
        if f is None:
            f = sdl.TTF_OpenFont(self.font_path.encode("utf-8"), size)
            if not f:
                return None
            self._fonts[size] = f
        return f

    @functools.lru_cache(maxsize=64)
    def _fit_label_cached(self, text: str, max_w: int, max_h: int, base_size: int, min_size: int) -> tuple:
        size = base_size
        lines_out: list[str] = []
        while size >= min_size:
            font = self._font(size)
            if font is None:
                return ([text], size)
            line_skip = sdl.TTF_FontLineSkip(font) or sdl.TTF_FontHeight(font) or size
            lines_out = []
            ok = True
            for hard in text.split("\n"):
                wrapped = self._wrap_one(font, hard, max_w)
                lines_out.extend(wrapped)
                for line in wrapped:
                    w_ptr = ctypes.c_int(0); h_ptr = ctypes.c_int(0)
                    sdl.TTF_SizeUTF8(font, line.encode("utf-8"), ctypes.byref(w_ptr), ctypes.byref(h_ptr))
                    if w_ptr.value > max_w:
                        ok = False
                        break
                if not ok:
                    break
            total_h = len(lines_out) * line_skip
            if ok and total_h <= max_h:
                return (lines_out, size)
            size -= 2
        return (lines_out or [text], max(min_size, size + 2))

    def _wrap_one(self, font: int, text: str, max_w: int) -> list[str]:
        if not text:
            return [""]
        words = text.split(" ")
        lines: list[str] = []
        cur = ""
        w_ptr = ctypes.c_int(0); h_ptr = ctypes.c_int(0)
        for word in words:
            cand = word if not cur else f"{cur} {word}"
            sdl.TTF_SizeUTF8(font, cand.encode("utf-8"), ctypes.byref(w_ptr), ctypes.byref(h_ptr))
            if w_ptr.value <= max_w or not cur:
                cur = cand
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        return lines

    def _init_audio(self) -> None:
        players = self.cfg.get("audio_players") or []
        cmd = next((shutil.which(p) for p in players if shutil.which(p)), None)
        if cmd is None:
            return
        rel = self.cfg.get("click_sound", "assets/click.wav")
        cand = Path(rel)
        if not cand.is_absolute():
            cand = HERE / rel
        if not cand.is_file():
            return
        self._click_cmd = cmd
        self._click_path = str(cand)

    def _play_click(self) -> None:
        if not self._click_cmd or not self._click_path:
            return
        debounce_ms = int(self.cfg["click_debounce_ms"])
        now_ms = int(time.monotonic() * 1000)
        if now_ms - self._last_click_ms < debounce_ms:
            return
        self._last_click_ms = now_ms
        for p in list(self._click_jobs):
            if p.poll() is not None:
                self._click_jobs.remove(p)
        try:
            vol = float(self.cfg["click_volume"])
            args = [self._click_cmd, "--playback", self._click_path, "--volume", str(vol)]
            p = subprocess.Popen(args,
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 stdin=subprocess.DEVNULL,
                                 start_new_session=True)
            self._click_jobs.append(p)
        except OSError:
            self._click_cmd = None

    def _load_icons(self) -> None:
        if not ICONS_DIR.is_dir():
            return
        for png in sorted(ICONS_DIR.glob("*.png")):
            surf = sdl.IMG_Load(str(png).encode("utf-8"))
            if not surf:
                continue
            try:
                tex = sdl.CreateTextureFromSurface(self.renderer, surf)
                if tex:
                    self.icons[png.stem] = (tex, surf.contents.w, surf.contents.h)
            finally:
                sdl.FreeSurface(surf)

    def _load_background(self) -> None:
        rel = self.cfg.get("bg_image", "")
        if not rel:
            return
        cand = Path(rel)
        if not cand.is_absolute():
            cand = HERE / rel
        if not cand.is_file():
            return
        surf = sdl.IMG_Load(str(cand).encode("utf-8"))
        if not surf:
            return
        try:
            self.bg_tex = sdl.CreateTextureFromSurface(self.renderer, surf)
        finally:
            sdl.FreeSurface(surf)

    def _resolve_states_root(self, deadline_sec: float = 3.0) -> Path | None:
        deadline = time.monotonic() + deadline_sec
        while True:
            raw = self.ra.get_savestate_directory()
            if raw and raw.startswith("/"):
                return Path(raw)
            if time.monotonic() >= deadline:
                return None
            sdl.Delay(200)

    def _discover_states_dir(self) -> Path | None:
        if self._states_root is None:
            return None
        rom_base = Path(self.rom).stem
        pattern = f"{rom_base}.state*"
        matches = list(self._states_root.glob(pattern))
        matches += list(self._states_root.glob(f"*/{pattern}"))
        if not matches:
            return None
        return matches[0].parent

    def _ensure_states_dir(self) -> Path | None:
        if self._states_dir is None:
            self._states_dir = self._discover_states_dir()
        return self._states_dir

    def _refresh_thumbnail(self, force: bool = False) -> None:
        self._thumb_next_check = (time.monotonic() + float(self.cfg["thumb_poll_sec"]))

        d = self._ensure_states_dir()
        chosen: Path | None = None
        chosen_mtime: float = 0.0
        self._has_state = False
        if d is not None:
            rom_base = Path(self.rom).stem
            slot_suffix = "" if self._slot == 0 else str(self._slot)
            state_file = d / f"{rom_base}.state{slot_suffix}"
            self._has_state = state_file.is_file()
            cand = d / f"{rom_base}.state{slot_suffix}.png"
            try:
                chosen_mtime = cand.stat().st_mtime
                chosen = cand
            except OSError:
                pass

        if chosen is None:
            if self.thumb_tex is not None:
                sdl.DestroyTexture(self.thumb_tex)
                self.thumb_tex = None
                self._thumb_path = None
                self._thumb_mtime = 0.0
            return

        unchanged = (
            not force
            and chosen == self._thumb_path
            and chosen_mtime == self._thumb_mtime
            and self.thumb_tex is not None
        )
        if unchanged:
            return

        surf = sdl.IMG_Load(str(chosen).encode("utf-8"))
        if not surf:
            return
        try:
            new_tex = sdl.CreateTextureFromSurface(self.renderer, surf)
            if not new_tex:
                return
            if self.thumb_tex is not None:
                sdl.DestroyTexture(self.thumb_tex)
            self.thumb_tex = new_tex
            self.thumb_w = surf.contents.w
            self.thumb_h = surf.contents.h
            self._thumb_path = chosen
            self._thumb_mtime = chosen_mtime
        finally:
            sdl.FreeSurface(surf)

    def _active_image(self) -> tuple | None:
        if self.thumb_tex is not None:
            return (self.thumb_tex, self.thumb_w, self.thumb_h)
        return None

    # ---------- input ----------
    def _handle_events(self, ev: sdl.Event) -> None:
        while sdl.PollEvent(ctypes.byref(ev)):
            t = ev.type
            if t == sdl.EVENT_QUIT:
                self._quit()
            elif t in (sdl.EVENT_FINGERDOWN, sdl.EVENT_FINGERUP):
                tf = ctypes.cast(ctypes.pointer(ev), ctypes.POINTER(sdl.TouchFingerEvent)).contents
                px = int(tf.x * self.size[0])
                py = int(tf.y * self.size[1])
                self._on_input(t == sdl.EVENT_FINGERDOWN, px, py)
            elif t == sdl.EVENT_FINGERMOTION:
                tf = ctypes.cast(ctypes.pointer(ev), ctypes.POINTER(sdl.TouchFingerEvent)).contents
                px = int(tf.x * self.size[0])
                py = int(tf.y * self.size[1])
                self._on_motion(px, py)
            elif t in (sdl.EVENT_MOUSEBUTTONDOWN, sdl.EVENT_MOUSEBUTTONUP):
                mb = ctypes.cast(ctypes.pointer(ev), ctypes.POINTER(sdl.MouseButtonEvent)).contents
                self._on_input(t == sdl.EVENT_MOUSEBUTTONDOWN, mb.x, mb.y)
            elif t == sdl.EVENT_MOUSEMOTION:
                mm = ctypes.cast(ctypes.pointer(ev), ctypes.POINTER(sdl.MouseMotionEvent)).contents
                self._on_motion(mm.x, mm.y)

    def _on_input(self, pressed: bool, x: int, y: int) -> None:
        was_dim = self._dim_state in (DIM_DIMMING, DIM_DIMMED, DIM_WAKING)
        if was_dim:
            if pressed:
                if self._dim_state in (DIM_DIMMED, DIM_DIMMING):
                    if self._in_wake_deadzone(x):
                        return
                    self._wake_dim()
                    self._ensure_menu(True)
                self._note_input()
                self._swallow_next_release = True
                return
            if self._swallow_next_release:
                self._swallow_next_release = False
                self._pressed = None
                self._dragging_brightness = False
                self._note_input()
                return
            return

        self._note_input()
        if pressed and self.brightness.available and self.layout.brightness_rect.contains(x, y):
            self._dragging_brightness = True
            self._set_brightness(self._brightness_x_to_pct(x))
            self._play_click()
            return
        if not pressed and self._dragging_brightness:
            self.brightness.set_percent(self._brightness_pct)
            self._brightness_last_apply_ts = time.monotonic()
            self._dragging_brightness = False
            return

        if self._view_mode == "achievements" and self.ach_view is not None:
            if pressed and self.ach_view.handle_press(x, y):
                return
            if not pressed:
                consumed, _was_tap = self.ach_view.handle_release(x, y)
                if consumed:
                    return

        self._tap(pressed, x, y)

    def _on_motion(self, x: int, y: int) -> None:
        if self._dragging_brightness:
            self._note_input()
            self._set_brightness(self._brightness_x_to_pct(x))
            return
        if self._view_mode == "achievements" and self.ach_view is not None:
            if self.ach_view.handle_motion(x, y):
                self._note_input()

    def _brightness_x_to_pct(self, x: int) -> int:
        r = self.layout.brightness_rect
        if r.w <= 0:
            return self._brightness_pct
        rel = (x - r.x) / r.w
        pct = int(round(rel * 100))
        return max(1, min(100, pct))

    def _set_brightness(self, pct: int) -> None:
        pct = max(1, min(100, pct))
        if pct == self._brightness_pct:
            return
        self._brightness_pct = pct
        now = time.monotonic()
        if now - self._brightness_last_apply_ts < float(self.cfg["brightness_throttle_sec"]):
            return
        self._brightness_last_apply_ts = now
        self.brightness.set_percent(pct)

    def _note_input(self) -> None:
        self._last_input_ts = time.monotonic()

    def _in_wake_deadzone(self, x: int) -> bool:
        w = self.size[0]
        if w <= 0:
            return False
        left = float(self.cfg.get("wake_deadzone_left_ratio", 0.0))
        right = float(self.cfg.get("wake_deadzone_right_ratio", 0.0))
        if left > 0.0 and x < w * left:
            return True
        if right > 0.0 and x >= w * (1.0 - right):
            return True
        return False

    def _wake_dim(self) -> None:
        if self._dim_state in (DIM_DIMMING, DIM_DIMMED):
            if self.bottom_panel.available:
                self.bottom_panel.restore(self._brightness_pct)
            self._dim_state = DIM_WAKING
            self._dim_anim_start = time.monotonic()
            self._dim_anim_from = self._dim_alpha

    def _tap(self, pressed: bool, x: int, y: int) -> None:
        if self.layout.view_toggle_rect.contains(x, y):
            if pressed:
                self._pressed = "toggle_view"
                self._play_click()
                return
            if self._pressed == "toggle_view":
                self._view_mode = "achievements" if self._view_mode == "vc" else "vc"
                if self.ach_view is not None and self._view_mode == "vc":
                    self.ach_view.cancel_drag()
            self._pressed = None
            return

        if self._view_mode != "vc":
            if not pressed:
                self._pressed = None
            return

        btn = self.layout.hit_test(x, y)
        if pressed:
            self._pressed = btn.action if btn else None
            if btn is not None:
                self._play_click()
            return
        if btn and self._pressed == btn.action:
            self._dispatch(btn.action)
        self._pressed = None

    def _ensure_menu(self, want_open: bool) -> None:
        actual = self.ra.get_menu_active()
        if actual is None:
            return
        if actual != self._ra_menu_open:
            self._ra_menu_open = actual
        if self._ra_menu_open == want_open:
            return
        self.ra.menu_toggle()
        self._ra_menu_open = want_open

    def _enter_dim(self) -> None:
        self._dim_state = DIM_DIMMED
        self._dim_alpha = float(self.cfg["dim_alpha"])
        self._dim_anim_start = 0.0
        idle_sec = float(self.cfg["idle_seconds"])
        self._last_input_ts = time.monotonic() - idle_sec - 1
        if self.bottom_panel.available:
            self.bottom_panel.power_off()

    def _cheevos_enabled(self) -> bool | None:
        if self._cheevos_enabled_cache is not None:
            return self._cheevos_enabled_cache
        val = self.ra.get_cheevos_enable()
        if val is None:
            return None
        self._cheevos_enabled_cache = val
        return val

    def _poll_menu_state(self) -> None:
        if self._dim_state != DIM_AWAKE:
            return
        actual = self.ra.get_menu_active()
        if actual is None or actual == self._ra_menu_open:
            return
        self._ra_menu_open = actual
        if not actual:
            self._enter_dim()

    def _defer(self, delay_sec: float, fn) -> None:
        self._pending.append((time.monotonic() + delay_sec, fn))

    def _tick_pending(self) -> None:
        if not self._pending:
            return
        now = time.monotonic()
        ready = [(d, fn) for (d, fn) in self._pending if d <= now]
        if not ready:
            return
        self._pending = [(d, fn) for (d, fn) in self._pending if d > now]
        for _, fn in ready:
            try:
                fn()
            except Exception as e:
                print(f"warning: deferred {fn} raised: {e}", file=sys.stderr)

    def _dispatch(self, action: str) -> None:
        now = time.monotonic()
        if now < self._action_cooldown_until:
            return
        self._action_cooldown_until = now + float(self.cfg["action_cooldown_sec"])

        if action == "resume":
            self._ensure_menu(False)
            self._enter_dim()
        elif action == "save":
            slot = self._slot
            self._ensure_menu(False)
            self._defer(float(self.cfg["menu_close_delay_sec"]), lambda: self.ra.save_state_slot(slot))
            self._enter_dim()
            self._states_dir = None
            self._thumb_next_check = now + float(self.cfg["thumb_post_save_sec"])
        elif action == "load":
            slot = self._slot
            self._ensure_menu(False)
            self._defer(float(self.cfg["menu_close_delay_sec"]), lambda: self.ra.load_state_slot(slot))
            self._enter_dim()
        elif action == "slot_prev":
            self._slot = (self._slot - 1) % 10
            self._thumb_next_check = 0
        elif action == "slot_next":
            self._slot = (self._slot + 1) % 10
            self._thumb_next_check = 0

    # ---------- idle dim ----------
    def _tick_dim(self) -> None:
        now = time.monotonic()
        target = float(self.cfg["dim_alpha"])
        idle_secs = float(self.cfg["idle_seconds"])
        dim_ms = max(1, int(self.cfg["dim_ms"]))
        wake_ms = max(1, int(self.cfg["wake_ms"]))

        if self._dim_state == DIM_AWAKE:
            if (now - self._last_input_ts) >= idle_secs:
                self._dim_state = DIM_DIMMING
                self._dim_anim_start = now
                self._dim_anim_from = self._dim_alpha
        elif self._dim_state == DIM_DIMMING:
            elapsed_ms = (now - self._dim_anim_start) * 1000.0
            t = min(1.0, elapsed_ms / dim_ms)
            self._dim_alpha = self._dim_anim_from + (target - self._dim_anim_from) * t
            if elapsed_ms >= dim_ms:
                self._dim_state = DIM_DIMMED
                self._dim_alpha = target
                if self.bottom_panel.available:
                    self.bottom_panel.power_off()
        elif self._dim_state == DIM_WAKING:
            elapsed_ms = (now - self._dim_anim_start) * 1000.0
            t = min(1.0, elapsed_ms / wake_ms)
            self._dim_alpha = max(0.0, self._dim_anim_from * (1.0 - t))
            if elapsed_ms >= wake_ms:
                self._dim_state = DIM_AWAKE
                self._dim_alpha = 0.0
                self._swallow_next_release = False

    def _draw_dim_overlay(self) -> None:
        if self._dim_alpha <= 0.5:
            return

        a = max(0, min(255, int(self._dim_alpha)))
        w, h = self.size

        sdl.SetRenderDrawColor(self.renderer, 0, 0, 0, a)
        full = sdl.Rect(0, 0, w, h)
        sdl.RenderFillRect(self.renderer, ctypes.byref(full))

        if self._dim_state == DIM_DIMMED:
            dim_ms = max(1, int(self.cfg["dim_ms"]))

            if self._dim_anim_start == 0.0:
                elapsed_ms = int((time.monotonic() - self._last_input_ts) * 1000)
            else:
                dimmed_start_time = self._dim_anim_start + (dim_ms / 1000.0)
                elapsed_ms = int((time.monotonic() - dimmed_start_time) * 1000)

            if elapsed_ms < 0:
                elapsed_ms = 0

            cycle_count = elapsed_ms // 4000
            if cycle_count < 3:
                cycle = elapsed_ms % 4000

                if cycle < 2000:
                    linear_ratio = cycle / 2000.0
                else:
                    linear_ratio = (4000 - cycle) / 2000.0

                ratio = linear_ratio * linear_ratio * (3.0 - 2.0 * linear_ratio)
                text_alpha = int(ratio * 255)

                if text_alpha > 0:
                    wait_text = "Tap the Touch Screen to go\nto the Virtual Console menu."
                    max_w = int(w * 0.9)
                    max_h = int(h * 0.7)
                    start_size = int(self.font_size_label * 1.2)
                    lines, used_size = self._fit_label_cached(
                        wait_text, max_w, max_h, start_size, self.font_size_date
                    )
                    font = self._font(used_size)
                    if font is not None:
                        line_skip = sdl.TTF_FontLineSkip(font) or used_size
                        total_h = len(lines) * line_skip
                        start_y = (h - total_h) // 2
                        for i, line in enumerate(lines):
                            s = self._render_text(font, line, [60, 60, 60, 255])
                            if not s:
                                continue
                            sdl.SetTextureAlphaMod(s["tex"], text_alpha)
                            dst = sdl.Rect((w - s["w"]) // 2, start_y + i * line_skip, s["w"], s["h"])
                            sdl.RenderCopy(self.renderer, s["tex"], None, ctypes.byref(dst))

        sdl.SetRenderDrawColor(self.renderer, 0, 0, 0, 255)

    # ---------- drawing ----------
    def _draw(self) -> None:
        if self.bg_tex is not None:
            sdl.RenderCopy(self.renderer, self.bg_tex, None, None)
        else:
            bg = self.cfg["bg"]
            sdl.SetRenderDrawColor(self.renderer, bg[0], bg[1], bg[2], 255)
            sdl.RenderClear(self.renderer)

        self._draw_brightness_slider()

        if self._view_mode == "vc":
            slot_actions = ("slot_prev", "slot_next")
            for b in self.layout.buttons:
                if b.action in slot_actions:
                    continue
                if b.action == "load":
                    self._draw_load_button(b)
                else:
                    self._draw_button(b, self.font_size_label)
            for b in self.layout.buttons:
                if b.action in slot_actions:
                    self._draw_button(b, self.font_size_label)
        else:
            self._draw_achievements_view()

        self._draw_view_toggle()
        self._draw_dim_overlay()

        sdl.RenderPresent(self.renderer)

    def _set_color(self, rgb: list) -> None:
        sdl.SetRenderDrawColor(self.renderer, rgb[0], rgb[1], rgb[2], 255)

    def _fill_round_rect(self, rect, radius: int, color) -> None:
        if rect.w <= 0 or rect.h <= 0:
            return
        tex = self._get_round_rect(rect.w, rect.h, int(radius), color)
        if tex is None:
            self._render_round_rect_immediate(rect, radius, color)
            return
        dst = sdl.Rect(rect.x, rect.y, rect.w, rect.h)
        sdl.RenderCopy(self.renderer, tex, None, ctypes.byref(dst))

    def _get_round_rect(self, w: int, h: int, radius: int, color):
        key = (w, h, radius, tuple(color[:3]))
        cached = self._round_rect_tex.get(key)
        if cached is not None:
            self._round_rect_tex.move_to_end(key)
            return cached
        tex = sdl.CreateTexture(self.renderer, sdl.PIXELFORMAT_ARGB8888, sdl.TEXTUREACCESS_TARGET, w, h)
        if not tex:
            return None
        sdl.SetTextureBlendMode(tex, sdl.BLENDMODE_BLEND)
        sdl.SetRenderTarget(self.renderer, tex)
        sdl.SetRenderDrawColor(self.renderer, 0, 0, 0, 0)
        sdl.RenderClear(self.renderer)
        self._render_round_rect_immediate(Rect(0, 0, w, h), radius, color)
        sdl.SetRenderTarget(self.renderer, None)
        self._round_rect_tex[key] = tex
        while len(self._round_rect_tex) > ROUND_RECT_CACHE_MAX:
            _, evicted = self._round_rect_tex.popitem(last=False)
            sdl.DestroyTexture(evicted)
        return tex

    def _render_round_rect_immediate(self, rect, radius: int, color) -> None:
        self._set_color(color)
        r = int(radius)
        w, h = rect.w, rect.h
        r = max(0, min(r, w // 2, h // 2))
        if r <= 0:
            full = sdl.Rect(rect.x, rect.y, w, h)
            sdl.RenderFillRect(self.renderer, ctypes.byref(full))
            return

        x0, y0 = rect.x, rect.y
        for sr in (
            sdl.Rect(x0 + r, y0, w - 2 * r, r),
            sdl.Rect(x0 + r, y0 + h - r, w - 2 * r, r),
            sdl.Rect(x0, y0 + r, w, h - 2 * r),
        ):
            sdl.RenderFillRect(self.renderer, ctypes.byref(sr))

        for y in range(r):
            dy = r - y - 0.5
            dx_sq = r * r - dy * dy
            if dx_sq <= 0:
                continue
            dx = int(dx_sq ** 0.5)
            if dx <= 0:
                continue
            x_left = x0 + r - dx
            x_right = x0 + w - r
            top_y = y0 + y
            bot_y = y0 + h - 1 - y
            for sr in (
                sdl.Rect(x_left, top_y, dx, 1),
                sdl.Rect(x_right, top_y, dx, 1),
                sdl.Rect(x_left, bot_y, dx, 1),
                sdl.Rect(x_right, bot_y, dx, 1),
            ):
                sdl.RenderFillRect(self.renderer, ctypes.byref(sr))

    def _press_inset(self, btn) -> int:
        if self._pressed == btn.action:
            return int(self.cfg["press_inset"])
        return 0

    def _shift(self, r: Rect, n: int) -> Rect:
        return Rect(r.x + n, r.y + n, r.w - 2 * n, r.h - 2 * n)

    def _draw_button_chrome(self, btn) -> None:
        rect = btn.rect
        n = self._press_inset(btn)
        radius = int(self.cfg["corner_radius"])
        fill_rect = self._shift(rect, n)
        fill = self.cfg["pressed"] if n else self.cfg["panel"]
        self._fill_round_rect(fill_rect, radius, fill)

    def _draw_inset_shadow(self, rect, corner_radius: int = 6, depth: int = 2) -> None:
        x, y, w, h = rect.x, rect.y, rect.w, rect.h
        cr = max(0, corner_radius)

        sdl.SetRenderDrawColor(self.renderer, 0, 0, 0, 90)
        for i in range(depth):
            sdl.RenderFillRect(self.renderer, ctypes.byref(sdl.Rect(x + cr, y + i, max(0, w - 2 * cr), 1)))
            sdl.RenderFillRect(self.renderer, ctypes.byref(sdl.Rect(x + i, y + cr, 1, max(0, h - 2 * cr))))

        sdl.SetRenderDrawColor(self.renderer, 255, 255, 255, 35)
        for i in range(depth):
            sdl.RenderFillRect(self.renderer, ctypes.byref(sdl.Rect(x + cr, y + h - 1 - i, max(0, w - 2 * cr), 1)))
            sdl.RenderFillRect(self.renderer, ctypes.byref(sdl.Rect(x + w - 1 - i, y + cr, 1, max(0, h - 2 * cr))))

    def _draw_button_content(self, btn, rect: Rect, base_size: int) -> None:
        pad = int(self.cfg["pad_inner"])
        icon = self.icons.get(btn.icon) if btn.icon else None
        label = btn.label

        if icon and not label:
            tex, iw, ih = icon
            target = int(min(rect.w, rect.h) * 0.55)
            if target > 0 and iw > 0 and ih > 0:
                scale = min(target / iw, target / ih)
                cw = max(1, int(iw * scale))
                ch = max(1, int(ih * scale))
                dst = sdl.Rect(rect.x + (rect.w - cw) // 2, rect.y + (rect.h - ch) // 2, cw, ch)
                sdl.RenderCopy(self.renderer, tex, None, ctypes.byref(dst))
            return

        if icon:
            tex, iw, ih = icon
            icon_box = max(0, min(int(rect.h * 0.6), (rect.w - 2 * pad) // 3))
            target = int(icon_box * 0.9)
            if target > 0 and iw > 0 and ih > 0:
                scale = min(target / iw, target / ih)
                cw = max(1, int(iw * scale))
                ch = max(1, int(ih * scale))
                ix = rect.x + pad + (icon_box - cw) // 2
                iy = rect.y + (rect.h - ch) // 2
                dst = sdl.Rect(ix, iy, cw, ch)
                sdl.RenderCopy(self.renderer, tex, None, ctypes.byref(dst))
                label_x_start = rect.x + pad + icon_box + pad // 2
            else:
                label_x_start = rect.x + pad
        else:
            label_x_start = rect.x + pad

        if not label or not self.font_path:
            return

        fmin = int(self.cfg["font_min"])
        label_max_w = max(1, rect.x + rect.w - pad - label_x_start)
        label_max_h = max(1, rect.h - 2 * pad)
        lines, used = self._fit_label_cached(label, label_max_w, label_max_h, base_size, fmin)
        font = self._font(used)
        if font is None:
            return
        line_skip = sdl.TTF_FontLineSkip(font) or sdl.TTF_FontHeight(font) or used
        total_h = len(lines) * line_skip
        y = rect.y + (rect.h - total_h) // 2
        for line in lines:
            s = self._render_text(font, line, self.cfg["text"])
            if not s:
                y += line_skip
                continue
            label_x = label_x_start + (label_max_w - s["w"]) // 2
            dst = sdl.Rect(label_x, y, s["w"], s["h"])
            sdl.RenderCopy(self.renderer, s["tex"], None, ctypes.byref(dst))
            y += line_skip

    def _draw_button(self, btn, base_size: int) -> None:
        self._draw_button_chrome(btn)
        n = self._press_inset(btn)
        self._draw_button_content(btn, self._shift(btn.rect, n), base_size)

    def _draw_load_button(self, btn) -> None:
        self._draw_button_chrome(btn)
        n = self._press_inset(btn)

        self._draw_text_centered(self.font_size_date,
                                 datetime.now().strftime("%m/%d/%Y"),
                                 self._shift(self.layout.date_rect, n),
                                 self.cfg["text"])

        image_rect = self._shift(self.layout.image_rect, n)
        panel = self.cfg["panel"]
        recessed = [max(0, int(c * 0.55)) for c in panel[:3]]
        inner_radius = max(2, int(self.cfg["corner_radius"]) // 2)
        self._fill_round_rect(image_rect, inner_radius, recessed)

        active = self._active_image()
        if active is not None:
            tex, iw, ih = active
            scale = min(image_rect.w / iw, image_rect.h / ih)
            cw, ch = max(1, int(iw * scale)), max(1, int(ih * scale))
            dst = sdl.Rect(image_rect.x + (image_rect.w - cw) // 2, image_rect.y + (image_rect.h - ch) // 2, cw, ch)
            sdl.RenderCopy(self.renderer, tex, None, ctypes.byref(dst))
        else:
            msg = "No Thumbnail" if self._has_state else "No State"
            self._draw_text_centered(self.font_size_label, msg, image_rect, self.cfg["text"])

        self._draw_inset_shadow(image_rect, corner_radius=inner_radius, depth=2)

        self._draw_text_centered(self.font_size_label, str(self._slot), self._shift(self.layout.slot_label_rect, n),
                                 self.cfg["text"])

        self._draw_button_content(btn, self._shift(self.layout.load_label_rect, n), self.font_size_label)

    def _draw_brightness_slider(self) -> None:
        rect = self.layout.brightness_rect
        radius = int(self.cfg["corner_radius"])

        self._fill_round_rect(rect, radius, self.cfg["panel"])

        if self.brightness.available:
            pct = max(1, min(100, self._brightness_pct))
            fill_w = max(2 * radius, int(rect.w * pct / 100.0))
            fill_rect = Rect(rect.x, rect.y, fill_w, rect.h)
            self._fill_round_rect(fill_rect, radius, self.cfg["accent"])
            label = f"Brightness {pct}%"
            color = self.cfg["text"]
        else:
            label = "Brightness N/A"
            color = self.cfg["text_dim"]

        self._draw_text_centered(self.font_size_date, label, rect, color)

    def _draw_view_toggle(self) -> None:
        rect = self.layout.view_toggle_rect
        radius = int(self.cfg["corner_radius"])
        n = int(self.cfg["press_inset"]) if self._pressed == "toggle_view" else 0
        fill_rect = self._shift(rect, n)
        fill = self.cfg["pressed"] if n else self.cfg["panel"]
        self._fill_round_rect(fill_rect, radius, fill)
        icon = self.icons.get("change")
        if icon:
            tex, iw, ih = icon
            target = int(min(fill_rect.w, fill_rect.h) * 0.55)
            if target > 0 and iw > 0 and ih > 0:
                scale = min(target / iw, target / ih)
                cw = max(1, int(iw * scale))
                ch = max(1, int(ih * scale))
                dst = sdl.Rect(
                    fill_rect.x + (fill_rect.w - cw) // 2,
                    fill_rect.y + (fill_rect.h - ch) // 2,
                    cw, ch,
                )
                sdl.RenderCopy(self.renderer, tex, None, ctypes.byref(dst))
                return
        label = "RA" if self._view_mode == "vc" else "VC"
        self._draw_text_centered(self.font_size_label, label, fill_rect, self.cfg["text"])

    def _draw_achievements_view(self) -> None:
        if self.ach_view is None:
            return
        self.ach_view.draw()

    def _draw_text_centered(self, base_size: int, text: str, rect, color) -> None:
        font = self._font(base_size)
        if font is None:
            return
        s = self._render_text(font, text, color)
        if not s:
            return
        dst = sdl.Rect(rect.x + (rect.w - s["w"]) // 2, rect.y + (rect.h - s["h"]) // 2, s["w"], s["h"])
        sdl.RenderCopy(self.renderer, s["tex"], None, ctypes.byref(dst))

    def _render_text(self, font, text: str, color) -> dict | None:
        if font is None or not text:
            return None
        key = (text, int(font), tuple(color))
        cached = self._text_tex.get(key)
        if cached is not None:
            self._text_tex.move_to_end(key)
            return cached
        surf = sdl.TTF_RenderUTF8_Blended(font, text.encode("utf-8"), _color(color))
        if not surf:
            return None
        try:
            tex = sdl.CreateTextureFromSurface(self.renderer, surf)
            if not tex:
                return None
            entry = {"tex": tex, "w": surf.contents.w, "h": surf.contents.h}
            self._text_tex[key] = entry
            while len(self._text_tex) > TEXT_CACHE_MAX:
                _, evicted = self._text_tex.popitem(last=False)
                sdl.DestroyTexture(evicted["tex"])
            return entry
        finally:
            sdl.FreeSurface(surf)


def parse_args(argv):
    p = argparse.ArgumentParser()
    p.add_argument("--rom", required=True)
    p.add_argument("--ra-pid", type=int, default=0)
    p.add_argument("--core", default="")
    p.add_argument("--platform", default="")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    os.environ.setdefault("SDL_VIDEODRIVER", "wayland")
    ui = UI(args.rom, args.ra_pid or None)
    return ui.run()


if __name__ == "__main__":
    sys.exit(main())
