# 生成 QDirLite 的 exe/窗口图标 app.ico（纯标准库，无第三方依赖）。
# 图案与 qdir_lite.py 的 make_app_icon() 一致：2x2 四色格子，
# 每格四周留 1 单位透明边（32px 时即 1px）。
#
# 用法：.venv/Scripts/python.exe make_icon.py   → 生成 app.ico
# 改图后需重新打包：.venv/Scripts/pyinstaller.exe QDirLite.spec --noconfirm --clean

import struct
import zlib

COLORS = ((0x42, 0xA5, 0xF5), (0x66, 0xBB, 0x6A),   # 蓝、绿
          (0xFF, 0xA7, 0x26), (0xAB, 0x47, 0xBC))   # 橙、紫
SIZES = (16, 24, 32, 48, 64, 128, 256)


def render_rgba(size):
    """渲染 size×size 的 RGBA 像素（list[bytes]，每行 size*4 字节）"""
    m = max(1, round(size / 32))          # 每格内缩边距，32px 时为 1
    half = size // 2
    px = bytearray(size * size * 4)       # 全透明底
    for gy in range(2):
        for gx in range(2):
            r, g, b = COLORS[gy * 2 + gx]
            x0, x1 = gx * half + m, (gx + 1) * half - m
            y0, y1 = gy * half + m, (gy + 1) * half - m
            if gx == 1 and size % 2:
                x1 += 1
            if gy == 1 and size % 2:
                y1 += 1
            for y in range(y0, y1):
                row = (y * size + x0) * 4
                for x in range(x0, x1):
                    px[row:row + 4] = (r, g, b, 255)
                    row += 4
    return bytes(px)


def png_encode(rgba, size):
    """把 RGBA 像素编码为 PNG（filter 0，逐行）"""
    raw = b"".join(b"\x00" + rgba[y * size * 4:(y + 1) * size * 4]
                   for y in range(size))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def build_ico(path):
    images = [(s, png_encode(render_rgba(s), s)) for s in SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blob = [], []
    for size, data in images:
        w = 0 if size >= 256 else size    # ICO 目录中 256 记为 0
        entries.append(struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32,
                                   len(data), offset))
        blob.append(data)
        offset += len(data)
    with open(path, "wb") as f:
        f.write(header + b"".join(entries) + b"".join(blob))


if __name__ == "__main__":
    build_ico("app.ico")
    print("app.ico written:", ", ".join(f"{s}x{s}" for s in SIZES))
