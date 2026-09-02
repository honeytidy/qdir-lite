# -*- coding: utf-8 -*-
"""从 Windows Shell 获取与资源管理器一致的文件图标，转成 tkinter PhotoImage。

- 用真实路径查询 SHGetFileInfoW，返回文件类型关联程序（"打开方式"）的图标
- 普通文件按扩展名缓存；.exe/.lnk/.ico 与目录按具体路径缓存
- 图标经 32bpp DIB 读出像素，手工编码为 PNG 后交给 tk.PhotoImage
- 抓取（SHGetFileInfoW + DIB 像素读出，不碰 tk）与 PhotoImage 创建分离：
  fetch_icons 可在后台线程跑，peek_photo 只在主线程把缓存的 PNG 包成 PhotoImage
"""

import base64
import ctypes
import os
import struct
import threading
import tkinter as tk
import zlib
from ctypes import wintypes

SHGFI_ICON = 0x00000100
SHGFI_SMALLICON = 0x00000001
SM_CXSMICON = 49  # 小图标尺寸（DPI 感知进程下随缩放）
DI_NORMAL = 0x0003
DIB_RGB_COLORS = 0
BI_RGB = 0


class SHFILEINFOW(ctypes.Structure):
    _fields_ = [
        ("hIcon", wintypes.HICON),
        ("iIcon", ctypes.c_int),
        ("dwAttributes", wintypes.DWORD),
        ("szDisplayName", wintypes.WCHAR * 260),
        ("szTypeName", wintypes.WCHAR * 80),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


_shell32 = ctypes.windll.shell32
_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32

# 句柄/指针返回值必须显式声明 restype/argtypes：默认 int 会在 64 位下截断句柄，
# 句柄值超过 2^31 时 CreateDIBSection 等调用直接抛 OverflowError（间歇性图标丢失的根因）
_H = ctypes.c_void_p
_shell32.SHGetFileInfoW.restype = ctypes.c_size_t  # DWORD_PTR
_shell32.SHGetFileInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                    ctypes.POINTER(SHFILEINFOW),
                                    wintypes.UINT, wintypes.UINT]
_user32.GetSystemMetrics.argtypes = [ctypes.c_int]
_user32.GetDC.restype = _H
_user32.GetDC.argtypes = [_H]
_user32.ReleaseDC.argtypes = [_H, _H]
_user32.DrawIconEx.argtypes = [_H, ctypes.c_int, ctypes.c_int, _H,
                               ctypes.c_int, ctypes.c_int, wintypes.UINT,
                               _H, wintypes.UINT]
_user32.DestroyIcon.argtypes = [_H]
_gdi32.CreateCompatibleDC.restype = _H
_gdi32.CreateCompatibleDC.argtypes = [_H]
_gdi32.CreateDIBSection.restype = _H
_gdi32.CreateDIBSection.argtypes = [_H, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
                                    ctypes.POINTER(ctypes.c_void_p), _H, wintypes.DWORD]
_gdi32.SelectObject.restype = _H
_gdi32.SelectObject.argtypes = [_H, _H]
_gdi32.DeleteObject.argtypes = [_H]
_gdi32.DeleteDC.argtypes = [_H]

_cache_lock = threading.Lock()
_png_cache = {}    # key -> PNG 字节（None = Shell 没给出图标）
_photo_cache = {}  # key -> tk.PhotoImage（None = 无图标，列表用 emoji 回退）
NOT_READY = object()  # peek_photo 返回此哨兵：图标还没抓取


def _png_rgba(width, height, rgba):
    """把 RGBA 像素字节手工编码为 PNG 字节串"""

    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    stride = width * 4
    raw = b"".join(b"\x00" + rgba[y * stride:(y + 1) * stride]
                   for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _hicon_to_png(hicon):
    """把 HICON 绘制到 32bpp DIB，读出像素转成 PNG 字节（纯 Win32，可跑在后台线程）"""
    size = max(_user32.GetSystemMetrics(SM_CXSMICON), 16)
    hdc_screen = _user32.GetDC(None)
    hdc = _gdi32.CreateCompatibleDC(hdc_screen)
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = size
    bmi.bmiHeader.biHeight = -size  # 负值 = 自上而下，与 PNG 行序一致
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB
    bits = ctypes.c_void_p()
    hbmp = _gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), DIB_RGB_COLORS,
                                   ctypes.byref(bits), None, 0)
    if not hbmp or not bits.value:
        if hbmp:
            _gdi32.DeleteObject(hbmp)
        _gdi32.DeleteDC(hdc)
        _user32.ReleaseDC(None, hdc_screen)
        return None
    old = _gdi32.SelectObject(hdc, hbmp)
    ctypes.memset(bits.value, 0, size * size * 4)
    _user32.DrawIconEx(hdc, 0, 0, hicon, size, size, 0, None, DI_NORMAL)
    raw = ctypes.string_at(bits.value, size * size * 4)
    _gdi32.SelectObject(hdc, old)
    _gdi32.DeleteObject(hbmp)
    _gdi32.DeleteDC(hdc)
    _user32.ReleaseDC(None, hdc_screen)

    # BGRA（预乘 alpha） -> RGBA（非预乘）
    px = bytearray(size * size * 4)
    for i in range(0, size * size * 4, 4):
        b, g, r, a = raw[i], raw[i + 1], raw[i + 2], raw[i + 3]
        if a and a < 255:
            r = min(255, r * 255 // a)
            g = min(255, g * 255 // a)
            b = min(255, b * 255 // a)
        px[i:i + 4] = bytes((r, g, b, a))
    png = _png_rgba(size, size, bytes(px))
    return png


def icon_key(path, is_dir):
    """图标的缓存键：exe/lnk/ico 与目录按真实路径，普通文件按扩展名"""
    ext = "" if is_dir else os.path.splitext(path)[1].lower()
    if is_dir or ext in (".exe", ".lnk", ".ico"):
        return ("path", path)
    return ("ext", ext)


def _fetch_png(path):
    """查询 Shell 图标并转成 PNG 字节；失败返回 None（不碰 tk，线程安全）"""
    info = SHFILEINFOW()
    ok = _shell32.SHGetFileInfoW(path, 0, ctypes.byref(info),
                                 ctypes.sizeof(info),
                                 SHGFI_ICON | SHGFI_SMALLICON)
    if not (ok and info.hIcon):
        return None
    try:
        return _hicon_to_png(info.hIcon)
    finally:
        _user32.DestroyIcon(info.hIcon)


def fetch_icons(items):
    """后台线程批量抓取：items 为 [(key, path), ...]，结果写入 PNG 缓存"""
    for key, path in items:
        with _cache_lock:
            if key in _png_cache:
                continue
        try:
            png = _fetch_png(path)
        except Exception:
            png = None
        with _cache_lock:
            _png_cache[key] = png


def peek_photo(key):
    """主线程调用：返回缓存的 PhotoImage；未抓取返回 NOT_READY，无图标返回 None"""
    if key in _photo_cache:
        return _photo_cache[key]
    with _cache_lock:
        if key not in _png_cache:
            return NOT_READY
        png = _png_cache[key]
    photo = tk.PhotoImage(data=base64.b64encode(png)) if png else None
    _photo_cache[key] = photo
    return photo
