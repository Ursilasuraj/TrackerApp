"""Draws the TodoTracker icon - a white tick on a blue rounded square, the
same design as the favicon in web/index.html - as PNG files, with the
standard library only.

    python3 tools/icons.py web               web/icon-192.png, icon-512.png,
                                             icon-maskable-512.png, icon.ico
    python3 tools/icons.py android RES_DIR   launcher icons for the APK
    python3 tools/icons.py iconset DIR       a macOS .iconset (iconutil -c icns DIR)
    python3 tools/icons.py png SIZE FILE     one icon

Shapes are described in the favicon's 32x32 viewBox and drawn with signed
distances, so every size is anti-aliased without supersampling.
"""

import math
import os
import struct
import sys
import zlib

BLUE = (0x25, 0x63, 0xEB)
WHITE = (0xFF, 0xFF, 0xFF)
CORNER = 7.0                                 # rx of the rounded square (viewBox units)
TICK = [(9.0, 16.5), (13.5, 21.0), (23.0, 11.0)]
TICK_HALF_WIDTH = 1.6                        # stroke-width 3.2, round caps and joins


def _rounded_square(u, v, inset=0.0):
    """Signed distance to the 32x32 rounded square (negative inside)."""
    half = 16.0 - inset
    r = CORNER
    qx = abs(u - 16.0) - (half - r)
    qy = abs(v - 16.0) - (half - r)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    return outside + min(max(qx, qy), 0.0) - r


def _segment(u, v, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    t = ((u - ax) * dx + (v - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(u - (ax + t * dx), v - (ay + t * dy))


def _tick(u, v):
    return min(_segment(u, v, TICK[0], TICK[1]), _segment(u, v, TICK[1], TICK[2])) - TICK_HALF_WIDTH


# Where the 32-unit design square sits on the canvas (left/top and side as
# fractions of the canvas) and which background is drawn.
VARIANTS = {
    # Rounded square filling the canvas (web, Windows, old Android launchers).
    'plain': {'box': (0.0, 1.0), 'background': 'rounded'},
    # macOS: the Big Sur icon grid keeps a 100/1024 margin around the shape.
    'mac': {'box': (100 / 1024, 824 / 1024), 'background': 'rounded'},
    # PWA "maskable": full-bleed blue; the tick stays inside the safe circle.
    'maskable': {'box': (0.0, 1.0), 'background': 'full'},
    # Android adaptive icon foreground (108dp canvas, 72dp visible): tick only.
    'foreground': {'box': ((1 - 72 / 108) / 2, 72 / 108), 'background': None},
    # Android status bar: a white tick filling the icon (only its shape counts).
    'mono': {'box': (-0.25, 1.5), 'background': None},
}


def render(size, variant='plain'):
    """RGBA rows (bytes per row) of the icon at size x size pixels."""
    spec = VARIANTS[variant]
    offset, side = spec['box']
    scale = 32.0 / (side * size)             # design units per pixel
    rows = []
    for y in range(size):
        row = bytearray()
        v = ((y + 0.5) - offset * size) * scale
        for x in range(size):
            u = ((x + 0.5) - offset * size) * scale
            if spec['background'] == 'rounded':
                a_bg = min(1.0, max(0.0, 0.5 - _rounded_square(u, v) / scale))
            elif spec['background'] == 'full':
                a_bg = 1.0
            else:
                a_bg = 0.0
            a_tick = min(1.0, max(0.0, 0.5 - _tick(u, v) / scale))
            if spec['background'] == 'rounded':
                a_tick = min(a_tick, a_bg)
            alpha = a_tick + a_bg * (1.0 - a_tick)
            if alpha <= 0.0:
                row += b'\0\0\0\0'
                continue
            px = [(WHITE[i] * a_tick + BLUE[i] * a_bg * (1.0 - a_tick)) / alpha for i in range(3)]
            row += bytes((round(px[0]), round(px[1]), round(px[2]), round(alpha * 255)))
        rows.append(bytes(row))
    return rows


def png(rows):
    """Encode RGBA rows as a PNG file."""
    height = len(rows)
    width = len(rows[0]) // 4

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b''.join(b'\0' + row for row in rows)
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 9))
            + chunk(b'IEND', b''))


def ico(sizes):
    """A Windows .ico holding PNG images of the given sizes."""
    images = [png(render(s)) for s in sizes]
    out = struct.pack('<HHH', 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for s, data in zip(sizes, images):
        out += struct.pack('<BBBBHHII', s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return out + b''.join(images)


def write(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)


def make_web(web_dir):
    write(os.path.join(web_dir, 'icon-192.png'), png(render(192)))
    write(os.path.join(web_dir, 'icon-512.png'), png(render(512)))
    write(os.path.join(web_dir, 'icon-maskable-512.png'), png(render(512, 'maskable')))
    write(os.path.join(web_dir, 'icon.ico'), ico([16, 24, 32, 48, 64, 256]))


ANDROID_DENSITIES = {'mdpi': 1.0, 'hdpi': 1.5, 'xhdpi': 2.0, 'xxhdpi': 3.0, 'xxxhdpi': 4.0}


def make_android(res_dir):
    """Launcher icons (48dp; Android 8+ uses the 108dp foreground on its own
    shape) and the 24dp status bar icon for reminders."""
    for name, factor in ANDROID_DENSITIES.items():
        folder = os.path.join(res_dir, 'mipmap-' + name)
        write(os.path.join(folder, 'ic_launcher.png'), png(render(round(48 * factor))))
        write(os.path.join(folder, 'ic_launcher_foreground.png'), png(render(round(108 * factor), 'foreground')))
        write(os.path.join(res_dir, 'drawable-' + name, 'ic_notification.png'), png(render(round(24 * factor), 'mono')))


def make_iconset(folder):
    for base in (16, 32, 128, 256, 512):
        write(os.path.join(folder, f'icon_{base}x{base}.png'), png(render(base, 'mac')))
        write(os.path.join(folder, f'icon_{base}x{base}@2x.png'), png(render(base * 2, 'mac')))


def main(argv):
    if len(argv) >= 1 and argv[0] == 'web':
        make_web(argv[1] if len(argv) > 1 else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'web'))
    elif len(argv) == 2 and argv[0] == 'android':
        make_android(argv[1])
    elif len(argv) == 2 and argv[0] == 'iconset':
        make_iconset(argv[1])
    elif len(argv) == 3 and argv[0] == 'png':
        write(argv[2], png(render(int(argv[1]))))
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
