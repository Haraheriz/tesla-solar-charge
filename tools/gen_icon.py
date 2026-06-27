"""アイコン再生成スクリプト（外部画像ライブラリ不使用、struct/zlibのみでPNGを自前エンコード）。
従来の単色フラットな雷アイコンより魅力的にするため、
- 背景：暖色（太陽）→ 深紺（夜間・蓄電）の斜めグラデーション + ソフトな光彩（glow）
- 雷：上から下へ黄緑→ティールのグラデーション、輪郭線、ドロップシャドウ
を追加し、2倍スーパーサンプリング後にボックスダウンサンプルしてアンチエイリアスする。
"""
import struct
import zlib


def lerp(a, b, t):
    return a + (b - a) * t


def lerp_color(c1, c2, t):
    return tuple(lerp(c1[i], c2[i], t) for i in range(3))


def clamp01(v):
    return max(0.0, min(1.0, v))


# 雷ボルトの輪郭（0..1正規化座標、中央80%セーフゾーン基準でこの後スケールする）
BOLT_POINTS = [
    (0.58, 0.06),
    (0.30, 0.52),
    (0.46, 0.52),
    (0.34, 0.94),
    (0.74, 0.42),
    (0.56, 0.42),
    (0.70, 0.06),
]


def point_in_polygon(x, y, poly):
    n = len(poly)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside


def render(size_out: int) -> bytes:
    ss = 2  # スーパーサンプリング倍率
    size = size_out * ss

    # セーフゾーン（maskable対応：図柄を全体の80%に収める）
    margin = size * 0.10
    content = size - 2 * margin

    poly = [(margin + px * content, margin + py * content) for px, py in BOLT_POINTS]
    shadow_offset = size * 0.018
    poly_shadow = [(x + shadow_offset, y + shadow_offset) for x, y in poly]

    min_y = min(p[1] for p in poly)
    max_y = max(p[1] for p in poly)

    bg_top_left = (255, 200, 87)     # 太陽（暖色）
    bg_bottom_right = (11, 15, 20)   # 深紺（夜間）
    bolt_top = (181, 230, 29)        # 黄緑
    bolt_bottom = (16, 185, 129)     # ティール
    outline_color = (8, 60, 48)

    glow_cx, glow_cy = size * 0.40, size * 0.30
    glow_radius = size * 0.55

    pixels = bytearray(size * size * 3)

    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * size)
            r, g, b = lerp_color(bg_top_left, bg_bottom_right, t)

            # ソフトな光彩（中心からの距離で減衰する暖色の加算ブレンド）
            dx, dy = x - glow_cx, y - glow_cy
            dist = (dx * dx + dy * dy) ** 0.5
            glow = clamp01(1.0 - dist / glow_radius) ** 2 * 0.35
            r = r + (255 - r) * glow
            g = g + (220 - g) * glow
            b = b + (140 - b) * glow

            if point_in_polygon(x + 0.5, y + 0.5, poly_shadow) and not point_in_polygon(
                x + 0.5, y + 0.5, poly
            ):
                r, g, b = lerp_color((r, g, b), (0, 0, 0), 0.35)

            if point_in_polygon(x + 0.5, y + 0.5, poly):
                fy = clamp01((y - min_y) / max(1.0, (max_y - min_y)))
                fr, fg, fb = lerp_color(bolt_top, bolt_bottom, fy)
                r, g, b = fr, fg, fb

                # ごく薄い輪郭（境界付近のみ）でシルエットを締める
                near_edge = False
                for ox, oy in ((1.2, 0), (-1.2, 0), (0, 1.2), (0, -1.2)):
                    if not point_in_polygon(x + 0.5 + ox, y + 0.5 + oy, poly):
                        near_edge = True
                        break
                if near_edge:
                    r, g, b = lerp_color((r, g, b), outline_color, 0.45)

            idx = (y * size + x) * 3
            pixels[idx] = int(clamp01(r / 255) * 255)
            pixels[idx + 1] = int(clamp01(g / 255) * 255)
            pixels[idx + 2] = int(clamp01(b / 255) * 255)

    # ボックスダウンサンプル（ss x ss -> 1）でアンチエイリアス
    out = bytearray(size_out * size_out * 3)
    for oy in range(size_out):
        for ox in range(size_out):
            rs = gs = bs = 0
            for sy in range(ss):
                for sx in range(ss):
                    idx = ((oy * ss + sy) * size + (ox * ss + sx)) * 3
                    rs += pixels[idx]
                    gs += pixels[idx + 1]
                    bs += pixels[idx + 2]
            n = ss * ss
            oidx = (oy * size_out + ox) * 3
            out[oidx] = rs // n
            out[oidx + 1] = gs // n
            out[oidx + 2] = bs // n

    return bytes(out)


def write_png(path: str, size: int, rgb: bytes) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = bytearray()
    stride = size * 3
    for y in range(size):
        raw.append(0)
        raw.extend(rgb[y * stride : (y + 1) * stride])

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(raw), 9)

    with open(path, "wb") as f:
        f.write(sig)
        f.write(chunk(b"IHDR", ihdr))
        f.write(chunk(b"IDAT", idat))
        f.write(chunk(b"IEND", b""))


if __name__ == "__main__":
    for size, path in ((192, "icon-192.png"), (512, "icon-512.png")):
        rgb = render(size)
        write_png(path, size, rgb)
        print(f"wrote {path}")
