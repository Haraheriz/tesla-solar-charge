"""アプリアイコンを生成する（外部画像ライブラリ不使用、struct/zlib のみで PNG を書く）。

図柄は 2026-09-28 に決めた K9 案である。淡い桜色の地に、光線がつながった深紅（#BC002D）の
太陽を置き、銀色の雷が太陽を縦に貫く。雷は旧アイコンの緑（黄緑→ティール）で縁取る。

形はすべて 512×512 の座標で定義し、出力サイズへ拡大縮小する。塗りの判定は
「多角形からの符号付き距離」で行う。SVG の stroke-linejoin="round" と同じく、
多角形を半径 r だけ膨らませた形（角が丸くなる）を1回の距離計算で得られ、
距離をそのまま画素の被覆率に換算できるため、スーパーサンプリングを要しない。

出力するファイルと用途：

  icon-{192,512}.png          manifest の purpose "any"
  icon-maskable-{192,512}.png manifest の purpose "maskable"（図柄を 90% に縮める）
  apple-touch-icon-180.png    iOS のホーム画面（不透明、角丸は iOS が付ける）

maskable の安全領域は中心から半径 40%（W3C Web App Manifest）。原寸の図柄は
最も外側の雷の先端が中心から 197/512 = 38.5% にあり、安全領域の内側に収まっている。
maskable 用はさらに 90% に縮め、端末ごとの切り抜き形状の差に余裕を持たせる。
"""
import math
import os
import struct
import zlib

ICONS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "icons")

# ---- 図柄の定義（512×512 座標） ----

# 背景：中心 (256, 215)・半径 307 の放射グラデーション
BG_CENTER = (256.0, 215.0)
BG_RADIUS = 307.0
BG_INNER = (0xFF, 0xF6, 0xF1)
BG_OUTER = (0xFB, 0xE4, 0xDA)

SUN_COLOR = (0xBC, 0x00, 0x2D)
SUN_ROUND = 8.0  # 光線の先端と谷を丸める半径（SVG の stroke-width 16 の半分）


def _sun_polygon():
    """円と12本の光線がつながった星形。先端の半径 136、谷の半径 104。"""
    points = []
    for i in range(24):
        t = math.pi * i / 12
        r = 136.0 if i % 2 == 0 else 104.0
        points.append((256 + r * math.sin(t), 256 - r * math.cos(t)))
    return points


SUN = _sun_polygon()

BOLT = [
    (277.1, 91.7), (174.8, 280.8), (246.2, 280.8), (218.2, 420.3),
    (339.1, 218.8), (267.9, 218.8), (329.9, 91.7),
]
BOLT_ROUND = 9.0     # 銀の部分の角の丸め（stroke-width 18 の半分）
OUTLINE_ROUND = 17.0  # 緑の縁取りの外側（stroke-width 34 の半分）。縁取りの幅は 8

# 緑の縁取り：雷の外接矩形の上端から下端へ
GREEN_TOP = (0xB5, 0xE6, 0x1D)
GREEN_BOTTOM = (0x10, 0xB9, 0x81)

# 銀：外接矩形を基準にした斜めのグラデーション。明暗の帯を交互に置いて金属の映り込みを表す
CHROME_STOPS = [
    (0.00, (0xFF, 0xFF, 0xFF)),
    (0.22, (0xBF, 0xC5, 0xCD)),
    (0.40, (0xF5, 0xF7, 0xF9)),
    (0.58, (0x86, 0x8E, 0x99)),
    (0.78, (0xE4, 0xE7, 0xEB)),
    (1.00, (0x9D, 0xA4, 0xAE)),
]
CHROME_FROM = (0.1, 0.0)  # 外接矩形の中の相対座標（SVG の x1, y1）
CHROME_TO = (0.9, 1.0)    # 同 x2, y2

BOLT_MIN_X = min(p[0] for p in BOLT)
BOLT_MAX_X = max(p[0] for p in BOLT)
BOLT_MIN_Y = min(p[1] for p in BOLT)
BOLT_MAX_Y = max(p[1] for p in BOLT)


# ---- 幾何 ----

def _clamp01(v):
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


def _lerp(c1, c2, t):
    return tuple(c1[i] + (c2[i] - c1[i]) * t for i in range(3))


def _gradient(stops, t):
    t = _clamp01(t)
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t <= t1:
            return _lerp(c0, c1, (t - t0) / (t1 - t0) if t1 > t0 else 0.0)
    return stops[-1][1]


def _inside(x, y, poly):
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _edge_distance(x, y, poly):
    best = float("inf")
    j = len(poly) - 1
    for i in range(len(poly)):
        ax, ay = poly[j]
        bx, by = poly[i]
        dx, dy = bx - ax, by - ay
        t = _clamp01(((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy))
        ex, ey = ax + dx * t - x, ay + dy * t - y
        d = ex * ex + ey * ey
        if d < best:
            best = d
        j = i
    return math.sqrt(best)


def _signed_distance(x, y, poly):
    """多角形の境界までの距離。内側を負にする。"""
    d = _edge_distance(x, y, poly)
    return -d if _inside(x, y, poly) else d


def _coverage(signed_distance, radius, pixel):
    """多角形を radius だけ膨らませた形が、1画素をどれだけ覆うか（0〜1）。"""
    return _clamp01(0.5 - (signed_distance - radius) / pixel)


# ---- 描画 ----

def render(size, scale=1.0):
    """size×size の RGB バイト列を返す。scale は図柄の縮小率（背景は常に全面）。"""
    unit = 512.0 / size  # 出力1画素が 512 座標でいくつ分か
    # 縮小は中心 (256, 256) を基準に、図柄側の座標へ逆変換して行う
    pixel = unit / scale
    sun_reach = 136.0 + SUN_ROUND + pixel
    out = bytearray(size * size * 3)

    for py in range(size):
        for px in range(size):
            # 出力画素の中心を 512 座標へ
            gx = (px + 0.5) * unit
            gy = (py + 0.5) * unit
            bgd = math.hypot(gx - BG_CENTER[0], gy - BG_CENTER[1]) / BG_RADIUS
            color = _lerp(BG_INNER, BG_OUTER, _clamp01(bgd))

            # 図柄側の座標
            x = 256 + (gx - 256) / scale
            y = 256 + (gy - 256) / scale

            if math.hypot(x - 256, y - 256) <= sun_reach:
                a = _coverage(_signed_distance(x, y, SUN), SUN_ROUND, pixel)
                if a > 0:
                    color = _lerp(color, SUN_COLOR, a)

            if (BOLT_MIN_X - OUTLINE_ROUND - pixel <= x <= BOLT_MAX_X + OUTLINE_ROUND + pixel
                    and BOLT_MIN_Y - OUTLINE_ROUND - pixel <= y <= BOLT_MAX_Y + OUTLINE_ROUND + pixel):
                sd = _signed_distance(x, y, BOLT)
                a = _coverage(sd, OUTLINE_ROUND, pixel)
                if a > 0:
                    t = (y - BOLT_MIN_Y) / (BOLT_MAX_Y - BOLT_MIN_Y)
                    color = _lerp(color, _lerp(GREEN_TOP, GREEN_BOTTOM, _clamp01(t)), a)
                    a = _coverage(sd, BOLT_ROUND, pixel)
                    if a > 0:
                        u = (x - BOLT_MIN_X) / (BOLT_MAX_X - BOLT_MIN_X)
                        v = (y - BOLT_MIN_Y) / (BOLT_MAX_Y - BOLT_MIN_Y)
                        gxv = CHROME_TO[0] - CHROME_FROM[0]
                        gyv = CHROME_TO[1] - CHROME_FROM[1]
                        t = ((u - CHROME_FROM[0]) * gxv + (v - CHROME_FROM[1]) * gyv) / (gxv * gxv + gyv * gyv)
                        color = _lerp(color, _gradient(CHROME_STOPS, t), a)

            i = (py * size + px) * 3
            out[i] = int(round(color[0]))
            out[i + 1] = int(round(color[1]))
            out[i + 2] = int(round(color[2]))
    return bytes(out)


def write_png(path, size, rgb):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = bytearray()
    stride = size * 3
    for y in range(size):
        raw.append(0)
        raw.extend(rgb[y * stride:(y + 1) * stride])

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(bytes(raw), 9)))
        f.write(chunk(b"IEND", b""))


OUTPUTS = [
    ("icon-192.png", 192, 1.0),
    ("icon-512.png", 512, 1.0),
    ("icon-maskable-192.png", 192, 0.9),
    ("icon-maskable-512.png", 512, 0.9),
    ("apple-touch-icon-180.png", 180, 1.0),
]


if __name__ == "__main__":
    for name, size, scale in OUTPUTS:
        path = os.path.join(ICONS_DIR, name)
        write_png(path, size, render(size, scale))
        print(f"wrote {path}")
