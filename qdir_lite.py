# -*- coding: utf-8 -*-
"""QDir-Lite: 极简多窗格文件管理器

功能：
1. 1~4 个窗格并列浏览文件，每个窗格独立导航；状态栏右端按钮切换单/双/三/四窗格；
2. 双击窗格标题栏或列表空白处：最大化该窗格（窗口同步放大），再次双击还原；
3. 右键菜单与资源管理器一致（空白处右键含"新建"子菜单）；原生菜单不可用时回退内置简易菜单；
4. 文件图标直接取自 Windows 系统（与资源管理器一致），字体与文字颜色均为系统默认；
5. 目录被外部改动（资源管理器/其他程序增删文件）约 2 秒内自动刷新，滚动与选中状态保留。

操作：
- 双击文件夹：进入；双击文件：系统默认程序打开；回车：打开选中项
- Backspace：返回上级；Ctrl+左键：唤出标题栏（按钮+地址栏），输入路径回车跳转，Esc 收起
- Ctrl+C / Ctrl+X / Ctrl+V：复制 / 剪切（项置灰）/ 粘贴（走系统剪贴板，与资源管理器互通）
- Ctrl+A：全选；Delete：删除选中项
- Ctrl+E / 双击状态栏路径：在资源管理器中打开当前窗格的目录
- 重命名：右键"重命名"或选中项再次单击（慢双击）进入行内改名；新建后自动进入改名
- 按住左键拖动：画出矩形选框，框住的文件即被选中（配合 Ctrl 追加选择）
"""

import ctypes
import json
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import ttk, messagebox, simpledialog

try:
    import fileicons
except Exception:
    fileicons = None

# ---------- 高 DPI 感知：解决文字模糊 ----------
def _enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except OSError:
            pass


_enable_dpi_awareness()
try:
    _DPI = ctypes.windll.user32.GetDpiForSystem()
except (AttributeError, OSError):
    _DPI = 96
SCALE = _DPI / 96.0


def S(v):
    """按系统 DPI 缩放像素尺寸（字体用磅值，由 tk scaling 自动缩放）"""
    return int(round(v * SCALE))


# ---------- 系统默认 UI 字体（中英文一致，跟随系统设置） ----------
class _LOGFONTW(ctypes.Structure):
    _fields_ = [
        ("lfHeight", wintypes.LONG), ("lfWidth", wintypes.LONG),
        ("lfEscapement", wintypes.LONG), ("lfOrientation", wintypes.LONG),
        ("lfWeight", wintypes.LONG), ("lfItalic", wintypes.BYTE),
        ("lfUnderline", wintypes.BYTE), ("lfStrikeOut", wintypes.BYTE),
        ("lfCharSet", wintypes.BYTE), ("lfOutPrecision", wintypes.BYTE),
        ("lfClipPrecision", wintypes.BYTE), ("lfQuality", wintypes.BYTE),
        ("lfPitchAndFamily", wintypes.BYTE), ("lfFaceName", wintypes.WCHAR * 32),
    ]


class _NONCLIENTMETRICSW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("iBorderWidth", ctypes.c_int), ("iScrollWidth", ctypes.c_int),
        ("iScrollHeight", ctypes.c_int), ("iCaptionWidth", ctypes.c_int),
        ("iCaptionHeight", ctypes.c_int), ("lfCaptionFont", _LOGFONTW),
        ("iSMCaptionWidth", ctypes.c_int), ("iSMCaptionHeight", ctypes.c_int),
        ("lfSMCaptionFont", _LOGFONTW),
        ("iMenuWidth", ctypes.c_int), ("iMenuHeight", ctypes.c_int),
        ("lfMenuFont", _LOGFONTW), ("lfStatusFont", _LOGFONTW),
        ("lfMessageFont", _LOGFONTW),
        ("iPaddedBorderWidth", ctypes.c_int),
    ]


def _default_ui_font():
    """读取 Windows 默认 UI 字体（资源管理器同款），失败回退 Segoe UI 9"""
    try:
        ncm = _NONCLIENTMETRICSW()
        ncm.cbSize = ctypes.sizeof(ncm)
        if ctypes.windll.user32.SystemParametersInfoW(
                0x0029, ncm.cbSize, ctypes.byref(ncm), 0):  # SPI_GETNONCLIENTMETRICS
            lf = ncm.lfMessageFont
            size = max(round(-lf.lfHeight * 72 / _DPI), 8) if lf.lfHeight < 0 else 9
            return lf.lfFaceName or "Segoe UI", size
    except Exception:
        pass
    return "Segoe UI", 9


FONT_FAMILY, FONT_SIZE = _default_ui_font()


# ---------- 图标后台抓取：列表先出文字，图标抓完再补，避免刷新卡住 UI ----------
_icon_jobs = queue.Queue()   # (pane, gen, [(key, path), ...])
_icon_done = queue.Queue()   # 抓取完成的 (pane, gen)
_paste_done = queue.Queue()  # 后台粘贴完成的 (pane, paths, is_cut, dst_dir, done, errors)
_CUT_PATHS = set()           # 本次 Ctrl+X 剪切的路径（列表里置灰显示）


def _icon_worker():
    try:
        ctypes.windll.ole32.CoInitialize(None)  # 部分 Shell 扩展需要 COM
    except Exception:
        pass
    while True:
        job = _icon_jobs.get()
        pane, gen, items = job
        try:
            fileicons.fetch_icons(items)
        except Exception:
            pass
        _icon_done.put((pane, gen))


# ---------- 运行状态持久化：下次启动恢复上次关闭时的窗口与目录 ----------
def _state_file():
    """状态文件路径：优先放程序旁边（便携），目录不可写时回退 %APPDATA%"""
    base = os.path.dirname(sys.executable if getattr(sys, "frozen", False)
                           else os.path.abspath(__file__))
    path = os.path.join(base, "qdir_lite_state.json")
    if os.path.exists(path) or os.access(base, os.W_OK):
        return path
    return os.path.join(os.environ.get("APPDATA", base), "QDirLite", "state.json")


def _load_state():
    """读取上次状态；文件不存在或损坏一律返回 None，静默走默认"""
    try:
        with open(_state_file(), encoding="utf-8") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else None
    except Exception:
        return None

# ---------- 系统剪贴板文件操作（CF_HDROP，与资源管理器互通） ----------
_CF_HDROP = 15
_GMEM_MOVEABLE = 0x0002
_DROP_EFFECT_FMT = None

# 句柄/指针返回值必须显式声明，否则 ctypes 默认 int 会在 64 位下截断
_kernel32 = ctypes.windll.kernel32
_kernel32.GlobalAlloc.restype = ctypes.c_void_p
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
_user32 = ctypes.windll.user32
_user32.SetClipboardData.restype = ctypes.c_void_p
_user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
_user32.GetClipboardData.restype = ctypes.c_void_p
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.GetDC.restype = ctypes.c_void_p
_user32.GetDC.argtypes = [ctypes.c_void_p]
_user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_user32.InvalidateRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL]
_user32.UpdateWindow.argtypes = [ctypes.c_void_p]
_user32.FrameRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]


# ---------- 橡皮筋选框（GDI 直接画在列表控件 DC 上，资源管理器同款） ----------
_gdi32 = ctypes.windll.gdi32
_msimg32 = ctypes.windll.msimg32
_gdi32.CreateSolidBrush.restype = ctypes.c_void_p
_gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
_gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
_gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
_gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
_gdi32.SelectObject.restype = ctypes.c_void_p
_gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_gdi32.SetPixelV.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
_gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
_gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
_msimg32.AlphaBlend.argtypes = [ctypes.c_void_p] + [ctypes.c_int] * 4 \
    + [ctypes.c_void_p] + [ctypes.c_int] * 4 + [wintypes.DWORD]

_RUBBER_FILL = 0x00FFDDAA    # 填充色 #AADDFF（COLORREF 是 BGR）
_RUBBER_BORDER = 0x00D59B5B  # 边框色 #5B9BD5
_RUBBER_ALPHA = 45           # 填充不透明度（0~255）


def _draw_rubber(hwnd, rect):
    """在控件上画半透明选框：1x1 纯色位图 AlphaBlend 填充 + FrameRect 边框"""
    x0, y0, x1, y1 = rect
    if x1 - x0 < 2 or y1 - y0 < 2:
        return
    hdc = _user32.GetDC(hwnd)
    if not hdc:
        return
    mem = bmp = old = brush = None
    try:
        mem = _gdi32.CreateCompatibleDC(hdc)
        bmp = _gdi32.CreateCompatibleBitmap(hdc, 1, 1)
        old = _gdi32.SelectObject(mem, bmp)
        _gdi32.SetPixelV(mem, 0, 0, _RUBBER_FILL)
        # BLENDFUNCTION: AC_SRC_OVER + SourceConstantAlpha（按值传，一个 DWORD）
        blend = struct.unpack("<I", struct.pack("BBBB", 0, 0, _RUBBER_ALPHA, 0))[0]
        _msimg32.AlphaBlend(hdc, x0, y0, x1 - x0, y1 - y0,
                            mem, 0, 0, 1, 1, blend)
        brush = _gdi32.CreateSolidBrush(_RUBBER_BORDER)
        r = wintypes.RECT(x0, y0, x1, y1)
        _user32.FrameRect(hdc, ctypes.byref(r), brush)
    finally:
        if brush:
            _gdi32.DeleteObject(brush)
        if old:
            _gdi32.SelectObject(mem, old)
        if bmp:
            _gdi32.DeleteObject(bmp)
        if mem:
            _gdi32.DeleteDC(mem)
        _user32.ReleaseDC(hwnd, hdc)


def _erase_rubber(hwnd, rect):
    """擦掉选框：让该区域立即重绘"""
    r = wintypes.RECT(*rect)
    _user32.InvalidateRect(hwnd, ctypes.byref(r), True)
    _user32.UpdateWindow(hwnd)


def _drop_effect_fmt():
    """Preferred DropEffect 剪贴板格式：1=复制 2=剪切（Explorer 约定）"""
    global _DROP_EFFECT_FMT
    if _DROP_EFFECT_FMT is None:
        _DROP_EFFECT_FMT = ctypes.windll.user32.RegisterClipboardFormatW(
            "Preferred DropEffect")
    return _DROP_EFFECT_FMT


def _open_clipboard():
    """打开剪贴板，被占用时短暂重试"""
    u32 = ctypes.windll.user32
    for _ in range(10):
        if u32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def set_clipboard_files(paths, cut=False):
    """把文件路径列表写入系统剪贴板（Explorer 里可直接 Ctrl+V）"""
    if not paths:
        return False
    u32, k32 = _user32, _kernel32
    blob = ("\0".join(os.path.normpath(p) for p in paths) + "\0\0").encode("utf-16-le")
    if not _open_clipboard():
        return False
    hglob = None
    try:
        u32.EmptyClipboard()
        size = 20 + len(blob)  # DROPFILES 头 20 字节 + 双 \0 结尾的路径列表
        hglob = k32.GlobalAlloc(_GMEM_MOVEABLE, size)
        if not hglob:
            return False
        p = k32.GlobalLock(hglob)
        if not p:
            return False
        ctypes.memmove(p, struct.pack("<IiiII", 20, 0, 0, 0, 1) + blob, size)
        k32.GlobalUnlock(hglob)
        if not u32.SetClipboardData(_CF_HDROP, hglob):
            return False
        hglob = None  # 所有权已移交系统
        # 附带 DropEffect，让 Explorer 知道是复制还是剪切
        heffect = k32.GlobalAlloc(_GMEM_MOVEABLE, 4)
        if heffect:
            p = k32.GlobalLock(heffect)
            if p:
                ctypes.memmove(p, struct.pack("<I", 2 if cut else 1), 4)
                k32.GlobalUnlock(heffect)
            if not u32.SetClipboardData(_DROP_EFFECT_FMT or _drop_effect_fmt(),
                                        heffect):
                k32.GlobalFree(heffect)
        return True
    finally:
        if hglob:
            k32.GlobalFree(hglob)
        u32.CloseClipboard()


def get_clipboard_files():
    """读取剪贴板里的文件列表；返回 (paths, is_cut)，没有文件则返回 None"""
    u32, k32 = _user32, _kernel32
    sh32 = ctypes.windll.shell32
    sh32.DragQueryFileW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                    wintypes.LPWSTR, ctypes.c_uint]
    sh32.DragQueryFileW.restype = ctypes.c_uint
    if not u32.IsClipboardFormatAvailable(_CF_HDROP):
        return None
    if not _open_clipboard():
        return None
    try:
        h = u32.GetClipboardData(_CF_HDROP)
        if not h:
            return None
        paths = []
        for i in range(sh32.DragQueryFileW(h, 0xFFFFFFFF, None, 0)):
            n = sh32.DragQueryFileW(h, i, None, 0)
            buf = ctypes.create_unicode_buffer(n + 1)
            sh32.DragQueryFileW(h, i, buf, n + 1)
            paths.append(buf.value)
        if not paths:
            return None
        is_cut = False
        fmt = _drop_effect_fmt()
        if u32.IsClipboardFormatAvailable(fmt):
            he = u32.GetClipboardData(fmt)
            if he:
                p = k32.GlobalLock(he)
                if p:
                    is_cut = bool(ctypes.cast(
                        p, ctypes.POINTER(ctypes.c_uint32)).contents.value & 2)
                    k32.GlobalUnlock(he)
        return paths, is_cut
    finally:
        u32.CloseClipboard()


# ---------- 文件类型分类（emoji 仅作系统图标取不到时的回退） ----------
CATEGORIES = {
    "folder":  "📁",
    "image":   "🖼️",
    "audio":   "🎵",
    "video":   "🎬",
    "code":    "💻",
    "doc":     "📄",
    "archive": "📦",
    "app":     "⚙️",
    "file":    "📃",
}

EXT_MAP = {}
for _exts, _cat in [
    ((".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".ico", ".tif", ".tiff"), "image"),
    ((".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma"), "audio"),
    ((".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts"), "video"),
    ((".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".cpp", ".h", ".hpp",
      ".cs", ".go", ".rs", ".html", ".css", ".json", ".xml", ".yml", ".yaml",
      ".toml", ".md", ".sh", ".bat", ".ps1", ".sql", ".vue"), "code"),
    ((".txt", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv",
      ".rtf", ".epub"), "doc"),
    ((".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso"), "archive"),
    ((".exe", ".msi", ".com", ".lnk"), "app"),
]:
    for _e in _exts:
        EXT_MAP[_e] = _cat


def category_of(path, is_dir):
    if is_dir:
        return "folder"
    return EXT_MAP.get(os.path.splitext(path)[1].lower(), "file")


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fmt_time(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def make_app_icon():
    """生成 2x2 四格窗口图标（Q-Dir 风格）"""
    img = tk.PhotoImage(width=32, height=32)
    colors = ("#42a5f5", "#66bb6a", "#ffa726", "#ab47bc")
    for gy in range(2):
        for gx in range(2):
            c = colors[gy * 2 + gx]
            x0, y0 = gx * 16, gy * 16
            for y in range(y0 + 1, y0 + 15):
                for x in range(x0 + 1, x0 + 15):
                    img.put(c, (x, y))
    return img


def make_layout_icon(n, color="#5a6b82", bg="#eef2f8"):
    """绘制布局示意小图标：用矩形块表示 1/2/3/4 窗格的划分方式"""
    w = S(16)
    img = tk.PhotoImage(width=w, height=w)
    img.put(bg, to=(0, 0, w, w))
    m, gap, t = S(2), max(S(1), 1), max(S(1), 1)  # 外边距/间距/线宽
    x0, y0, x1, y1 = m, m, w - m, w - m
    mx, my = (x0 + x1) // 2, (y0 + y1) // 2
    if n == 1:
        rects = [(x0, y0, x1, y1)]
    elif n == 2:
        rects = [(x0, y0, mx - gap, y1), (mx + gap, y0, x1, y1)]
    elif n == 3:
        rects = [(x0, y0, mx - gap, y1),
                 (mx + gap, y0, x1, my - gap), (mx + gap, my + gap, x1, y1)]
    else:
        rects = [(x0, y0, mx - gap, my - gap), (mx + gap, y0, x1, my - gap),
                 (x0, my + gap, mx - gap, y1), (mx + gap, my + gap, x1, y1)]
    for a, b, c, d in rects:  # 画矩形边框（四条细线）
        img.put(color, to=(a, b, c, b + t))
        img.put(color, to=(a, d - t, c, d))
        img.put(color, to=(a, b, a + t, d))
        img.put(color, to=(c - t, b, c, d))
    return img


class MiniScrollbar(tk.Canvas):
    """极简悬浮滚动条：一条几像素宽的细条，覆盖在列表右缘，不占布局空间。

    - 内容不足一屏时自动隐藏滑块
    - 拖动滑块滚动；点击滑槽翻页；悬停时加深颜色
    """

    THUMB = "#c4d0e0"
    THUMB_HOVER = "#93aac7"

    def __init__(self, master, command, width):
        super().__init__(master, width=width, highlightthickness=0, bd=0,
                         bg="#ffffff", takefocus=0)
        self.command = command
        self._lo, self._hi = 0.0, 1.0
        self._grab = None
        self._thumb = self.create_rectangle(0, 0, width, 0,
                                            fill=self.THUMB, outline="")
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", lambda e: setattr(self, "_grab", None))
        self.bind("<Enter>", lambda e: self.itemconfigure(self._thumb, fill=self.THUMB_HOVER))
        self.bind("<Leave>", lambda e: self.itemconfigure(self._thumb, fill=self.THUMB))
        self.bind("<Configure>", lambda e: self.set(self._lo, self._hi))

    def set(self, lo, hi):
        """yscrollcommand 回调：first, last 为 0~1 的可见范围"""
        self._lo, self._hi = float(lo), float(hi)
        h = self.winfo_height()
        if h < 2:
            self.after(10, lambda: self.set(lo, hi))
            return
        if self._hi - self._lo >= 1.0:
            self.coords(self._thumb, 0, 0, 0, 0)  # 无需滚动，隐藏
            return
        y0 = self._lo * h
        y1 = max(self._hi * h, y0 + 12)  # 滑块最小高度
        self.coords(self._thumb, 0, y0, self.winfo_width(), min(y1, h))

    def _on_press(self, event):
        h = max(self.winfo_height(), 1)
        y0, y1 = self._lo * h, self._hi * h
        if y0 <= event.y <= y1:
            self._grab = event.y - y0
        else:
            self.command("scroll", 1 if event.y > y1 else -1, "pages")
            self._grab = None

    def _on_drag(self, event):
        if self._grab is None:
            return
        h = max(self.winfo_height(), 1)
        span = self._hi - self._lo
        lo = (event.y - self._grab) / h
        lo = min(max(lo, 0.0), max(1.0 - span, 0.0))
        self.command("moveto", lo)


class Pane(ttk.Frame):
    """单个文件浏览窗格"""

    def __init__(self, master, app, start_path):
        super().__init__(master, relief="flat", borderwidth=0)
        self.app = app
        self.path = os.path.abspath(start_path)
        self._status_text = ""
        self._build_header()
        self._build_tree()
        self._build_menu()
        self.refresh()

    # ---------- UI 构建 ----------
    def _build_header(self):
        # 标题栏默认不 pack（隐藏），Ctrl+左键点击窗格时才整块唤出
        self.header = tk.Frame(self, bg="#eef2f8")

        btn_cfg = dict(relief="flat", bd=0, bg="#eef2f8", activebackground="#d7e3f5",
                       font=(FONT_FAMILY, FONT_SIZE), cursor="hand2", padx=2, pady=0)
        self.up_btn = tk.Button(self.header, text="⬆", command=self.go_up, **btn_cfg)
        self.up_btn.pack(side="left", padx=(1, 0))
        self.re_btn = tk.Button(self.header, text="⟳", command=self.refresh, **btn_cfg)
        self.re_btn.pack(side="left")

        self.path_var = tk.StringVar(value=self.path)
        self.entry_frame = tk.Frame(self.header, bg="white",
                                    highlightbackground="#c5d3e8", highlightthickness=1)
        # 地址栏默认不 pack（隐藏），Ctrl+左键点击窗格时才显示
        self.path_entry = tk.Entry(self.entry_frame, textvariable=self.path_var, bd=0,
                                   font=(FONT_FAMILY, FONT_SIZE), highlightthickness=0)
        self.path_entry.pack(fill="x", padx=2, pady=0)
        self.path_entry.bind("<Return>", self._on_path_enter)
        self.path_entry.bind("<Escape>", self._on_path_escape)
        self.path_entry.bind("<Double-1>", lambda e: self.app.toggle_maximize(self))

        # 双击标题栏空白区 -> 最大化/还原
        self.header.bind("<Double-1>", lambda e: self.app.toggle_maximize(self))
        for b in (self.up_btn, self.re_btn):
            b.bind("<Double-1>", lambda e: self.app.toggle_maximize(self))

    # ---------- 标题栏（按钮+地址栏）显隐 ----------
    def show_address(self):
        """唤出标题栏和地址栏并聚焦，全选路径便于直接输入"""
        self.header.pack(fill="x", before=self.tree)
        self.entry_frame.pack(side="left", fill="x", expand=True, padx=2, pady=0)
        self.path_entry.focus_set()
        self.path_entry.select_range(0, "end")

    def hide_address(self):
        # 地址栏在标题栏内部，隐藏标题栏即整块收起
        self.header.pack_forget()

    def _build_tree(self):
        self.sort_col = "#0"        # 当前排序列：#0=名称 / size=大小 / mtime=修改时间
        self.sort_reverse = False
        self._entries = []
        self._heading_texts = {"#0": "名称", "size": "大小", "mtime": "修改时间"}

        cols = ("size", "mtime")
        self.tree = ttk.Treeview(self, columns=cols, show="tree headings",
                                 selectmode="extended")
        for col in ("#0", "size", "mtime"):
            self.tree.heading(col, text="", anchor="w" if col != "size" else "e",
                              command=lambda c=col: self.sort_by(c))
        self.tree.column("#0", width=S(200), stretch=True)
        self.tree.column("size", width=S(64), stretch=False, anchor="e")
        self.tree.column("mtime", width=S(112), stretch=False)
        self._update_headings()

        # 悬浮细滚动条：覆盖在列表右缘，宽度仅几像素，不占用布局空间
        self.tree.pack(fill="both", expand=True)
        self.sb = MiniScrollbar(self, self.tree.yview, width=S(4))
        self.tree.configure(yscrollcommand=self.sb.set)
        self.sb.place(in_=self.tree, relx=1.0, rely=0.0, anchor="ne",
                      relheight=1.0, width=S(4))
        self.sb.tk.call("raise", self.sb._w, self.tree._w)  # 浮于列表之上

        # 类型颜色 + 隔行条纹 + 剪切置灰
        self.tree.tag_configure("stripe", background="#f5f8fc")
        self.tree.tag_configure("cut", foreground="#a8b0bc")

        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<BackSpace>", lambda e: self.go_up())
        self.tree.bind("<Return>", self._on_return)
        self.tree.bind("<Button-3>", self._on_right_click)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_status())
        # 按住左键拖动：画出资源管理器式矩形选框，框住的行即被选中
        self._drag = None
        self.tree.bind("<ButtonPress-1>", self._on_press_1, add="+")
        self.tree.bind("<B1-Motion>", self._on_drag_1)
        self.tree.bind("<ButtonRelease-1>", self._on_release_1)
        # 文件操作快捷键（复制/剪切/粘贴走系统剪贴板，与资源管理器互通）
        for seq in ("<Control-c>", "<Control-C>"):
            self.tree.bind(seq, lambda e: self._copy_sel(cut=False))
        for seq in ("<Control-x>", "<Control-X>"):
            self.tree.bind(seq, lambda e: self._copy_sel(cut=True))
        for seq in ("<Control-v>", "<Control-V>"):
            self.tree.bind(seq, lambda e: self._paste())
        for seq in ("<Control-a>", "<Control-A>"):
            self.tree.bind(seq, self._select_all)
        for seq in ("<Control-e>", "<Control-E>"):
            self.tree.bind(seq, lambda e: self._open_in_explorer())
        self.tree.bind("<Delete>", lambda e: self._menu_delete())

    def _build_menu(self):
        self.menu = tk.Menu(self, tearoff=0, font=(FONT_FAMILY, FONT_SIZE))
        self.menu.add_command(label="打开", command=self._menu_open)
        self.menu.add_command(label="在资源管理器中显示", command=self._menu_reveal)
        self.menu.add_command(label="在资源管理器中打开目录",
                              command=self._open_in_explorer)
        self.menu.add_separator()
        self.menu.add_command(label="复制路径", command=self._menu_copy_path)
        self.menu.add_command(label="重命名...", command=self._menu_rename)
        self.menu.add_command(label="删除...", command=self._menu_delete)
        self.menu.add_separator()
        self.menu.add_command(label="新建文件夹", command=self._menu_new_folder)
        self.menu.add_command(label="新建文本文档", command=self._menu_new_text_file)
        self.menu.add_command(label="刷新", command=self.refresh)
        self.menu.add_separator()
        self.menu.add_command(label="属性", command=self._menu_properties)

    # ---------- 数据 ----------
    def refresh(self):
        """重新扫描当前目录，缓存条目后按当前排序方式填充"""
        # 目录可能刚被剪切走/删除：向上回退到最近存在的祖先
        while not os.path.isdir(self.path):
            parent = os.path.dirname(self.path)
            if parent == self.path:
                return
            self.path = parent
        self.path_var.set(self.path)
        # 同目录刷新（自动刷新/粘贴后）时保留滚动位置和选中项，不打断浏览
        keep = self.path == getattr(self, "_last_refresh_path", None)
        sel = self.tree.selection() if keep else ()
        top = self.tree.yview()[0] if keep and self.tree.get_children() else 0.0
        try:
            scanned = list(os.scandir(self.path))
        except OSError as e:
            messagebox.showerror("无法打开目录", str(e), parent=self)
            return
        try:
            self._dir_mtime = os.stat(self.path).st_mtime  # 供外部改动轮询对比
        except OSError:
            self._dir_mtime = None
        self._last_refresh_path = self.path
        self._entries = []
        for e in scanned:
            try:
                st = e.stat()
                size, mtime = st.st_size, st.st_mtime
            except OSError:
                size, mtime = 0, 0.0
            self._entries.append({
                "path": e.path, "name": e.name, "is_dir": e.is_dir(),
                "size": size, "mtime": mtime,
                "cat": category_of(e.path, e.is_dir()),
            })
        self._fill()
        if keep:
            still = [i for i in sel if self.tree.exists(i)]
            if still:
                self.tree.selection_set(still)
            self.tree.yview_moveto(top)

    def sort_by(self, col):
        """点击列头排序；重复点击同一列切换升/降序"""
        if col == self.sort_col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_col = col
            self.sort_reverse = False
        self._fill()

    def _fill(self):
        key_funcs = {
            "#0": lambda e: e["name"].lower(),
            "size": lambda e: e["size"],
            "mtime": lambda e: e["mtime"],
        }
        key = key_funcs[self.sort_col]
        # 目录始终排在文件前面，目录与文件各自按所选列排序
        dirs = sorted((e for e in self._entries if e["is_dir"]),
                      key=key, reverse=self.sort_reverse)
        files = sorted((e for e in self._entries if not e["is_dir"]),
                       key=key, reverse=self.sort_reverse)
        self.tree.delete(*self.tree.get_children())
        # 图标优先用缓存；没缓存的先无图标插入，交给后台线程抓完再补（不阻塞 UI）
        self._icon_gen = getattr(self, "_icon_gen", 0) + 1
        pending = {}  # key -> [iid, ...]
        for i, e in enumerate(dirs + files):
            img, text = "", " " + e["name"]
            if fileicons is not None:
                key = fileicons.icon_key(e["path"], e["is_dir"])
                photo = fileicons.peek_photo(key)
                if photo is fileicons.NOT_READY:
                    pending.setdefault(key, []).append(e["path"])
                elif photo is not None:
                    img = photo
                else:
                    text = f"{CATEGORIES[e['cat']]} {e['name']}"  # 无图标，emoji 回退
            else:
                text = f"{CATEGORIES[e['cat']]} {e['name']}"
            size = "" if e["is_dir"] else fmt_size(e["size"])
            mtime = fmt_time(e["mtime"]) if e["mtime"] else ""
            tags = ["stripe"] if i % 2 else []
            if e["path"] in _CUT_PATHS:
                tags.append("cut")  # 被剪切的项置灰
            self.tree.insert("", "end", iid=e["path"], image=img,
                             text=text,
                             values=(size, mtime), tags=tags)
        if pending:
            self._pending_icons = pending
            _icon_jobs.put((self, self._icon_gen,
                            [(k, iids[0]) for k, iids in pending.items()]))
        self._update_headings()
        self._update_status()

    def _apply_icons(self, gen):
        """后台图标抓取完成：给等待中的行补上图标（或 emoji 回退）"""
        if gen != getattr(self, "_icon_gen", 0):  # 期间又刷新过，结果已过期
            return
        pending = getattr(self, "_pending_icons", None)
        if not pending:
            return
        self._pending_icons = {}
        for key, iids in pending.items():
            photo = fileicons.peek_photo(key)
            if photo is fileicons.NOT_READY:
                continue
            for iid in iids:
                if not self.tree.exists(iid):
                    continue
                if photo is not None:
                    self.tree.item(iid, image=photo)
                else:
                    cat = category_of(iid, os.path.isdir(iid))
                    self.tree.item(iid,
                                   text=f"{CATEGORIES[cat]} {os.path.basename(iid)}")

    def _update_headings(self):
        for col, text in self._heading_texts.items():
            if col == self.sort_col:
                text += " ▼" if not self.sort_reverse else " ▲"
            self.tree.heading(col, text=" " + text)

    def _update_status(self):
        total = len(self.tree.get_children())
        sel = len(self.tree.selection())
        text = f"{total} 个项目"
        if sel:
            text += f"　已选 {sel} 个"
        self._status_text = text
        self.app.set_status(self)  # 仅当本窗格为焦点窗格时才真正显示

    # ---------- 导航 ----------
    def go_up(self):
        parent = os.path.dirname(self.path)
        if parent and parent != self.path:
            self.path = parent
            self.refresh()

    def open_item(self, iid):
        if os.path.isdir(iid):
            self.path = os.path.abspath(iid)
            self.refresh()
        else:
            try:
                os.startfile(iid)
            except OSError as e:
                messagebox.showerror("无法打开文件", str(e), parent=self)

    # ---------- 矩形框选（橡皮筋） ----------
    def _on_press_1(self, event):
        # 只在数据区/空白区记录拖动起点（表头留给列宽拖拽）；默认单击行为不变
        # region: heading / separator / tree / cell / nothing（行下方的空白）
        if self.tree.identify_region(event.x, event.y) not in ("heading",
                                                               "separator"):
            ctrl = bool(event.state & 0x0004)
            row = self.tree.identify_row(event.y)
            # 资源管理器式慢双击改名：唯一选中的项时隔双击间隔后被再次单击
            now = time.time()
            prev_row, prev_t = getattr(self, "_last_click", (None, 0.0))
            dbl = _user32.GetDoubleClickTime() / 1000
            self._rename_candidate = (
                row if row and row == prev_row and now - prev_t > dbl
                and list(self.tree.selection()) == [row] else None)
            self._last_click = (row, now)
            # 点在空白处（不在任何行上）：清空已选；Ctrl 按住时保留（追加框选）
            if not row and not ctrl:
                self.tree.selection_set()
            self._drag = {"x": event.x, "y": event.y,
                          "additive": ctrl,
                          "active": False, "base": [], "rect": None}
        else:
            self._drag = None
            self._rename_candidate = None

    def _on_drag_1(self, event):
        d = self._drag
        if not d:
            return
        if not d["active"]:
            if abs(event.x - d["x"]) < S(4) and abs(event.y - d["y"]) < S(4):
                return  # 位移太小，视为单击，不接管
            d["active"] = True
            d["base"] = list(self.tree.selection()) if d["additive"] else []
        # 选框矩形（归一化）
        x0, x1 = sorted((d["x"], event.x))
        y0, y1 = sorted((d["y"], event.y))
        rect = (x0, y0, x1, y1)
        hwnd = self.tree.winfo_id()
        # 与选框相交的行入选（滚出可视区的行没有 bbox，自然排除）
        sel = []
        for iid in self.tree.get_children():
            b = self.tree.bbox(iid)
            if b and b[0] < x1 and b[0] + b[2] > x0 \
                    and b[1] < y1 and b[1] + b[3] > y0:
                sel.append(iid)
        self.tree.selection_set(d["base"] + sel)
        # 旧框区域标脏；选框推迟到 after_idle 再画——tk 的重绘也在空闲时执行，
        # 同步绘制会被随后的选中态重绘覆盖，等它画完再把选框画在最上层
        if d["rect"]:
            r = wintypes.RECT(*d["rect"])
            _user32.InvalidateRect(hwnd, ctypes.byref(r), True)
        d["rect"] = rect
        if not d.get("draw_pending"):
            d["draw_pending"] = True
            self.after_idle(self._draw_pending_rubber)
        # 拖出上下边缘时自动滚动
        if event.y < 0:
            self.tree.yview_scroll(-1, "units")
        elif event.y > self.tree.winfo_height():
            self.tree.yview_scroll(1, "units")
        return "break"

    def _draw_pending_rubber(self):
        d = self._drag
        if not d:
            return
        d["draw_pending"] = False
        if d["active"] and d["rect"]:
            _draw_rubber(self.tree.winfo_id(), d["rect"])

    def _on_release_1(self, event):
        d, self._drag = self._drag, None
        if d and d["active"] and d["rect"]:
            _erase_rubber(self.tree.winfo_id(), d["rect"])
            self._rename_candidate = None
            return
        # 没拖动出选框：慢双击的第二次单击落定，进入行内重命名
        cand = getattr(self, "_rename_candidate", None)
        self._rename_candidate = None
        if cand and self.tree.identify_row(event.y) == cand \
                and list(self.tree.selection()) == [cand]:
            self._begin_rename(cand)

    # ---------- 事件 ----------
    def _on_path_enter(self, event):
        p = self.path_var.get().strip().strip('"')
        if os.path.isdir(p):
            self.path = os.path.abspath(p)
            self.refresh()
            self.hide_address()  # 跳转成功，收起地址栏
            self.tree.focus_set()
        else:
            messagebox.showerror("路径无效", f"目录不存在：{p}", parent=self)
            self.path_var.set(self.path)

    def _on_path_escape(self, event):
        """放弃编辑：恢复当前路径并收起地址栏"""
        self.path_var.set(self.path)
        self.hide_address()
        self.tree.focus_set()
        return "break"

    def _on_double_click(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.open_item(row)
        else:
            # 双击列表空白处 -> 最大化/还原当前窗格
            self.app.toggle_maximize(self)
        return "break"

    def _on_return(self, event):
        sel = self.tree.selection()
        if sel:
            self.open_item(sel[0])
        return "break"

    def _on_right_click(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            if row not in self.tree.selection():
                self.tree.selection_set(row)
        else:
            self.tree.selection_set()
        sel = list(self.tree.selection())
        try:
            # 优先使用与 Windows 资源管理器一致的原生右键菜单
            from shellmenu import show_shell_menu
            hwnd = self.winfo_toplevel().winfo_id()
            before = None
            if sel:
                result = show_shell_menu(hwnd, sel, event.x_root, event.y_root)
            else:
                # 点在空白处：目录项目菜单 + 背景菜单拆出的"新建"子菜单
                before = {e["name"] for e in self._entries}
                result = show_shell_menu(hwnd, [self.path], event.x_root,
                                         event.y_root, background=True)
            self.refresh()  # 菜单可能执行了删除/新建等操作
            if result == "rename" and sel:
                # shell 的"重命名"verb 在本程序是空操作，走自实现行内重命名
                self._begin_rename(sel[0])
            elif result and before is not None:
                # 新建出来的项：选中并进入改名状态（与资源管理器一致）
                added = [e for e in self._entries if e["name"] not in before]
                if len(added) == 1:
                    self._begin_rename(added[0]["path"])
        except Exception:
            # 回退到内置简易菜单
            self._popup_fallback_menu(event, bool(sel))

    def _popup_fallback_menu(self, event, has_selection):
        state = "normal" if has_selection else "disabled"
        for label in ("打开", "在资源管理器中显示", "复制路径",
                      "重命名...", "删除...", "属性"):
            self.menu.entryconfigure(label, state=state)
        self.menu.tk_popup(event.x_root, event.y_root)

    # ---------- 剪贴板操作 ----------
    def _select_all(self, event):
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def _copy_sel(self, cut=False):
        sel = list(self.tree.selection())
        if not sel:
            return "break"
        if set_clipboard_files(sel, cut=cut):
            _CUT_PATHS.clear()
            if cut:
                _CUT_PATHS.update(sel)
            self.app.apply_cut_tags()  # 剪切项置灰 / 旧的置灰清掉
            self.flash_status(f"已{'剪切' if cut else '复制'} {len(sel)} 个项目")
        return "break"

    def _paste(self):
        if getattr(self, "_paste_busy", False):  # 上一次粘贴还在跑，忽略
            return "break"
        clip = get_clipboard_files()
        if not clip:
            return "break"
        paths, is_cut = clip
        self._paste_busy = True
        self.flash_status(f"正在粘贴 {len(paths)} 个项目…")
        # 文件复制/移动放后台线程，大文件不卡 UI，完成后统一刷新
        threading.Thread(target=self._paste_worker,
                         args=(paths, is_cut, self.path), daemon=True).start()
        return "break"

    def _paste_worker(self, paths, is_cut, dst_dir):
        done, errors = 0, []
        for src in paths:
            if not os.path.lexists(src):
                continue  # 剪贴板里的源可能已不存在（如剪切后已粘贴过）
            dst = self._unique_path(os.path.join(dst_dir, os.path.basename(src)))
            try:
                if is_cut:
                    shutil.move(src, dst)
                elif os.path.isdir(src):
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
                done += 1
            except OSError as e:
                errors.append(f"{src}\n{e}")
        _paste_done.put((self, paths, is_cut, dst_dir, done, errors))

    def _paste_finish(self, paths, is_cut, dst_dir, done, errors):
        """主线程收尾：刷新受影响窗格（目标目录 + 各源目录），清剪切置灰"""
        self._paste_busy = False
        if is_cut:
            _CUT_PATHS.clear()
        if done:
            affected = {os.path.dirname(os.path.normpath(s)) for s in paths}
            affected.add(dst_dir)
            for p in self.app.panes:
                if p.path in affected or not os.path.isdir(p.path):
                    p.refresh()
        self.app.apply_cut_tags()
        if done:
            self.flash_status(f"已粘贴 {done} 个项目")
        if errors:
            messagebox.showerror("粘贴失败", "\n\n".join(errors), parent=self)

    def _unique_path(self, dst):
        """目标已存在时自动改名：name (2).ext、name (3).ext ..."""
        if not os.path.lexists(dst):
            return dst
        base, ext = os.path.splitext(dst)
        n = 2
        while os.path.lexists(f"{base} ({n}){ext}"):
            n += 1
        return f"{base} ({n}){ext}"

    def flash_status(self, msg):
        """状态栏短暂显示操作结果，随后恢复为常规的项目统计"""
        self._status_text = msg
        self.app.set_status(self)
        self.after(2500, self._update_status)

    # ---------- 菜单动作 ----------
    def _selected_path(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _menu_open(self):
        p = self._selected_path()
        if p:
            self.open_item(p)

    def _menu_reveal(self):
        p = self._selected_path()
        if p:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(p)])

    def _open_in_explorer(self):
        """在 Windows 资源管理器中打开当前窗格的目录"""
        subprocess.Popen(["explorer", os.path.normpath(self.path)])

    def _menu_copy_path(self):
        p = self._selected_path()
        if p:
            self.clipboard_clear()
            self.clipboard_append(p)

    def _menu_rename(self):
        p = self._selected_path()
        if p:
            self._begin_rename(p)

    def _begin_rename(self, iid):
        """行内重命名：在列表项文字区覆盖输入框（资源管理器风格），
        Enter/焦点离开提交，Esc 取消；文件只选中主名（不含扩展名）"""
        if getattr(self, "_rename_entry", None) is not None:
            return  # 一次只能改一个
        if not self.tree.exists(iid):
            return
        self.tree.selection_set(iid)
        self.tree.see(iid)
        self.tree.update_idletasks()
        box = self.tree.bbox(iid, "#0")
        if not box:
            return
        x, y, w, h = box
        icon_w = S(20)  # 行首图标区，输入框只覆盖文字
        old = os.path.basename(iid)
        entry = tk.Entry(self.tree, font=(FONT_FAMILY, FONT_SIZE),
                         relief="solid", bd=1, highlightthickness=1,
                         highlightcolor="#5b9bd5")
        entry.insert(0, old)
        if os.path.isdir(iid):
            entry.select_range(0, "end")
        else:
            entry.select_range(0, len(os.path.splitext(old)[0]))
        entry.place(x=x + icon_w, y=y, width=max(w - icon_w, S(120)), height=h)
        entry.focus_set()
        self._rename_entry = entry
        done = False

        def finish(commit):
            nonlocal done
            if done:
                return
            done = True
            new = entry.get().strip()
            entry.destroy()
            self._rename_entry = None
            self.tree.focus_set()
            if not commit or not new or new == old:
                return
            dst = os.path.join(os.path.dirname(iid), new)
            try:
                os.rename(iid, dst)
            except OSError as e:
                messagebox.showerror("重命名失败", str(e), parent=self)
                return
            self.refresh()
            if self.tree.exists(dst):
                self.tree.selection_set(dst)
                self.tree.see(dst)

        entry.bind("<Return>", lambda e: finish(True))
        entry.bind("<Escape>", lambda e: finish(False))
        entry.bind("<FocusOut>", lambda e: finish(True))

    def _menu_delete(self):
        sel = list(self.tree.selection())
        if not sel:
            return
        if len(sel) == 1:
            p = sel[0]
            kind = "文件夹及其全部内容" if os.path.isdir(p) else "文件"
            msg = f"将永久删除{kind}：\n{p}\n\n（不进入回收站）"
        else:
            msg = f"将永久删除选中的 {len(sel)} 个项目\n\n（不进入回收站）"
        if not messagebox.askyesno("确认删除", msg, icon="warning", parent=self):
            return
        def _force(func, p2, _exc):
            # 只读文件（如 git pack 的 .idx/.pack）直接删会 WinError 5，先清只读再重试
            try:
                os.chmod(p2, 0o666)
            except OSError:
                pass
            func(p2)
        failed = []
        for p in sel:
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p, onexc=_force)
                else:
                    try:
                        os.remove(p)
                    except PermissionError:
                        os.chmod(p, 0o666)
                        os.remove(p)
            except OSError as e:
                failed.append(f"{p}\n{e}")
        self.refresh()
        if failed:
            messagebox.showerror("删除失败", "\n\n".join(failed), parent=self)

    def _menu_new_folder(self):
        name = simpledialog.askstring("新建文件夹", "文件夹名称：", parent=self)
        if name:
            try:
                os.makedirs(os.path.join(self.path, name), exist_ok=False)
                self.refresh()
            except OSError as e:
                messagebox.showerror("创建失败", str(e), parent=self)

    def _menu_new_text_file(self):
        dst = self._unique_path(os.path.join(self.path, "新建文本文档.txt"))
        try:
            with open(dst, "wb"):
                pass
            self.refresh()
            self._begin_rename(dst)  # 与资源管理器一致：建完直接进入改名
        except OSError as e:
            messagebox.showerror("创建失败", str(e), parent=self)

    def _menu_properties(self):
        p = self._selected_path()
        if not p:
            return
        try:
            st = os.stat(p)
            kind = "文件夹" if os.path.isdir(p) else "文件"
            info = (f"名称：{os.path.basename(p)}\n类型：{kind}\n路径：{p}\n"
                    f"大小：{fmt_size(st.st_size)}（{st.st_size} 字节）\n"
                    f"修改时间：{fmt_time(st.st_mtime)}\n"
                    f"创建时间：{fmt_time(st.st_ctime)}")
            messagebox.showinfo("属性", info, parent=self)
        except OSError as e:
            messagebox.showerror("错误", str(e), parent=self)


class QDirLite(tk.Tk):
    COLS, ROWS = 2, 2  # 2x2 四窗格

    def __init__(self):
        super().__init__()
        self.title("QDir-Lite")
        self.geometry(f"{S(1150)}x{S(720)}")
        self.minsize(S(600), S(400))
        self.resizable(True, True)  # 窗口可拖拽调整大小
        self.maximized_pane = None
        self._saved_geometry = self.geometry()
        self.tk.call("tk", "scaling", _DPI / 72.0)  # 字体随 DPI 缩放

        self._icon = make_app_icon()
        self.iconphoto(True, self._icon)

        self._setup_style()

        # 全局唯一状态栏（先 pack 在底部，列表区填满剩余空间）
        # 右端放 1/2/3/4 布局切换按钮：复用已有的一行，零额外空间
        self.status_var = tk.StringVar()
        statusbar = tk.Frame(self, bg="#eef2f8")
        statusbar.pack(fill="x", side="bottom")
        # 双击状态栏路径 -> 在资源管理器中打开当前窗格目录
        self.status_label = tk.Label(statusbar, textvariable=self.status_var,
                                     anchor="w", bg="#eef2f8", fg="#5a6b82",
                                     font=(FONT_FAMILY, FONT_SIZE - 1),
                                     padx=4, pady=0, cursor="hand2")
        self.status_label.pack(side="left", fill="x", expand=True)
        self.status_label.bind(
            "<Double-1>",
            lambda e: getattr(self, "active_pane", None)
            and self.active_pane._open_in_explorer())
        self._layout_btns = {}
        self._layout_icons = {n: make_layout_icon(n) for n in (1, 2, 3, 4)}
        for n in (4, 3, 2, 1):  # side=right 从右往左排，1 在最右
            b = tk.Button(statusbar, image=self._layout_icons[n],
                          command=lambda n=n: self.set_layout(n),
                          relief="flat", bd=0, bg="#eef2f8",
                          activebackground="#d7e5f8", cursor="hand2",
                          padx=S(4), pady=0)
            b.pack(side="right", padx=S(5))
            self._layout_btns[n] = b

        # 用 PanedWindow 布局：窗格之间有可拖拽的分隔条
        # 注意：PanedWindow 容器必须先于 Pane 创建，否则 Pane 无法正常渲染（实测 tk 的坑）
        home = os.path.expanduser("~")
        state = _load_state() or {}
        saved_panes = state.get("panes") or []
        self.hpw = ttk.PanedWindow(self, orient="horizontal")
        self.hpw.pack(fill="both", expand=True, padx=0, pady=0)
        self.vpws = [ttk.PanedWindow(self.hpw, orient="vertical")
                     for _ in range(self.COLS)]
        self.panes = []
        for i in range(self.COLS * self.ROWS):
            sp = saved_panes[i] if i < len(saved_panes) and isinstance(
                saved_panes[i], dict) else {}
            path = sp.get("path", "")
            if not os.path.isdir(path):
                path = home  # 上次的目录已不存在，回退主目录
            pane = Pane(self, self, path)
            if sp.get("sort_col") in pane._heading_texts:
                pane.sort_col = sp["sort_col"]
                pane.sort_reverse = bool(sp.get("sort_reverse"))
                pane._fill()  # 按恢复的排序方式重排
            self.panes.append(pane)

        self.layout_n = state.get("layout", 4)
        if self.layout_n not in (1, 2, 3, 4):
            self.layout_n = 4
        self._build_layout(self.layout_n)
        self._update_layout_buttons()

        self.bind_all("<Button-1>", self._on_focus_pane, add="+")
        self.bind_all("<Control-Button-1>", self._on_ctrl_click, add="+")
        active = state.get("active_pane", 0)
        if not isinstance(active, int) or not 0 <= active < len(self.panes):
            active = 0
        self._on_focus_pane_type(self.panes[active])

        # 恢复上次窗口位置/尺寸（完全超出屏幕时忽略，用默认）
        geo = state.get("geometry", "")
        m = re.match(r"\d+x\d+([+-]\d+)([+-]\d+)", geo)
        if m:
            x, y = int(m.group(1)), int(m.group(2))
            if (-200 < x < self.winfo_vrootwidth() - 50
                    and -200 < y < self.winfo_vrootheight() - 50):
                self.geometry(geo)
        if state.get("zoomed"):
            self.state("zoomed")
        mp = state.get("maximized_pane")
        if isinstance(mp, int) and 0 <= mp < len(self.panes):
            self.toggle_maximize(self.panes[mp])

        # 实时跟踪正常状态窗口尺寸（窗口最大化关闭时用于恢复）
        self.bind("<Configure>", self._track_geometry)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # 后台线程：图标抓取 + 目录外部改动轮询 + 粘贴完成结果处理
        if fileicons is not None:
            threading.Thread(target=_icon_worker, daemon=True).start()
        self.after(80, self._poll_bg)
        self.after(2000, self._poll_dirs)

    def apply_cut_tags(self):
        """按 _CUT_PATHS 同步各窗格列表行的剪切置灰标签"""
        for p in self.panes:
            for iid in p.tree.get_children():
                tags = list(p.tree.item(iid, "tags"))
                has, want = "cut" in tags, iid in _CUT_PATHS
                if want and not has:
                    tags.append("cut")
                    p.tree.item(iid, tags=tags)
                elif has and not want:
                    tags.remove("cut")
                    p.tree.item(iid, tags=tags)

    def _poll_bg(self):
        try:
            while True:
                pane, gen = _icon_done.get_nowait()
                try:
                    pane._apply_icons(gen)
                except Exception:
                    pass
        except queue.Empty:
            pass
        try:
            while True:
                pane, paths, is_cut, dst_dir, done, errors = _paste_done.get_nowait()
                try:
                    pane._paste_finish(paths, is_cut, dst_dir, done, errors)
                except Exception:
                    pass
        except queue.Empty:
            pass
        try:
            self.after(80, self._poll_bg)
        except tk.TclError:
            pass  # 窗口已销毁，停止轮询

    def _poll_dirs(self):
        """每 2 秒对比各窗格目录的 mtime：外部增删改文件时自动刷新"""
        for p in self.panes:
            if getattr(p, "_paste_busy", False):
                continue  # 正在往这个目录粘贴，收尾时会统一刷新
            old = getattr(p, "_dir_mtime", None)
            if old is None:
                continue
            try:
                mt = os.stat(p.path).st_mtime
            except OSError:
                continue
            if mt != old:
                p.refresh()
        try:
            self.after(2000, self._poll_dirs)
        except tk.TclError:
            pass  # 窗口已销毁，停止轮询

    def _build_layout(self, n):
        """按 n=1/2/3/4 重排窗格布局：2=左右，3=左一右二，4=2x2。
        只用 forget/add 调整管理关系，容器和 Pane 对象都不销毁，
        各窗格路径与浏览状态完全保留。"""
        for child in self.hpw.panes():
            self.hpw.forget(child)
        for v in self.vpws:
            for child in v.panes():
                v.forget(child)
        p = self.panes
        if n == 1:
            self.hpw.add(p[0], weight=1)
        elif n == 2:
            self.hpw.add(p[0], weight=1)
            self.hpw.add(p[1], weight=1)
        elif n == 3:
            # 左边一格，右边上下两格（复用 vpws[1] 作右列）
            v = self.vpws[1]
            v.add(p[1], weight=1)
            v.add(p[2], weight=1)
            self.hpw.add(p[0], weight=1)
            self.hpw.add(v, weight=1)
        else:
            # 2x2：左列 p0/p2，右列 p1/p3
            self.vpws[0].add(p[0], weight=1)
            self.vpws[0].add(p[2], weight=1)
            self.vpws[1].add(p[1], weight=1)
            self.vpws[1].add(p[3], weight=1)
            self.hpw.add(self.vpws[0], weight=1)
            self.hpw.add(self.vpws[1], weight=1)

    def set_layout(self, n):
        """切换 1/2/3/4 窗格布局"""
        if self.maximized_pane is not None:
            # 先退出单窗格最大化状态
            self.state("normal")
            self.geometry(self._saved_geometry)
            self.maximized_pane = None
            self.title("QDir-Lite")
        if n == self.layout_n:
            return
        self.layout_n = n
        self._build_layout(n)
        self._update_layout_buttons()
        # 焦点窗格被隐藏时，焦点落到第一个可见窗格
        if getattr(self, "active_pane", None) not in self.panes[:n if n < 4 else 4]:
            self._on_focus_pane_type(self.panes[0])

    def _update_layout_buttons(self):
        for n, b in self._layout_btns.items():
            color = "#d7e5f8" if n == self.layout_n else "#eef2f8"
            b.configure(bg=color, activebackground="#d7e5f8")

    def _track_geometry(self, event):
        if (event.widget is self and self.state() == "normal"
                and self.maximized_pane is None):
            self._saved_geometry = self.geometry()

    def _on_close(self):
        """关闭时保存窗口与窗格状态，下次启动恢复"""
        try:
            max_idx = (self.panes.index(self.maximized_pane)
                       if self.maximized_pane is not None else None)
            zoomed = max_idx is None and self.state() == "zoomed"
            geometry = (self._saved_geometry
                        if (max_idx is not None or zoomed) else self.geometry())
            active = self.panes.index(getattr(self, "active_pane", self.panes[0]))
            state = {
                "geometry": geometry,
                "zoomed": zoomed,
                "maximized_pane": max_idx,
                "active_pane": active,
                "layout": self.layout_n,
                "panes": [{"path": p.path, "sort_col": p.sort_col,
                           "sort_reverse": p.sort_reverse} for p in self.panes],
            }
            f = _state_file()
            os.makedirs(os.path.dirname(f), exist_ok=True)
            with open(f, "w", encoding="utf-8") as fp:
                json.dump(state, fp, ensure_ascii=False, indent=1)
        except Exception:
            pass
        self.destroy()

    def _setup_style(self):
        style = ttk.Style(self)
        for theme in ("vista", "xpnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        style.configure("Treeview",
                        font=(FONT_FAMILY, FONT_SIZE), rowheight=S(18),
                        background="white", fieldbackground="white")
        style.configure("Treeview.Heading", font=(FONT_FAMILY, FONT_SIZE, "bold"),
                        padding=(2, 0))
        style.map("Treeview", background=[("selected", "#cde0f7")],
                  foreground=[("selected", "black")])

    def _on_focus_pane(self, event):
        w = event.widget
        while w is not None and not isinstance(w, Pane):
            w = w.master
        if w is not None:
            self._on_focus_pane_type(w)

    def _on_ctrl_click(self, event):
        """Ctrl+左键：聚焦所在窗格并弹出其地址栏"""
        w = event.widget
        while w is not None and not isinstance(w, Pane):
            w = w.master
        if w is not None:
            self._on_focus_pane_type(w)
            w.show_address()

    def _on_focus_pane_type(self, active_pane):
        self.active_pane = active_pane
        for p in self.panes:
            color = "#d7e5f8" if p is active_pane else "#eef2f8"
            p.header.configure(bg=color)
            p.up_btn.configure(bg=color, activebackground=color)
            p.re_btn.configure(bg=color, activebackground=color)
            # 所有窗格默认隐藏标题栏，给足文件浏览空间；
            # 焦点切换时收起非焦点窗格，焦点窗格保持当前显隐状态（Ctrl+左键唤出）
            if p is not active_pane:
                p.hide_address()
        self.set_status(active_pane)

    def set_status(self, pane):
        """统一状态栏：只显示当前焦点窗格的状态"""
        if getattr(self, "active_pane", None) is pane:
            self.status_var.set(f" {pane.path}　{pane._status_text}")

    def toggle_maximize(self, pane):
        """最大化当前窗格（窗口同步放到全屏）；再次双击还原回当前布局与窗口尺寸"""
        if self.maximized_pane is pane:
            # 还原
            self.state("normal")
            self.geometry(self._saved_geometry)
            self._build_layout(self.layout_n)
            self.maximized_pane = None
            self.title("QDir-Lite")
        else:
            if self.maximized_pane is None:
                self._saved_geometry = self.geometry()
            for child in self.hpw.panes():
                self.hpw.forget(child)
            self.hpw.add(pane, weight=1)
            self.maximized_pane = pane
            self.state("zoomed")  # 窗口真正最大化
            self.title(f"QDir-Lite - {pane.path} （双击标题栏还原）")


if __name__ == "__main__":
    app = QDirLite()
    app.mainloop()
