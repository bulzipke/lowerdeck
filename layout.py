# SPDX-License-Identifier: GPL-2.0-or-later
# Pure-data layout: no pygame imports so this stays unit-testable.

from dataclasses import dataclass


@dataclass
class Rect:
    x: int
    y: int
    w: int
    h: int

    def contains(self, px: int, py: int) -> bool:
        return self.x <= px < self.x + self.w and self.y <= py < self.y + self.h


@dataclass
class Button:
    rect: Rect
    label: str
    action: str
    icon: str = ""


@dataclass
class Layout:
    brightness_rect: Rect
    view_toggle_rect: Rect
    achievements_rect: Rect
    date_rect: Rect
    image_rect: Rect
    slot_label_rect: Rect
    load_label_rect: Rect
    buttons: list[Button]

    def hit_test(self, px: int, py: int) -> Button | None:
        for b in self.buttons:
            if b.rect.contains(px, py):
                return b
        return None


def build(w: int, h: int) -> Layout:
    """Layout:
       top strip:                Brightness slider (full width)
       left column, top half:    Resume
       left column, bottom half: Create Restore Point (save)
       right column (full height under top strip, single big button): date strip → savestate screenshot → icon+label
    """
    pad = max(8, w // 60)

    brightness_h = max(40, h // 12)
    toggle_size = brightness_h
    view_toggle = Rect(w - pad - toggle_size, pad, toggle_size, toggle_size)
    brightness = Rect(pad, pad, w - 3 * pad - toggle_size, brightness_h)

    grid_top = pad * 2 + brightness_h
    grid_h = h - grid_top - pad

    achievements = Rect(pad, grid_top, w - 2 * pad, grid_h)

    cell_w = (w - 3 * pad) // 2
    cell_h = (grid_h - pad) // 2

    resume = Button(Rect(pad, grid_top, cell_w, cell_h), "Resume Game", "resume", icon="resume")
    save = Button(Rect(pad, grid_top + pad + cell_h, cell_w, cell_h), "Create\nRestore Point", "save", icon="save")

    load_x = pad * 2 + cell_w
    load_y = grid_top
    load_w = cell_w
    load_h = grid_h
    load = Button(Rect(load_x, load_y, load_w, load_h), "Load\nRestore Point", "load")

    date_h = max(24, load_h // 12)
    label_h = max(48, load_h // 4)
    slot_h = max(56, load_h // 6)
    image_pad_x = pad
    date = Rect(load_x, load_y, load_w, date_h)
    image_y = load_y + date_h
    image_h = load_h - date_h - slot_h - label_h
    image = Rect(load_x + image_pad_x, image_y, load_w - 2 * image_pad_x, image_h)

    slot_y = image_y + image_h
    slot_btn_w = slot_h
    slot_prev_rect = Rect(load_x + image_pad_x, slot_y, slot_btn_w, slot_h)
    slot_next_rect = Rect(load_x + load_w - image_pad_x - slot_btn_w, slot_y, slot_btn_w, slot_h)
    slot_label = Rect(slot_prev_rect.x + slot_btn_w, slot_y, slot_next_rect.x - (slot_prev_rect.x + slot_btn_w), slot_h)
    slot_prev = Button(slot_prev_rect, "", "slot_prev", icon="prev")
    slot_next = Button(slot_next_rect, "", "slot_next", icon="next")

    load_label_rect = Rect(load_x, load_y + load_h - label_h, load_w, label_h)

    return Layout(
        brightness_rect=brightness,
        view_toggle_rect=view_toggle,
        achievements_rect=achievements,
        date_rect=date,
        image_rect=image,
        slot_label_rect=slot_label,
        load_label_rect=load_label_rect,
        buttons=[resume, save, slot_prev, slot_next, load],
    )
