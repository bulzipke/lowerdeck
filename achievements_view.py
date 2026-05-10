# SPDX-License-Identifier: GPL-2.0-or-later
# Achievements list view: SDL list with badges, inertial touch scroll.

import collections
import ctypes
import math
import os
import ssl
import threading
import time
import urllib.request
from datetime import datetime

import sdl2 as sdl
from layout import Rect

def _color(rgb) -> sdl.Color:
    a = rgb[3] if len(rgb) == 4 else 255
    return sdl.Color(rgb[0], rgb[1], rgb[2], a)


class AchievementsView:
    TAP_THRESHOLD_PX = 12
    INERTIA_DECAY = 4.5
    MIN_VELOCITY = 8.0
    TEXT_CACHE_MAX = 256

    def __init__(self, renderer, cfg, font_provider, badges_dir, poller, cheevos_enabled_fn=None):
        self.renderer = renderer
        self.cfg = cfg
        self._font = font_provider
        self.badges_dir = badges_dir
        self._poller = poller
        self._cheevos_enabled_fn = cheevos_enabled_fn

        self.rect = Rect(0, 0, 0, 0)

        self.scroll_y = 0.0
        self.velocity = 0.0
        self._dragging = False
        self._drag_start_y = 0
        self._drag_start_scroll = 0.0
        self._drag_last_y = 0
        self._drag_last_t = 0.0
        self._drag_total_dy = 0

        self._badge_tex: dict[tuple[str, bool], tuple | None] = {}
        self._text_tex: "collections.OrderedDict[tuple, tuple]" = collections.OrderedDict()
        self._last_game_id = None

        self._icon_url = ""
        self._icon_bytes: bytes | None = None
        self._icon_bytes_lock = threading.Lock()
        self._icon_tex: tuple | None = None  # (tex, w, h)

        self._marquee_t0 = 0.0

        self.size_title = 0
        self.size_desc = 0
        self.size_footer = 0

    # ---------- lifecycle ----------

    def configure(self, rect: Rect, font_size_label: int, font_size_date: int) -> None:
        self.rect = rect
        self.size_title = max(font_size_date, int(font_size_label * 0.92))
        self.size_desc = max(font_size_date, int(font_size_label * 0.72))
        self.size_footer = font_size_date

    def teardown(self) -> None:
        for entry in self._badge_tex.values():
            if entry is not None:
                sdl.DestroyTexture(entry[0])
        self._badge_tex.clear()
        for tex, _, _ in self._text_tex.values():
            sdl.DestroyTexture(tex)
        self._text_tex.clear()
        if self._icon_tex is not None:
            sdl.DestroyTexture(self._icon_tex[0])
            self._icon_tex = None

    # ---------- input ----------

    def hit(self, x: int, y: int) -> bool:
        return self.rect.contains(x, y)

    def handle_press(self, x: int, y: int) -> bool:
        if not self.hit(x, y):
            return False
        self._dragging = True
        self._drag_start_y = y
        self._drag_last_y = y
        self._drag_last_t = time.monotonic()
        self._drag_start_scroll = self.scroll_y
        self._drag_total_dy = 0
        self.velocity = 0.0
        return True

    def handle_motion(self, x: int, y: int) -> bool:
        if not self._dragging:
            return False
        now = time.monotonic()
        dy = y - self._drag_start_y
        self._drag_total_dy = max(self._drag_total_dy, abs(dy))
        self.scroll_y = self._clamp(self._drag_start_scroll - dy)
        dt = now - self._drag_last_t
        if dt > 0.001:
            self.velocity = -(y - self._drag_last_y) / dt
        self._drag_last_y = y
        self._drag_last_t = now
        return True

    def handle_release(self, x: int, y: int) -> tuple[bool, bool]:
        if not self._dragging:
            return False, False
        self._dragging = False
        was_tap = self._drag_total_dy < self.TAP_THRESHOLD_PX
        if was_tap:
            self.velocity = 0.0
        return True, was_tap

    def cancel_drag(self) -> None:
        self._dragging = False
        self.velocity = 0.0

    # ---------- per-frame ----------

    def tick(self, dt: float) -> None:
        state = self._poller.get()
        gid = (state.get("game") or {}).get("id")
        if gid != self._last_game_id:
            self._last_game_id = gid
            self.scroll_y = 0.0
            self.velocity = 0.0
            self._marquee_t0 = time.monotonic()
            self._drop_caches()

        icon_url = (state.get("game") or {}).get("icon_url") or ""
        if icon_url != self._icon_url:
            self._icon_url = icon_url
            with self._icon_bytes_lock:
                self._icon_bytes = None
            if self._icon_tex is not None:
                sdl.DestroyTexture(self._icon_tex[0])
                self._icon_tex = None
            if icon_url:
                threading.Thread(target=self._fetch_icon, args=(icon_url,), name="ach-icon-fetch", daemon=True).start()
        if self._icon_tex is None:
            with self._icon_bytes_lock:
                pending = self._icon_bytes
                self._icon_bytes = None
            if pending:
                self._icon_tex = self._texture_from_png_bytes(pending)

        if self._dragging or dt <= 0:
            return
        if abs(self.velocity) < self.MIN_VELOCITY:
            self.velocity = 0.0
            return
        self.scroll_y += self.velocity * dt
        max_s = self._max_scroll(state)
        if self.scroll_y < 0:
            self.scroll_y = 0.0
            self.velocity = 0.0
        elif self.scroll_y > max_s:
            self.scroll_y = max_s
            self.velocity = 0.0
        else:
            self.velocity *= math.exp(-self.INERTIA_DECAY * dt)

    # ---------- drawing ----------

    def draw(self) -> None:
        cfg = self.cfg
        radius = int(cfg["corner_radius"])
        self._fill_round_rect(self.rect, radius, cfg["panel"])

        if self._cheevos_enabled_fn is not None:
            if self._cheevos_enabled_fn() is False:
                self._draw_centered_message(self.rect, "RetroAchievements is disabled.")
                return

        state = self._poller.get()
        achievements = state.get("achievements") or []
        unlocks = state.get("unlocks") or {}
        game = state.get("game") or {}

        if state.get("login_status") == "failed":
            self._draw_centered_message(self.rect, "RetroAchievements login failed.")
            return

        if state.get("upstream_status") == "down" and not achievements:
            self._draw_centered_message(
                self.rect,
                "Failed to connect to RetroAchievements.\nNo internet connection.",
            )
            return

        header_h = self._header_height()
        header_rect = Rect(self.rect.x, self.rect.y, self.rect.w, header_h)

        list_y = self.rect.y + header_h
        list_h = self.rect.h - header_h
        body_rect = Rect(self.rect.x, list_y, self.rect.w, list_h)

        if "unsupported" in (game.get("title") or "").lower():
            self._draw_header(header_rect, game, achievements, unlocks, state, score_only=True)
            self._draw_centered_message(body_rect, "Unsupported Game")
            return

        self._draw_header(header_rect, game, achievements, unlocks, state)

        if not achievements:
            if game.get("id") and list_h > 0:
                self._draw_empty_state(body_rect, "No achievements")
            return

        ordered = self._sort_by_unlock(achievements, unlocks)

        row_h = self._row_height()
        first = max(0, int(self.scroll_y // row_h))
        last = min(len(ordered) - 1, int((self.scroll_y + list_h) // row_h))

        scrollbar_w = max(4, self.rect.w // 80)
        list_w = self.rect.w - scrollbar_w
        list_clip = sdl.Rect(self.rect.x, list_y, list_w, list_h)
        sdl.RenderSetClipRect(self.renderer, ctypes.byref(list_clip))

        for i in range(first, last + 1):
            ach = ordered[i]
            top = list_y + int(i * row_h - self.scroll_y)
            row_rect = Rect(self.rect.x, top, list_w, int(row_h))
            unlock_info = unlocks.get(str(ach.get("id"))) or unlocks.get(ach.get("id"))
            self._draw_row(ach, row_rect, unlock_info)
            sep_y = top + int(row_h) - 1
            if sep_y < list_y + list_h:
                sdl.SetRenderDrawColor(self.renderer, 0, 0, 0, 80)
                sep = sdl.Rect(self.rect.x + 8, sep_y, list_w - 16, 1)
                sdl.RenderFillRect(self.renderer, ctypes.byref(sep))

        sdl.RenderSetClipRect(self.renderer, None)

        self._draw_scrollbar(list_y, list_h, len(achievements), row_h, scrollbar_w)

    # ---------- internals ----------

    @staticmethod
    def _sort_by_unlock(achievements: list, unlocks: dict) -> list:
        locked, unlocked = [], []
        for a in achievements:
            aid = a.get("id")
            is_unlocked = (str(aid) in unlocks) or (aid in unlocks)
            (unlocked if is_unlocked else locked).append(a)
        return locked + unlocked

    def _row_height(self) -> int:
        return max(72, int(self.rect.h * 0.22))

    def _header_height(self) -> int:
        return max(40, int(self.rect.h * 0.12))

    def _max_scroll(self, state: dict | None = None) -> float:
        state = state if state is not None else self._poller.get()
        achievements = state.get("achievements") or []
        list_h = self.rect.h - self._header_height()
        total = len(achievements) * self._row_height()
        return max(0.0, total - list_h)

    def _clamp(self, s: float) -> float:
        return max(0.0, min(s, self._max_scroll()))

    def _drop_caches(self) -> None:
        for entry in self._badge_tex.values():
            if entry is not None:
                sdl.DestroyTexture(entry[0])
        self._badge_tex.clear()
        for tex, _, _ in self._text_tex.values():
            sdl.DestroyTexture(tex)
        self._text_tex.clear()

    def _fetch_icon(self, url: str) -> None:
        try:
            ctx = ssl.create_default_context()
            req = urllib.request.Request(url, headers={"User-Agent": "ra-proxy/1"})
            with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
                data = r.read()
        except Exception:
            return
        with self._icon_bytes_lock:
            if self._icon_url == url:
                self._icon_bytes = data

    def _texture_from_png_bytes(self, data: bytes):
        size = len(data)
        if size <= 0:
            return None
        buf = (ctypes.c_ubyte * size).from_buffer_copy(data)
        rw = sdl.RWFromConstMem(buf, size)
        if not rw:
            return None
        surf = sdl.IMG_Load_RW(rw, 1)
        if not surf:
            return None
        try:
            tex = sdl.CreateTextureFromSurface(self.renderer, surf)
            if not tex:
                return None
            return (tex, surf.contents.w, surf.contents.h)
        finally:
            sdl.FreeSurface(surf)

    def _draw_header(self, rect: Rect, game: dict, achievements: list,
                     unlocks: dict, state: dict, score_only: bool = False) -> None:
        cfg = self.cfg
        pad = int(cfg["pad_inner"])
        score = (state.get("user") or {}).get("score", 0)

        if score_only:
            info = f"{score} pts"
        else:
            info = f"{len(unlocks)} / {len(achievements)}   ·   {score} pts"
        info_tex = self._render_text(info, self.size_desc, cfg["text_dim"])
        info_w = info_tex[1] if info_tex else 0

        if score_only:
            if info_tex:
                tex, w, h = info_tex
                dst = sdl.Rect(rect.x + rect.w - pad - w, rect.y + (rect.h - h) // 2, w, h)
                sdl.RenderCopy(self.renderer, tex, None, ctypes.byref(dst))
            self._draw_header_border(rect)
            return

        title = (game.get("title") or "").strip()
        if not title:
            err = self._poller.error
            sub = "Waiting for RetroArch..." if err else "No game loaded"
            self._blit_text(sub, self.size_desc, cfg["text_dim"],
                            rect.x + pad, rect.y + (rect.h - self._line_skip(self.size_desc)) // 2)
            self._draw_header_border(rect)
            return

        icon_box = max(0, rect.h - 2 * (pad // 2))
        icon_x = rect.x + pad // 2
        icon_y = rect.y + (rect.h - icon_box) // 2
        if self._icon_tex is not None and icon_box > 0:
            tex, iw, ih = self._icon_tex
            scale = min(icon_box / iw, icon_box / ih) if iw and ih else 0
            cw = max(1, int(iw * scale))
            ch = max(1, int(ih * scale))
            dst = sdl.Rect(icon_x + (icon_box - cw) // 2,
                           icon_y + (icon_box - ch) // 2, cw, ch)
            sdl.RenderCopy(self.renderer, tex, None, ctypes.byref(dst))

        title_x = icon_x + icon_box + (pad if icon_box > 0 else 0)
        title_right = rect.x + rect.w - pad - info_w - pad
        title_max_w = max(0, title_right - title_x)
        ts = self._line_skip(self.size_title)
        title_y = rect.y + (rect.h - ts) // 2
        self._draw_marquee_text(title, self.size_title, cfg["text"], title_x, title_y, title_max_w, ts)

        if info_tex:
            tex, w, h = info_tex
            dst = sdl.Rect(rect.x + rect.w - pad - w, rect.y + (rect.h - h) // 2, w, h)
            sdl.RenderCopy(self.renderer, tex, None, ctypes.byref(dst))

        self._draw_header_border(rect)

    def _draw_header_border(self, rect: Rect) -> None:
        sdl.SetRenderDrawColor(self.renderer, 0, 0, 0, 110)
        b = sdl.Rect(rect.x + 6, rect.y + rect.h - 1, rect.w - 12, 1)
        sdl.RenderFillRect(self.renderer, ctypes.byref(b))

    def _draw_marquee_text(self, text: str, size: int, color, x: int, y: int, max_w: int, line_h: int) -> None:
        if max_w <= 0:
            return
        entry = self._render_text(text, size, color)
        if not entry:
            return
        tex, tw, th = entry
        if tw <= max_w:
            dst = sdl.Rect(x, y, tw, th)
            sdl.RenderCopy(self.renderer, tex, None, ctypes.byref(dst))
            return

        gap = max(20, line_h)
        period = tw + gap
        speed = max(20, int(line_h * 1.2))  # px/sec
        offset = ((time.monotonic() - self._marquee_t0) * speed) % period

        col_left = x
        col_right = x + max_w
        for copy_offset in (0, period):
            sx = x - int(offset) + copy_offset
            v_start = max(sx, col_left)
            v_end = min(sx + tw, col_right)
            if v_end <= v_start:
                continue
            src = sdl.Rect(v_start - sx, 0, v_end - v_start, th)
            dst = sdl.Rect(v_start, y, v_end - v_start, th)
            sdl.RenderCopy(self.renderer, tex, ctypes.byref(src), ctypes.byref(dst))


    def _draw_empty_state(self, rect: Rect, msg: str) -> None:
        size = self.size_desc
        h = self._line_skip(size)
        self._blit_text(msg, size, self.cfg["text_dim"],
                        rect.x + (rect.w - 240) // 2,
                        rect.y + (rect.h - h) // 2,
                        max_w=240)

    def _draw_centered_message(self, rect: Rect, text: str) -> None:
        if rect.w <= 0 or rect.h <= 0:
            return
        size = max(self.size_footer, int(self.size_title * 1.3))
        line_skip = self._line_skip(size)
        lines = text.split("\n")
        total_h = len(lines) * line_skip
        start_y = rect.y + (rect.h - total_h) // 2
        for i, line in enumerate(lines):
            entry = self._render_text(line, size, self.cfg["text_dim"])
            if not entry:
                continue
            tex, w, h = entry
            dst = sdl.Rect(
                rect.x + (rect.w - w) // 2,
                start_y + i * line_skip,
                w, h,
            )
            sdl.RenderCopy(self.renderer, tex, None, ctypes.byref(dst))

    def _draw_row(self, ach: dict, row_rect: Rect, unlock_info: dict | None) -> None:
        cfg = self.cfg
        pad = int(cfg["pad_inner"])
        locked = unlock_info is None

        badge_size = row_rect.h - pad
        badge_x = row_rect.x + pad // 2
        badge_y = row_rect.y + (row_rect.h - badge_size) // 2
        self._draw_badge(ach.get("badge") or "", locked, badge_x, badge_y, badge_size)

        text_x = badge_x + badge_size + pad
        text_right = row_rect.x + row_rect.w - pad
        text_w = max(0, text_right - text_x)
        if text_w <= 0:
            return

        text_color = cfg["text"] if not locked else cfg["text_dim"]
        dim_color = cfg["text_dim"]

        ts = self._line_skip(self.size_title)
        ds = self._line_skip(self.size_desc)
        fs = self._line_skip(self.size_footer)
        y = row_rect.y + max(2, (row_rect.h - (ts + ds + fs)) // 2)

        points = ach.get("points")
        self._draw_split_line(ach.get("title", ""), str(points) if points else "",
                              self.size_title, text_color, text_x, y, text_w)
        y += ts

        desc = ach.get("desc", "")
        if desc:
            self._draw_marquee_text(ach.get("desc", ""), self.size_desc, text_color, text_x, y, text_w, ds)
        y += ds

        label, value = self._format_footer(ach, unlock_info)
        self._draw_split_line(label, value, self.size_footer, dim_color, text_x, y, text_w)

    def _draw_split_line(self, left_text: str, right_text: str, size: int,
                         color, x: int, y: int, total_w: int) -> None:
        right_w = right_h = 0
        right_entry = None
        if right_text:
            right_entry = self._render_text(right_text, size, color)
            if right_entry:
                _, right_w, right_h = right_entry

        gap = max(8, size // 4) if right_w else 0
        left_max_w = max(0, total_w - right_w - gap)
        if left_text and left_max_w > 0:
            self._blit_text(left_text, size, color, x, y, max_w=left_max_w)

        if right_entry and right_w > 0:
            tex, _, _ = right_entry
            ry = y + (self._line_skip(size) - right_h) // 2
            dst = sdl.Rect(x + total_w - right_w, ry, right_w, right_h)
            sdl.RenderCopy(self.renderer, tex, None, ctypes.byref(dst))

    def _format_footer(self, ach: dict, unlock_info: dict | None) -> tuple[str, str]:
        if unlock_info:
            when = unlock_info.get("when")
            if when:
                try:
                    return ("Unlocked",
                            datetime.fromtimestamp(int(when)).strftime("%b %d %Y, %I:%M%p"))
                except (OverflowError, OSError, ValueError):
                    pass
            return ("Unlocked", "")
        r = ach.get("rarity_hardcore") or ach.get("rarity")
        if r:
            return ("Locked", f"{r:.1f}% have this")
        return ("Locked", "")

    def _draw_badge(self, name: str, locked: bool, x: int, y: int, size: int) -> None:
        if size <= 0:
            return
        panel = self.cfg["panel"]
        recessed = [max(0, int(c * 0.6)) for c in panel[:3]]
        sdl.SetRenderDrawColor(self.renderer, recessed[0], recessed[1], recessed[2], 255)
        bg = sdl.Rect(x, y, size, size)
        sdl.RenderFillRect(self.renderer, ctypes.byref(bg))

        entry = self._load_badge(name, locked)
        if entry is None:
            return
        tex, iw, ih = entry
        if iw <= 0 or ih <= 0:
            return
        scale = min(size / iw, size / ih)
        w = max(1, int(iw * scale))
        h = max(1, int(ih * scale))
        dst = sdl.Rect(x + (size - w) // 2, y + (size - h) // 2, w, h)
        sdl.RenderCopy(self.renderer, tex, None, ctypes.byref(dst))

    def _load_badge(self, name: str, locked: bool):
        if not name:
            return None
        key = (name, locked)
        cached = self._badge_tex.get(key)
        if cached is not None:
            return cached
        suffix = "_lock.png" if locked else ".png"
        path = os.path.join(self.badges_dir, name + suffix)
        if not os.path.exists(path):
            return None
        surf = sdl.IMG_Load(path.encode("utf-8"))
        if not surf:
            return None
        try:
            tex = sdl.CreateTextureFromSurface(self.renderer, surf)
            if not tex:
                return None
            entry = (tex, surf.contents.w, surf.contents.h)
            self._badge_tex[key] = entry
            return entry
        finally:
            sdl.FreeSurface(surf)

    def _draw_scrollbar(self, list_y: int, list_h: int, n_rows: int, row_h: float, bar_w: int) -> None:
        if list_h <= 0 or n_rows <= 0:
            return
        content_h = n_rows * row_h
        if content_h <= list_h:
            return
        x = self.rect.x + self.rect.w - bar_w
        sdl.SetRenderDrawColor(self.renderer, 0, 0, 0, 60)
        track = sdl.Rect(x, list_y, bar_w, list_h)
        sdl.RenderFillRect(self.renderer, ctypes.byref(track))
        ratio = list_h / content_h
        thumb_h = max(20, int(list_h * ratio))
        max_scroll = content_h - list_h
        t = self.scroll_y / max_scroll if max_scroll > 0 else 0
        thumb_y = list_y + int((list_h - thumb_h) * t)
        accent = self.cfg["accent"]
        sdl.SetRenderDrawColor(self.renderer, accent[0], accent[1], accent[2], 220)
        thumb = sdl.Rect(x, thumb_y, bar_w, thumb_h)
        sdl.RenderFillRect(self.renderer, ctypes.byref(thumb))

    def _line_skip(self, size: int) -> int:
        font = self._font(size)
        if not font:
            return size
        return sdl.TTF_FontLineSkip(font) or sdl.TTF_FontHeight(font) or size

    def _render_text(self, text: str, size: int, color):
        if not text:
            return None
        key = (text, size, tuple(color))
        cached = self._text_tex.get(key)
        if cached is not None:
            self._text_tex.move_to_end(key)
            return cached
        font = self._font(size)
        if not font:
            return None
        surf = sdl.TTF_RenderUTF8_Blended(font, text.encode("utf-8"), _color(color))
        if not surf:
            return None
        try:
            tex = sdl.CreateTextureFromSurface(self.renderer, surf)
            if not tex:
                return None
            entry = (tex, surf.contents.w, surf.contents.h)
            self._text_tex[key] = entry
            while len(self._text_tex) > self.TEXT_CACHE_MAX:
                _, evicted = self._text_tex.popitem(last=False)
                sdl.DestroyTexture(evicted[0])
            return entry
        finally:
            sdl.FreeSurface(surf)

    def _blit_text(self, text: str, size: int, color, x: int, y: int, max_w: int | None = None) -> None:
        entry = self._render_text(text, size, color)
        if entry is None:
            return
        tex, w, h = entry
        if max_w is not None and max_w < w:
            src = sdl.Rect(0, 0, max_w, h)
            dst = sdl.Rect(x, y, max_w, h)
            sdl.RenderCopy(self.renderer, tex, ctypes.byref(src), ctypes.byref(dst))
        else:
            dst = sdl.Rect(x, y, w, h)
            sdl.RenderCopy(self.renderer, tex, None, ctypes.byref(dst))

    def _fill_round_rect(self, rect: Rect, radius: int, color) -> None:
        sdl.SetRenderDrawColor(self.renderer, color[0], color[1], color[2], 255)
        r = max(0, min(radius, rect.w // 2, rect.h // 2))
        if r <= 0:
            full = sdl.Rect(rect.x, rect.y, rect.w, rect.h)
            sdl.RenderFillRect(self.renderer, ctypes.byref(full))
            return
        x0, y0, w, h = rect.x, rect.y, rect.w, rect.h
        for sr in (
            sdl.Rect(x0 + r, y0, w - 2 * r, r),
            sdl.Rect(x0 + r, y0 + h - r, w - 2 * r, r),
            sdl.Rect(x0, y0 + r, w, h - 2 * r),
        ):
            sdl.RenderFillRect(self.renderer, ctypes.byref(sr))
        for yy in range(r):
            dy = r - yy - 0.5
            dx_sq = r * r - dy * dy
            if dx_sq <= 0:
                continue
            dx = int(dx_sq ** 0.5)
            if dx <= 0:
                continue
            xl = x0 + r - dx
            xr = x0 + w - r
            ty = y0 + yy
            by = y0 + h - 1 - yy
            for sr in (
                sdl.Rect(xl, ty, dx, 1),
                sdl.Rect(xr, ty, dx, 1),
                sdl.Rect(xl, by, dx, 1),
                sdl.Rect(xr, by, dx, 1),
            ):
                sdl.RenderFillRect(self.renderer, ctypes.byref(sr))
