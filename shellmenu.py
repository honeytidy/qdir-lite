# -*- coding: utf-8 -*-
"""通过 Windows Shell COM 接口弹出与资源管理器一致的原生右键菜单（仅 ctypes，无第三方依赖）"""

import ctypes
import os
import shutil
import subprocess
import uuid
from ctypes import POINTER, byref, c_int, c_long, c_uint, c_void_p, cast, sizeof, wintypes

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
ole32 = ctypes.windll.ole32

HRESULT = c_long
CMF_EXPLORE = 0x00000004
CMF_CANRENAME = 0x00000010  # 加上它菜单里才有"重命名"项
TPM_RETURNCMD = 0x0100
SW_SHOWNORMAL = 1
# 转发给 IContextMenu2/3 的菜单消息（让扩展填充子菜单文字、自绘项）
WM_DRAWITEM = 0x002B
WM_MEASUREITEM = 0x002C
WM_INITMENUPOPUP = 0x0117
WM_MENUCHAR = 0x0120
GWLP_WNDPROC = -4
MF_BYPOSITION = 0x0400
MF_SEPARATOR = 0x0800
MF_POPUP = 0x0010
# 自定义菜单命令 id（须避开两个 QueryContextMenu 的 id 段 1..0x7FFF / 0x8000..0xDFFF）
CMD_OPEN_TERMINAL = 0xE000
# 目录背景菜单（只为拆出"新建"子菜单）占用的命令 id 段
ID_BG_FIRST, ID_BG_LAST = 0x8000, 0xDFFF

user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, c_int, c_void_p]
user32.CallWindowProcW.restype = ctypes.c_ssize_t
user32.CallWindowProcW.argtypes = [c_void_p, wintypes.HWND, wintypes.UINT,
                                   ctypes.c_size_t, ctypes.c_ssize_t]
user32.InsertMenuW.argtypes = [c_void_p, wintypes.UINT, wintypes.UINT,
                               ctypes.c_size_t, wintypes.LPCWSTR]
# HMENU 也是句柄：不声明 restype 时默认 int 会在 64 位下截断（同 fileicons 的坑）
user32.CreatePopupMenu.restype = c_void_p
user32.GetSubMenu.restype = c_void_p
user32.GetSubMenu.argtypes = [c_void_p, c_int]
user32.GetMenuStringW.argtypes = [c_void_p, c_uint, ctypes.c_wchar_p,
                                  c_int, c_uint]
user32.RemoveMenu.argtypes = [c_void_p, c_uint, c_uint]
user32.TrackPopupMenu.argtypes = [c_void_p, wintypes.UINT, c_int, c_int,
                                  c_int, wintypes.HWND, c_void_p]
user32.DestroyMenu.argtypes = [c_void_p]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             ctypes.c_size_t, ctypes.c_ssize_t)


class GUID(ctypes.Structure):
    _fields_ = [("_b", ctypes.c_ubyte * 16)]

    def __init__(self, s):
        super().__init__()
        ctypes.memmove(byref(self), uuid.UUID(s).bytes_le, 16)


IID_IShellFolder = GUID("{000214E6-0000-0000-C000-000000000046}")
IID_IContextMenu = GUID("{000214E4-0000-0000-C000-000000000046}")
IID_IContextMenu2 = GUID("{000214F4-0000-0000-C000-000000000046}")
IID_IContextMenu3 = GUID("{BCFCE0A0-EC21-11D0-8FAB-00A0C90E50E1}")


class CMINVOKECOMMANDINFOEX(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("fMask", wintypes.DWORD),
        ("hwnd", wintypes.HWND), ("lpVerb", c_void_p), ("lpParameters", c_void_p),
        ("lpDirectory", c_void_p), ("nShow", c_int),
        ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE),
        ("lpTitle", c_void_p), ("lpVerbW", c_void_p), ("lpParametersW", c_void_p),
        ("lpDirectoryW", c_void_p), ("lpTitleW", c_void_p),
        ("ptInvoke", wintypes.POINT),
    ]


shell32.SHParseDisplayName.restype = HRESULT
shell32.SHParseDisplayName.argtypes = [wintypes.LPCWSTR, c_void_p,
                                       POINTER(c_void_p), wintypes.DWORD, c_void_p]
shell32.SHBindToParent.restype = HRESULT
shell32.SHBindToParent.argtypes = [c_void_p, POINTER(GUID),
                                   POINTER(c_void_p), POINTER(c_void_p)]
shell32.ILFindLastID.restype = c_void_p
shell32.ILFindLastID.argtypes = [c_void_p]
shell32.SHBindToObject.restype = HRESULT
shell32.SHBindToObject.argtypes = [c_void_p, c_void_p, c_void_p,
                                   POINTER(GUID), POINTER(c_void_p)]
ole32.CoTaskMemFree.argtypes = [c_void_p]


def _com_method(obj, index, restype, *argtypes):
    """按 vtable 索引取 COM 方法。obj 为接口指针，其第一个字段指向 vtable"""
    proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    vtbl = cast(cast(obj, POINTER(c_void_p))[0], POINTER(c_void_p))
    return proto(vtbl[index])


def _terminal_command(paths):
    """返回（菜单文字, 启动函数）：在目标目录打开终端，优先 wt，其次 PowerShell、cmd"""
    d = paths[0] if os.path.isdir(paths[0]) else os.path.dirname(paths[0])
    if shutil.which("wt"):
        return "在终端中打开(&T)", lambda: subprocess.Popen(["wt", "-d", d])
    if shutil.which("powershell"):
        return ("在此处打开 PowerShell(&P)",
                lambda: subprocess.Popen(["powershell.exe"], cwd=d))
    return ("在此处打开命令提示符(&C)",
            lambda: subprocess.Popen(["cmd.exe"], cwd=d))


def _verb_of(pcm, offset):
    """读命令的规范 verb 名（用于识别 rename 这类需要视图支持的 verb）。

    实测该聚合对象的 GCS_VERBW 错误地返回帮助文本，GCS_VERBA 则按 UTF-16
    写入规范名（rename/delete/properties），故按 UTF-16 解码。
    """
    buf = ctypes.create_string_buffer(128)
    gcs = _com_method(pcm, 5, HRESULT, ctypes.c_size_t, c_uint,
                      c_void_p, c_void_p, c_uint)
    if gcs(pcm, offset, 4, None, buf, 128) == 0:  # GCS_VERBA
        return buf.raw.decode("utf-16-le", "ignore").split("\x00")[0].lower()
    return ""


def show_shell_menu(hwnd, paths, x, y, background=False):
    """在 (x, y) 屏幕坐标处，对 paths（同目录下的文件/文件夹）弹出系统右键菜单。

    background=True 时 paths[0] 是当前目录本身：主菜单 = 该目录作为子项的项目菜单
    （扩展子菜单消息转发正常），另从目录背景菜单拆出"新建"子菜单插入——直接弹
    CreateViewObject 的背景菜单会导致 PS7 等扩展子菜单空白（聚合对象不路由消息）。
    返回 True=命令已执行 / "rename"=用户选了重命名（调用方自行进入改名）/
    False=未选择。路径无效或 COM 调用失败时抛异常，由调用方回退。
    """
    ole32.CoInitialize(None)
    pidls, child_ptrs = [], []
    psf = c_void_p()
    psf_bg = c_void_p()   # 目录本身的 IShellFolder（拆"新建"用）
    pcm_bg = c_void_p()   # 目录背景菜单的 IContextMenu
    hmenu_bg = None
    try:
        item_paths = paths[:1] if background else paths
        for i, p in enumerate(item_paths):
            pidl = c_void_p()
            hr = shell32.SHParseDisplayName(str(p), None, byref(pidl), 0, None)
            if hr != 0 or not pidl:
                continue
            pidls.append(pidl)
            if i == 0:
                hr = shell32.SHBindToParent(pidl, byref(IID_IShellFolder),
                                            byref(psf), None)
                if hr != 0 or not psf:
                    raise OSError(f"SHBindToParent failed: {hr:#x}")
            child_ptrs.append(shell32.ILFindLastID(pidl))
        if not pidls:
            raise OSError("no valid path")

        pcm = c_void_p()
        arr = (c_void_p * len(child_ptrs))(*child_ptrs)
        get_ui = _com_method(psf, 10, HRESULT, wintypes.HWND, c_uint,
                             POINTER(c_void_p), POINTER(GUID), c_void_p,
                             POINTER(c_void_p))
        hr = get_ui(psf, hwnd, len(child_ptrs), arr,
                    byref(IID_IContextMenu), None, byref(pcm))
        if hr != 0 or not pcm:
            raise OSError(f"GetUIObjectOf(IContextMenu) failed: {hr:#x}")

        if background:
            # CreateViewObject(IID_IContextMenu) 拿目录背景菜单（含"新建"，
            # 由注册表 ShellNew 处理器提供）；GetUIObjectOf(cidl=0) 会 E_INVALIDARG
            hr = shell32.SHBindToObject(None, pidls[0], None,
                                        byref(IID_IShellFolder), byref(psf_bg))
            if hr == 0 and psf_bg:
                create_view = _com_method(psf_bg, 8, HRESULT, wintypes.HWND,
                                          POINTER(GUID), POINTER(c_void_p))
                create_view(psf_bg, hwnd, byref(IID_IContextMenu), byref(pcm_bg))
        try:
            hmenu = user32.CreatePopupMenu()
            query = _com_method(pcm, 3, HRESULT, c_void_p, c_uint, c_uint, c_uint, c_uint)
            hr = query(pcm, hmenu, 0, 1, 0x7FFF, CMF_EXPLORE | CMF_CANRENAME)
            if hr < 0:
                raise OSError(f"QueryContextMenu failed: {hr:#x}")

            # 从背景菜单拆出"新建"子菜单（RemoveMenu 只拆不销毁，HMENU 仍归 pcm_bg 管）
            new_sub = None
            if pcm_bg:
                hmenu_bg = user32.CreatePopupMenu()
                query_bg = _com_method(pcm_bg, 3, HRESULT, c_void_p, c_uint,
                                       c_uint, c_uint, c_uint)
                hr = query_bg(pcm_bg, hmenu_bg, 0, ID_BG_FIRST, ID_BG_LAST,
                              CMF_EXPLORE)
                if hr >= 0:
                    for i in range(user32.GetMenuItemCount(hmenu_bg)):
                        buf = ctypes.create_unicode_buffer(128)
                        user32.GetMenuStringW(hmenu_bg, i, buf, 128, MF_BYPOSITION)
                        sub = user32.GetSubMenu(hmenu_bg, i)
                        if sub and "新建" in buf.value:
                            user32.RemoveMenu(hmenu_bg, i, MF_BYPOSITION)
                            new_sub = (sub, buf.value)
                            break

            # 顶部加"打开命令行"项（经典菜单拿不到 Win11 的"在终端中打开"）
            term_label, term_cmd = _terminal_command(paths)
            user32.InsertMenuW(hmenu, 0, MF_BYPOSITION, CMD_OPEN_TERMINAL, term_label)
            user32.InsertMenuW(hmenu, 1, MF_BYPOSITION | MF_SEPARATOR, 0, None)
            if new_sub:
                user32.InsertMenuW(hmenu, 2, MF_BYPOSITION | MF_POPUP,
                                   new_sub[0], new_sub[1])
                user32.InsertMenuW(hmenu, 3, MF_BYPOSITION | MF_SEPARATOR, 0, None)

            # IContextMenu2/3：转发菜单消息，扩展才能填充子菜单文字（如 PowerShell 7）
            forwards = []  # (icm_extra, HandleMenuMsg2, HandleMenuMsg)
            for c in (pcm, pcm_bg):
                if not c:
                    continue
                extra = c_void_p()
                qi = _com_method(c, 0, HRESULT, POINTER(GUID), POINTER(c_void_p))
                if qi(c, byref(IID_IContextMenu3), byref(extra)) == 0 and extra:
                    forwards.append((extra, _com_method(
                        extra, 7, HRESULT, c_uint, ctypes.c_size_t,
                        ctypes.c_ssize_t, POINTER(c_long)), None))
                elif qi(c, byref(IID_IContextMenu2), byref(extra)) == 0 and extra:
                    forwards.append((extra, None, _com_method(
                        extra, 6, HRESULT, c_uint, ctypes.c_size_t,
                        ctypes.c_ssize_t)))

            # 菜单消息会发到 TrackPopupMenu 的属主窗口：临时子类化以转发
            orig_proc = None
            if forwards:
                def _forward(h, msg, wp, lp):
                    if msg in (WM_INITMENUPOPUP, WM_DRAWITEM,
                               WM_MEASUREITEM, WM_MENUCHAR):
                        # 无法预知子菜单归哪个 pcm 管，全部转发（实测互不干扰）
                        ret, handled = 0, False
                        for extra, hmm2, hmm in forwards:
                            if hmm2:
                                res = c_long(0)
                                if hmm2(extra, msg, wp, lp, byref(res)) == 0:
                                    ret, handled = res.value, True
                            elif hmm(extra, msg, wp, lp) == 0:
                                handled = True
                        if handled:
                            return ret
                    return user32.CallWindowProcW(orig_proc, h, msg, wp, lp)
                wnd_cb = WNDPROC(_forward)  # 保持引用，防止被 GC
                orig_proc = user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, wnd_cb)
            try:
                user32.SetForegroundWindow(hwnd)
                cmd = user32.TrackPopupMenu(hmenu, TPM_RETURNCMD, x, y, 0, hwnd, None)
            finally:
                if orig_proc:
                    user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, orig_proc)
            for extra, _, _ in forwards:
                _com_method(extra, 2, c_uint)(extra)  # Release

            if cmd == CMD_OPEN_TERMINAL:
                term_cmd()
                return True
            if cmd:
                info = CMINVOKECOMMANDINFOEX()
                info.cbSize = sizeof(info)
                info.hwnd = hwnd
                info.nShow = SW_SHOWNORMAL
                if ID_BG_FIRST <= cmd <= ID_BG_LAST and pcm_bg:
                    # "新建"子菜单的命令：走背景菜单的 pcm，verb 需指定目标目录
                    info.lpVerb = cmd - ID_BG_FIRST
                    dirbuf = ctypes.create_unicode_buffer(
                        os.path.normpath(str(paths[0])))
                    info.fMask = 0x00004000  # CMIC_MASK_UNICODE
                    info.lpDirectoryW = cast(dirbuf, c_void_p)
                    target = pcm_bg
                else:
                    # "重命名"verb 需要 IShellView 进入编辑态，invoke 是空操作：
                    # 拦截下来交回调用方做行内重命名
                    if _verb_of(pcm, cmd - 1) == "rename":
                        return "rename"
                    info.lpVerb = cmd - 1  # MAKEINTRESOURCE(idCmdFirst 偏移)
                    target = pcm
                invoke = _com_method(target, 4, HRESULT,
                                     POINTER(CMINVOKECOMMANDINFOEX))
                hr = invoke(target, byref(info))
                if hr != 0:
                    raise OSError(f"InvokeCommand failed: {hr:#x}")
                return True
            return False
        finally:
            user32.DestroyMenu(hmenu)
            if hmenu_bg:
                user32.DestroyMenu(hmenu_bg)
            _com_method(pcm, 2, c_uint)(pcm)  # Release
            if pcm_bg:
                _com_method(pcm_bg, 2, c_uint)(pcm_bg)
    finally:
        if psf:
            _com_method(psf, 2, c_uint)(psf)  # Release
        if psf_bg:
            _com_method(psf_bg, 2, c_uint)(psf_bg)
        for pidl in pidls:
            ole32.CoTaskMemFree(pidl)
