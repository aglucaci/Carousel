"""Generate the CAROUSEL logo SVGs (stdlib only; the wordmark is drawn as paths, no fonts).

Usage:  python logo/generate.py logo

Writes carousel-logo.svg (light), carousel-logo-dark.svg (dark backgrounds),
carousel-mark.svg (symbol only) and carousel-icon.svg (app icon / favicon source).
The PNGs in this folder were rendered from these SVGs with headless Chrome.
"""
import math
import sys
from pathlib import Path

NAVY = "#0B2545"
TEAL = "#13A89E"
GOLD = "#E3A72F"
SLATE = "#8DA2B8"
PAPER = "#F7F5F0"

SW = 15  # wordmark stroke width, cap height 100


# ---------------- mark ----------------

def mark(fg=NAVY, ring=NAVY, paper="#FFFFFF", show_ring=True):
    """Square mark, viewBox 0 0 120 120: a ring of paper cards in perspective, featured card in front."""
    cx, cy = 60, 60
    parts = [f'<circle cx="{cx}" cy="{cy}" r="54" fill="none" stroke="{ring}" stroke-width="7"/>'] if show_ring else []
    # orbit track behind the cards
    parts.append(f'<ellipse cx="{cx}" cy="70" rx="38" ry="11" fill="none" stroke="{TEAL}" stroke-opacity=".45" stroke-width="3"/>')

    def card(x, y_bottom, w, color, op=1.0, lines=False):
        h = w * 1.3
        r = w * 0.14
        svg = f'<rect x="{x - w / 2:.2f}" y="{y_bottom - h:.2f}" width="{w:.2f}" height="{h:.2f}" rx="{r:.2f}" fill="{color}" fill-opacity="{op}"/>'
        if lines:
            lx = x - w / 2 + w * 0.2
            for i, frac in enumerate((0.6, 0.6, 0.38)):
                ly = y_bottom - h + h * (0.26 + i * 0.17)
                svg += f'<rect x="{lx:.2f}" y="{ly:.2f}" width="{w * frac:.2f}" height="{w * 0.09:.2f}" rx="{w * 0.045:.2f}" fill="{paper}" fill-opacity=".92"/>'
        return svg

    # back -> front
    parts.append(card(42, 62, 16, SLATE, 0.75))
    parts.append(card(78, 62, 16, SLATE, 0.75))
    parts.append(card(31, 74, 20, TEAL))
    parts.append(card(89, 74, 20, TEAL))
    parts.append(card(60, 84, 34, GOLD, lines=True))
    return "\n  ".join(parts)


# ---------------- wordmark ----------------

def _arc_pt(cx, cy, rx, ry, deg):
    t = math.radians(deg)
    return cx + rx * math.cos(t), cy + ry * math.sin(t)


def letters(fg, accent):
    """Return (svg, width). Monoline geometric caps on a 0..100 cap height, clipped flat."""
    h = SW / 2
    out = []
    x = 0.0
    gap = 24

    def add(paths, adv, lsb=0):
        nonlocal x
        x += lsb
        out.append(f'<g transform="translate({x:.2f} 0)">{paths}</g>')
        x += adv + gap

    st = f'fill="none" stroke="{fg}" stroke-width="{SW}"'
    # C
    r = 50 - h
    p1 = _arc_pt(50, 50, r, r, -42)
    p2 = _arc_pt(50, 50, r, r, 42)
    add(f'<path d="M{p1[0]:.2f} {p1[1]:.2f} A{r} {r} 0 1 0 {p2[0]:.2f} {p2[1]:.2f}" {st}/>', 88)
    # A (apex and feet clipped flat)
    add(f'<path d="M-2 112 L42 -6 L86 112 M18 70 H66" {st} stroke-linejoin="miter"/>', 84, lsb=2)
    # R
    add(f'<path d="M{h} -5 V105 M{h} {h} H40 A{25 - h / 2 + 4} {25 - h / 2 + 4} 0 0 1 40 57 H{h} M40 57 L74 108" {st}/>', 74)
    # O
    add(f'<circle cx="50" cy="50" r="{50 - h}" {st}/>', 100)
    # U
    r = 40 - h
    add(f'<path d="M{h} -5 V60 A{r} {r} 0 0 0 {80 - h} 60 V-5" {st}/>', 80)
    # S
    rx, ry = 31, (100 - SW) / 4
    top_c = (38, h + ry)
    bot_c = (38, 100 - h - ry)
    a = _arc_pt(*top_c, rx, ry, -28)
    m = (38, h + 2 * ry)
    b = _arc_pt(*bot_c, rx, ry, 152)
    add(f'<path d="M{a[0]:.2f} {a[1]:.2f} A{rx} {ry} 0 1 0 {m[0]:.2f} {m[1]:.2f} A{rx} {ry} 0 1 1 {b[0]:.2f} {b[1]:.2f}" {st}/>', 76, lsb=0)
    # E (middle bar in accent)
    add(
        f'<path d="M{h} -5 V105 M{h} {h} H66 M{h} {100 - h} H66" {st}/>'
        f'<path d="M{SW - 1} 50 H60" fill="none" stroke="{accent}" stroke-width="{SW}"/>',
        66,
    )
    # L
    add(f'<path d="M{h} -5 V{100 - h} H62" {st}/>', 62)
    return "\n  ".join(out), x - gap


def logo(fg, ring, word_fg, accent):
    word, ww = letters(word_fg, accent)
    # mark 120x120 scaled by 1.4; wordmark (cap height 100) centered beside it
    scale = 1.4
    mw = 120 * scale
    pad = 18
    W = mw + 46 + ww + pad
    H = 120 * scale
    wy = (H - 100) / 2 + 6
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.0f} {H:.0f}" width="{W:.0f}" height="{H:.0f}" role="img" aria-label="CAROUSEL">
  <title>CAROUSEL</title>
  <defs><clipPath id="cap"><rect x="-20" y="0" width="{ww + 40:.0f}" height="100"/></clipPath></defs>
  <g transform="scale({scale})">
  {mark(fg, ring)}
  </g>
  <g transform="translate({mw + 46:.2f} {wy:.2f})" clip-path="url(#cap)">
  {word}
  </g>
</svg>
'''


def mark_svg(fg=NAVY, ring=NAVY, bg=None):
    rect = f'<rect width="120" height="120" rx="26" fill="{bg}"/>' if bg else ""
    if bg:  # app icon: the tile is the frame, so drop the ring and enlarge the cards
        return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120" width="120" height="120" role="img" aria-label="CAROUSEL">
  <title>CAROUSEL</title>
  {rect}
  <g transform="translate(60 58) scale(1.3) translate(-60 -60)">
  {mark(fg, ring, show_ring=False)}
  </g>
</svg>
'''
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120" width="120" height="120" role="img" aria-label="CAROUSEL">
  <title>CAROUSEL</title>
  {rect}
  {mark(fg, ring)}
</svg>
'''


if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    (out / "carousel-logo.svg").write_text(logo(NAVY, NAVY, NAVY, TEAL))
    (out / "carousel-logo-dark.svg").write_text(logo(PAPER, PAPER, PAPER, TEAL))
    (out / "carousel-mark.svg").write_text(mark_svg())
    (out / "carousel-icon.svg").write_text(mark_svg(fg=PAPER, ring=PAPER, bg=NAVY))
