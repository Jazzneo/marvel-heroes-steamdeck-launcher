"""Tiny controller-friendly UI toolkit on top of pygame.

Screens rebuild their widget list every frame (immediate-mode style); focus is
remembered by widget key, so state never goes stale.  Navigation is spatial:
D-pad / arrow keys move to the nearest widget in that direction.
"""
import math
import time

import pygame

W, H = 1280, 800

# ----------------------------------------------------------------- theme
C = {
    "bg": (13, 14, 19),
    "bg2": (19, 21, 28),
    "panel": (24, 27, 35),
    "panel2": (32, 36, 47),
    "line": (46, 51, 66),
    "text": (240, 241, 245),
    "muted": (150, 157, 175),
    "dim": (95, 101, 118),
    "accent": (226, 36, 41),
    "accent_hi": (255, 70, 72),
    "accent_dk": (150, 22, 28),
    "gold": (244, 186, 64),
    "green": (52, 199, 120),
    "yellow": (245, 184, 61),
    "blue": (82, 163, 255),
    "red": (240, 76, 76),
    "focus": (255, 255, 255),
}

_FONT_CACHE = {}
_FONT_NAMES = "notosans,notosansui,cantarell,dejavusans,liberationsans,freesans,arial"


def font(size, bold=False):
    key = (size, bold)
    f = _FONT_CACHE.get(key)
    if f is None:
        try:
            f = pygame.font.SysFont(_FONT_NAMES, size, bold=bold)
        except Exception:
            f = None
        if f is None:
            f = pygame.font.Font(None, int(size * 1.35))
            f.set_bold(bold)
        _FONT_CACHE[key] = f
    return f


def text(surf, s, pos, size=20, color=None, bold=False, anchor="topleft", max_w=None):
    f = font(size, bold)
    s = str(s)
    if max_w and f.size(s)[0] > max_w:
        while s and f.size(s + "…")[0] > max_w:
            s = s[:-1]
        s += "…"
    img = f.render(s, True, color or C["text"])
    r = img.get_rect(**{anchor: pos})
    surf.blit(img, r)
    return r


def wrap(s, size, width, bold=False):
    f = font(size, bold)
    lines = []
    for para in str(s).split("\n"):
        words, cur = para.split(" "), ""
        for w in words:
            t = (cur + " " + w).strip()
            if f.size(t)[0] <= width or not cur:
                cur = t
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


def text_block(surf, s, pos, width, size=18, color=None, bold=False, spacing=6):
    x, y = pos
    lh = font(size, bold).get_linesize()
    for line in wrap(s, size, width, bold):
        text(surf, line, (x, y), size, color, bold)
        y += lh + spacing - 4
    return y


def rrect(surf, color, rect, radius=12, width=0):
    pygame.draw.rect(surf, color, rect, width, border_radius=radius)


def panel(surf, rect, color=None, radius=16, border=True):
    rrect(surf, color or C["panel"], rect, radius)
    if border:
        rrect(surf, C["line"], rect, radius, 1)


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def status_dot(surf, color, center, r=7, pulse=False):
    if pulse:
        k = (math.sin(time.time() * 4) + 1) / 2
        pygame.draw.circle(surf, lerp(C["bg"], color, 0.25 + 0.25 * k), center, r + 5)
    pygame.draw.circle(surf, color, center, r)


# ---------------------------------------------------------- controller glyphs
GLYPH_COL = {"A": (52, 199, 120), "B": (240, 76, 76), "X": (82, 163, 255), "Y": (245, 184, 61)}


def glyph(surf, name, center):
    """Draw a controller button glyph (A/B/X/Y circle or LB/RB/≡ pill)."""
    x, y = center
    if name in GLYPH_COL:
        pygame.draw.circle(surf, GLYPH_COL[name], center, 13)
        text(surf, name, (x, y + 1), 16, C["bg"], True, "center")
        return 14
    if name == "SELECT":  # Steam Deck "View" button: two overlapping squares
        r = pygame.Rect(0, 0, 30, 24)
        r.center = center
        rrect(surf, C["panel2"], r, 12)
        rrect(surf, C["line"], r, 12, 1)
        pygame.draw.rect(surf, C["text"], (x - 6, y - 6, 9, 9), 2, border_radius=2)
        pygame.draw.rect(surf, C["text"], (x - 2, y - 2, 9, 9), 0, border_radius=2)
        return 15
    label = {"START": "≡", "DPAD": "✛"}.get(name, name)
    w = max(30, font(14, True).size(label)[0] + 16)
    r = pygame.Rect(0, 0, w, 24)
    r.center = center
    rrect(surf, C["panel2"], r, 12)
    rrect(surf, C["line"], r, 12, 1)
    text(surf, label, r.center, 14, C["text"], True, "center")
    return w // 2


# ----------------------------------------------------------------- widgets
class Widget:
    focusable = True
    group = "content"

    def __init__(self, key, rect, enabled=True):
        self.key = key
        self.rect = pygame.Rect(rect)
        self.enabled = enabled

    def draw(self, surf, focused):
        pass

    def activate(self, app):
        pass

    def nav(self, app, direction):
        """Return True if the widget consumed a direction press."""
        return False

    def alt(self, app, button):
        return False

    def scroll(self, app, amount):
        return False

    def click(self, app, pos):
        self.activate(app)


def focus_ring(surf, rect, radius=14):
    k = (math.sin(time.time() * 5) + 1) / 2
    col = lerp(C["accent"], C["focus"], 0.55 + 0.45 * k)
    rrect(surf, col, rect.inflate(8, 8), radius + 4, 3)


class Button(Widget):
    def __init__(self, key, rect, label, on_click=None, style="secondary", enabled=True,
                 sub=None, icon=None, size=22, align="center"):
        super().__init__(key, rect, enabled)
        self.label, self.on_click, self.style, self.sub, self.icon = label, on_click, style, sub, icon
        self.size, self.align = size, align

    def draw(self, surf, focused):
        st = self.style
        if not self.enabled:
            bg, fg = C["panel"], C["dim"]
        elif st == "primary":
            bg, fg = (C["accent_hi"] if focused else C["accent"]), C["text"]
        elif st == "danger":
            bg, fg = ((120, 30, 34) if focused else (84, 24, 28)), C["text"]
        elif st == "success":
            bg, fg = ((36, 150, 92) if focused else (28, 118, 74)), C["text"]
        elif st == "ghost":
            bg, fg = ((44, 49, 64) if focused else C["panel2"]), C["text"]
        else:
            bg, fg = ((56, 62, 80) if focused else (40, 45, 59)), C["text"]
        rrect(surf, bg, self.rect, 14)
        if st == "ghost" and self.enabled:
            rrect(surf, C["line"], self.rect, 14, 1)
        if st == "primary" and self.enabled:
            # subtle top highlight
            hl = self.rect.copy()
            hl.height = max(6, hl.height // 2)
            s = pygame.Surface(hl.size, pygame.SRCALPHA)
            rrect(s, (255, 255, 255, 22), s.get_rect(), 14)
            surf.blit(s, hl)
        if focused:
            focus_ring(surf, self.rect)
        cx = self.rect.centerx if self.align == "center" else self.rect.x + 22
        anchor = "center" if self.align == "center" else "midleft"
        if self.sub:
            text(surf, self.label, (cx, self.rect.centery - 12), self.size, fg, True, anchor,
                 self.rect.w - 30)
            text(surf, self.sub, (cx, self.rect.centery + 15), 15,
                 C["muted"] if st != "primary" else (255, 220, 220), False, anchor, self.rect.w - 30)
        else:
            text(surf, self.label, (cx, self.rect.centery), self.size, fg, True, anchor, self.rect.w - 30)

    def activate(self, app):
        if self.enabled and self.on_click:
            self.on_click()


class NavItem(Button):
    group = "nav"

    def __init__(self, key, rect, label, on_click, active, badge=None):
        super().__init__(key, rect, label, on_click, "ghost", align="left", size=21)
        self.active, self.badge = active, badge

    def draw(self, surf, focused):
        if self.active:
            rrect(surf, C["panel2"], self.rect, 12)
            bar = pygame.Rect(self.rect.x, self.rect.y + 10, 4, self.rect.h - 20)
            rrect(surf, C["accent"], bar, 2)
        elif focused:
            rrect(surf, C["panel"], self.rect, 12)
        if focused:
            focus_ring(surf, self.rect, 12)
        col = C["text"] if (self.active or focused) else C["muted"]
        text(surf, self.label, (self.rect.x + 24, self.rect.centery), 21, col, self.active, "midleft")
        if self.badge:
            status_dot(surf, self.badge, (self.rect.right - 20, self.rect.centery), 5)


class Toggle(Widget):
    def __init__(self, key, rect, label, value, on_change, sub=None, enabled=True):
        super().__init__(key, rect, enabled)
        self.label, self.value, self.on_change, self.sub = label, value, on_change, sub

    def draw(self, surf, focused):
        rrect(surf, C["panel2"] if focused else C["panel"], self.rect, 12)
        if focused:
            focus_ring(surf, self.rect, 12)
        col = C["text"] if self.enabled else C["dim"]
        if self.sub:
            text(surf, self.label, (self.rect.x + 18, self.rect.centery - 11), 19, col, False, "midleft",
                 self.rect.w - 110)
            text(surf, self.sub, (self.rect.x + 18, self.rect.centery + 13), 14, C["muted"], False,
                 "midleft", self.rect.w - 110)
        else:
            text(surf, self.label, (self.rect.x + 18, self.rect.centery), 19, col, False, "midleft",
                 self.rect.w - 110)
        sw = pygame.Rect(0, 0, 58, 30)
        sw.midright = (self.rect.right - 16, self.rect.centery)
        rrect(surf, C["accent"] if self.value else C["line"], sw, 15)
        knob = (sw.right - 15, sw.centery) if self.value else (sw.x + 15, sw.centery)
        pygame.draw.circle(surf, C["text"], knob, 11)

    def activate(self, app):
        if self.enabled:
            self.on_change(not self.value)

    def nav(self, app, d):
        if d in ("left", "right") and self.enabled:
            want = d == "right"
            if want != self.value:
                self.on_change(want)
                return True
        return False


class Selector(Widget):
    """Pick one of several options with left/right (or A to cycle)."""

    def __init__(self, key, rect, label, options, index, on_change, enabled=True):
        super().__init__(key, rect, enabled)
        self.label, self.options, self.index, self.on_change = label, options, index, on_change

    def draw(self, surf, focused):
        rrect(surf, C["panel2"] if focused else C["panel"], self.rect, 12)
        if focused:
            focus_ring(surf, self.rect, 12)
        text(surf, self.label, (self.rect.x + 18, self.rect.y + 10), 14, C["muted"])
        val = self.options[self.index] if self.options else "—"
        text(surf, val, (self.rect.x + 18, self.rect.bottom - 12), 19, C["text"], True, "bottomleft",
             self.rect.w - 90)
        if len(self.options) > 1:
            text(surf, "◀  ▶", (self.rect.right - 18, self.rect.centery), 16,
                 C["text"] if focused else C["dim"], False, "midright")

    def _move(self, step):
        if self.options and self.enabled:
            self.on_change((self.index + step) % len(self.options))

    def activate(self, app):
        self._move(1)

    def nav(self, app, d):
        if d == "left":
            self._move(-1)
            return True
        if d == "right":
            self._move(1)
            return True
        return False

    def click(self, app, pos):
        self._move(-1 if pos[0] < self.rect.centerx - self.rect.w // 4 else 1)


class TextField(Widget):
    def __init__(self, key, rect, label, value, on_change, password=False, placeholder="",
                 enabled=True, on_submit=None):
        super().__init__(key, rect, enabled)
        self.label, self.value, self.on_change = label, value, on_change
        self.password, self.placeholder, self.on_submit = password, placeholder, on_submit

    def draw(self, surf, focused):
        rrect(surf, C["bg2"], self.rect, 12)
        rrect(surf, C["accent"] if focused else C["line"], self.rect, 12, 2 if focused else 1)
        if focused:
            focus_ring(surf, self.rect, 12)
        text(surf, self.label, (self.rect.x + 16, self.rect.y + 8), 14, C["muted"])
        if self.value:
            shown = "•" * len(self.value) if self.password else self.value
            text(surf, shown, (self.rect.x + 16, self.rect.bottom - 10), 20, C["text"], False, "bottomleft",
                 self.rect.w - 40)
        else:
            text(surf, self.placeholder, (self.rect.x + 16, self.rect.bottom - 10), 19, C["dim"], False,
                 "bottomleft", self.rect.w - 40)

    def activate(self, app):
        if self.enabled:
            app.open_keyboard(self.label, self.value, self.on_change, self.password, self.on_submit)


class ListView(Widget):
    """Vertical list with its own selection. Up/down move inside; edges hand focus back."""

    def __init__(self, key, rect, items, index, on_index, on_activate=None, row_h=58,
                 render=None, empty="Nothing here", on_alt=None):
        super().__init__(key, rect)
        self.items, self.index, self.on_index = items, max(0, min(index, len(items) - 1)), on_index
        self.on_activate, self.row_h, self.render, self.empty, self.on_alt = \
            on_activate, row_h, render, empty, on_alt
        self.focusable = True

    def _visible(self):
        return max(1, self.rect.h // self.row_h)

    def _top(self):
        vis = self._visible()
        return max(0, min(self.index - vis // 2, len(self.items) - vis))

    def draw(self, surf, focused):
        panel(surf, self.rect, C["bg2"], 14)
        if not self.items:
            text(surf, self.empty, self.rect.center, 18, C["dim"], False, "center")
            if focused:
                focus_ring(surf, self.rect, 14)
            return
        clip = surf.get_clip()
        surf.set_clip(self.rect.inflate(-4, -4))
        top = self._top()
        for i in range(top, min(len(self.items), top + self._visible() + 1)):
            r = pygame.Rect(self.rect.x + 6, self.rect.y + 6 + (i - top) * self.row_h,
                            self.rect.w - 12, self.row_h - 6)
            sel = i == self.index
            if sel:
                rrect(surf, C["panel2"] if focused else C["panel"], r, 10)
                if focused:
                    bar = pygame.Rect(r.x, r.y + 8, 4, r.h - 16)
                    rrect(surf, C["accent"], bar, 2)
            if self.render:
                self.render(surf, r, self.items[i], sel, focused)
            else:
                text(surf, str(self.items[i]), (r.x + 18, r.centery), 19,
                     C["text"] if sel else C["muted"], sel, "midleft", r.w - 30)
        surf.set_clip(clip)
        # scrollbar
        if len(self.items) > self._visible():
            frac = self._visible() / len(self.items)
            bar_h = max(30, int(self.rect.h * frac))
            pos = int((self.rect.h - bar_h) * (top / max(1, len(self.items) - self._visible())))
            rrect(surf, C["line"], pygame.Rect(self.rect.right - 7, self.rect.y + pos, 4, bar_h), 2)
        if focused:
            focus_ring(surf, self.rect, 14)

    def nav(self, app, d):
        if d == "up" and self.index > 0:
            self.on_index(self.index - 1)
            return True
        if d == "down" and self.index < len(self.items) - 1:
            self.on_index(self.index + 1)
            return True
        return False

    def scroll(self, app, amount):
        if self.items:
            self.on_index(max(0, min(len(self.items) - 1, self.index + amount)))
        return True

    def activate(self, app):
        if self.items and self.on_activate:
            self.on_activate(self.index)

    def alt(self, app, button):
        if button == "y" and self.items and self.on_alt:
            self.on_alt(self.index)
            return True
        return False

    def click(self, app, pos):
        if not self.items:
            return
        i = self._top() + (pos[1] - self.rect.y - 6) // self.row_h
        if 0 <= i < len(self.items):
            if i == self.index and self.on_activate:
                self.on_activate(i)
            else:
                self.on_index(i)
                if self.on_activate and app.touch_activates:
                    self.on_activate(i)


# ------------------------------------------------------------ spatial nav
def _score(cur, cand, d):
    cx, cy = cur.center
    ox, oy = cand.center
    if d == "right":
        primary, ok = ox - cx, cand.left >= cur.centerx - 4
        gap = max(0, max(cand.top - cur.bottom, cur.top - cand.bottom))
    elif d == "left":
        primary, ok = cx - ox, cand.right <= cur.centerx + 4
        gap = max(0, max(cand.top - cur.bottom, cur.top - cand.bottom))
    elif d == "down":
        primary, ok = oy - cy, cand.top >= cur.centery - 4
        gap = max(0, max(cand.left - cur.right, cur.left - cand.right))
    else:
        primary, ok = cy - oy, cand.bottom <= cur.centery + 4
        gap = max(0, max(cand.left - cur.right, cur.left - cand.right))
    if not ok or primary <= 0:
        return None
    return primary + gap * 3


def spatial_next(widgets, current, d):
    best, best_s = None, None
    for w in widgets:
        if w is current or not w.focusable:
            continue
        s = _score(current.rect, w.rect, d)
        if s is not None and (best_s is None or s < best_s):
            best, best_s = w, s
    return best
