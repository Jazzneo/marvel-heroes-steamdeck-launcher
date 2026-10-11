"""Render the fallback Steam library artwork in assets/steam (original MH Launcher art only).  Usage: make-steam-art.py <out dir>"""
import math
import os
import sys

os.environ["SDL_VIDEODRIVER"] = "dummy"
import pygame

F = "/usr/share/fonts/opentype/inter/"
pygame.init()

RED = (226, 36, 41)
RED_HI = (255, 82, 84)
RED_DK = (150, 22, 28)
BG = (11, 12, 17)
TEXT = (242, 243, 247)
MUTED = (160, 166, 184)
GOLD = (244, 186, 64)


def font(name, size):
    return pygame.font.Font(F + name, size)


def hexagon(surf, center, r, color, width=0, rot=30):
    cx, cy = center
    pts = [(cx + r * math.cos(math.radians(60 * k + rot)), cy + r * math.sin(math.radians(60 * k + rot)))
           for k in range(6)]
    pygame.draw.polygon(surf, color, pts, width)
    return pts


def glow(surf, center, radius, color, strength=70):
    g = pygame.Surface((radius * 2, radius * 2), pygame.SRCALPHA)
    for i in range(radius, 0, -4):
        a = int(strength * (1 - i / radius) ** 2)
        pygame.draw.circle(g, (*color, a), (radius, radius), i)
    surf.blit(g, (center[0] - radius, center[1] - radius))


def emblem(surf, center, r):
    glow(surf, center, int(r * 2.4), RED, 60)
    hexagon(surf, (center[0] + r * 0.06, center[1] + r * 0.08), r, (60, 8, 12))   # shadow
    hexagon(surf, center, r, RED)
    hexagon(surf, center, r * 0.80, RED_DK, max(2, int(r * 0.035)))
    hexagon(surf, center, r, RED_HI, max(2, int(r * 0.05)))
    # top-half shine
    shine = pygame.Surface(surf.get_size(), pygame.SRCALPHA)
    pts = hexagon(shine, center, r * 0.96, (255, 255, 255, 28))
    cut = pygame.Rect(0, center[1], surf.get_width(), surf.get_height())
    shine.fill((0, 0, 0, 0), cut)
    surf.blit(shine, (0, 0))
    f = font("InterDisplay-Black.otf", int(r * 0.78))
    t = f.render("MH", True, TEXT)
    surf.blit(t, t.get_rect(center=(center[0], center[1] + r * 0.02)))


def background(surf, glow_at):
    w, h = surf.get_size()
    surf.fill(BG)
    # vertical gradient
    for y in range(h):
        k = y / h
        pygame.draw.line(surf, (int(11 + 8 * k), int(12 + 6 * k), int(17 + 10 * k)), (0, y), (w, y))
    glow(surf, glow_at, int(max(w, h) * 0.55), RED, 55)
    stripes = pygame.Surface((w, h), pygame.SRCALPHA)
    for i in range(-10, w // 60 + 10):
        x = i * 64
        pygame.draw.polygon(stripes, (226, 36, 41, 11), [(x, h), (x + 26, h), (x + 26 + h * 0.6, 0), (x + h * 0.6, 0)])
    surf.blit(stripes, (0, 0))


K = [1.0]


def chip(surf, x, y, label):
    f = font("Inter-SemiBold.otf", int(22 * K[0]))
    t = f.render(label, True, TEXT)
    r = pygame.Rect(x, y, t.get_width() + int(40 * K[0]), int(44 * K[0]))
    pygame.draw.rect(surf, (30, 33, 44), r, border_radius=r.h // 2)
    pygame.draw.rect(surf, (58, 63, 80), r, 1, border_radius=r.h // 2)
    pygame.draw.circle(surf, RED, (r.x + int(18 * K[0]), r.centery), int(5 * K[0]))
    surf.blit(t, (r.x + int(30 * K[0]), r.centery - t.get_height() // 2))
    return r.right


def handheld(surf, center, screen_w, shot):
    """Generic handheld console drawn from simple shapes (no real product branding)."""
    sw, sh = screen_w, int(screen_w * 800 / 1280)
    body = pygame.Rect(0, 0, int(sw * 1.52), int(sh * 1.34))
    body.center = center
    # shadow
    sh_s = pygame.Surface((body.w + 80, body.h + 80), pygame.SRCALPHA)
    pygame.draw.rect(sh_s, (0, 0, 0, 120), sh_s.get_rect().inflate(-40, -40), border_radius=body.h // 3)
    surf.blit(pygame.transform.smoothscale(sh_s, (body.w + 60, body.h + 60)), (body.x - 30, body.y - 10))
    pygame.draw.rect(surf, (28, 30, 38), body, border_radius=body.h // 3)
    pygame.draw.rect(surf, (52, 56, 70), body, 2, border_radius=body.h // 3)
    # screen bezel + screen
    bez = pygame.Rect(0, 0, sw + 24, sh + 24)
    bez.center = body.center
    pygame.draw.rect(surf, (8, 9, 12), bez, border_radius=14)
    img = pygame.transform.smoothscale(shot, (sw, sh))
    srect = img.get_rect(center=body.center)
    surf.blit(img, srect)
    # screen glare
    glare = pygame.Surface((sw, sh), pygame.SRCALPHA)
    pygame.draw.polygon(glare, (255, 255, 255, 14), [(0, 0), (sw * 0.45, 0), (sw * 0.2, sh), (0, sh)])
    surf.blit(glare, srect)
    # controls: left = stick over d-pad, right = face buttons over stick
    side = (body.w - bez.w) // 2
    lx = body.x + side // 2
    rx = body.right - side // 2
    top_y = body.y + int(body.h * 0.33)
    low_y = body.y + int(body.h * 0.67)
    st = int(side * 0.30)

    def stick(x, y):
        pygame.draw.circle(surf, (12, 13, 18), (x, y), st)
        pygame.draw.circle(surf, (46, 50, 64), (x, y), int(st * 0.68))
        pygame.draw.circle(surf, (60, 65, 82), (x, y), int(st * 0.68), 2)
    stick(lx, top_y)
    stick(rx, low_y)
    a = max(5, int(side * 0.085))
    pygame.draw.rect(surf, (12, 13, 18), (lx - a, low_y - 3 * a, 2 * a, 6 * a), border_radius=3)
    pygame.draw.rect(surf, (12, 13, 18), (lx - 3 * a, low_y - a, 6 * a, 2 * a), border_radius=3)
    for dx, dy, col in ((0, -1, (245, 184, 61)), (1, 0, (240, 76, 76)), (0, 1, (52, 199, 120)), (-1, 0, (82, 163, 255))):
        p = (rx + dx * 2.4 * a, top_y + dy * 2.4 * a)
        pygame.draw.circle(surf, (12, 13, 18), p, int(a * 1.2))
        pygame.draw.circle(surf, col, p, int(a * 0.6))
    return body



OUT = sys.argv[1]
os.makedirs(OUT, exist_ok=True)


def title_block(s, x, cy, k, size=66):
    title = font("InterDisplay-Black.otf", int(size * k))
    t = title.render("MH LAUNCHER", True, TEXT)
    emblem(s, (x + int(46 * k * size / 66), cy), int(46 * k * size / 66))
    s.blit(t, (x + int(110 * k * size / 66), cy - t.get_height() // 2 + int(3 * k)))
    return t


def capsule(w, h, path):            # library_600x900 (tall cover)
    s = pygame.Surface((w, h))
    background(s, (w // 2, int(h * 0.36)))
    k = w / 600
    emblem(s, (w // 2, int(h * 0.36)), int(150 * k))
    f = font("InterDisplay-Black.otf", int(84 * k))
    for i, word in enumerate(("MH", "LAUNCHER")):
        t = f.render(word, True, TEXT)
        s.blit(t, t.get_rect(center=(w // 2, int(h * 0.64) + i * int(86 * k))))
    f2 = font("Inter-Bold.otf", int(28 * k))
    t = f2.render("for Marvel Heroes  ·  Steam Deck", True, RED_HI)
    s.blit(t, t.get_rect(center=(w // 2, int(h * 0.87))))
    pygame.image.save(s, path)


def wide(w, h, path):               # header / grid 920x430
    s = pygame.Surface((w, h))
    background(s, (int(w * 0.75), int(h * 0.5)))
    k = h / 430
    f = font("InterDisplay-Black.otf", int(92 * k))
    emblem(s, (int(w * 0.18), h // 2), int(110 * k))
    for i, word in enumerate(("MH", "LAUNCHER")):
        t = f.render(word, True, TEXT)
        s.blit(t, (int(w * 0.34), int(h * 0.5) - int(100 * k) + i * int(96 * k)))
    f2 = font("Inter-Bold.otf", int(26 * k))
    s.blit(f2.render("for Marvel Heroes  ·  Steam Deck", True, RED_HI), (int(w * 0.34), int(h * 0.5) + int(96 * k)))
    pygame.image.save(s, path)


def hero(w, h, path):               # library_hero 1920x620 (no text: Steam puts the logo on top)
    s = pygame.Surface((w, h))
    background(s, (int(w * 0.72), int(h * 0.5)))
    for i, (cx, r, a) in enumerate(((0.72, 260, 255), (0.88, 120, 120), (0.60, 90, 90))):
        layer = pygame.Surface((w, h), pygame.SRCALPHA)
        hexagon(layer, (int(w * cx), int(h * 0.5)), r, (*RED, a // 4))
        hexagon(layer, (int(w * cx), int(h * 0.5)), r, (*RED_HI, a // 2), 3)
        s.blit(layer, (0, 0))
    pygame.image.save(s, path)


def logo(w, h, path):               # logo with transparent background
    s = pygame.Surface((w, h), pygame.SRCALPHA)
    k = h / 200
    title_block(s, int(10 * k), h // 2, k, size=96)
    pygame.image.save(s, path)


capsule(600, 900, os.path.join(OUT, "capsule.png"))
wide(920, 430, os.path.join(OUT, "wide.png"))
hero(1920, 620, os.path.join(OUT, "hero.png"))
logo(1100, 200, os.path.join(OUT, "logo.png"))
print("ok")
