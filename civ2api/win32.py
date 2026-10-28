"""
Thin ctypes wrappers over the Win32 APIs we need. Standard library only.

Read-only by design for process memory: the process handle is opened with
PROCESS_VM_READ | PROCESS_QUERY_LIMITED_INFORMATION and never with write rights.
Works from 64-bit Python against the 32-bit CIV2.EXE (WOW64) because
ReadProcessMemory accepts the 32-bit addresses unchanged.
"""
from __future__ import annotations

import ctypes
import os
import struct
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass

if sys.platform != "win32":  # allow import (for tests / py_compile) on other OSes
    raise ImportError("civ2api.win32 only works on Windows")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

# --------------------------------------------------------------------------- constants
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

WM_SETTEXT = 0x000C
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
BM_CLICK = 0x00F5
GW_OWNER = 4
MAPVK_VK_TO_VSC = 0

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002

# --------------------------------------------------------------------------- structs
class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUTUNION(ctypes.Union):
    # MOUSEINPUT is the largest member; pad so sizeof(INPUT) is correct on x86/x64
    _fields_ = [("ki", KEYBDINPUT),
                ("_pad", ctypes.c_byte * (32 if ctypes.sizeof(ctypes.c_void_p) == 8 else 24))]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


# --------------------------------------------------------------------------- prototypes
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wintypes.BOOL
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.ReadProcessMemory.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t)]
kernel32.ReadProcessMemory.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.IsWow64Process.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
kernel32.IsWow64Process.restype = wintypes.BOOL

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumChildWindows.argtypes = [wintypes.HWND, WNDENUMPROC, wintypes.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowEnabled.argtypes = [wintypes.HWND]
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetDlgCtrlID.argtypes = [wintypes.HWND]
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
user32.GetGUIThreadInfo.restype = wintypes.BOOL
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetForegroundWindow.restype = wintypes.HWND


def _err(msg: str) -> OSError:
    code = ctypes.get_last_error()
    return OSError(code, f"{msg} (WinError {code}: {ctypes.FormatError(code).strip()})")


# --------------------------------------------------------------------------- processes
def find_pids(exe_name: str = "civ2.exe") -> list[int]:
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE or not snap:
        raise _err("CreateToolhelp32Snapshot")
    pids = []
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(pe)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            if pe.szExeFile.lower() == exe_name.lower():
                pids.append(pe.th32ProcessID)
            ok = kernel32.Process32NextW(snap, ctypes.byref(pe))
    finally:
        kernel32.CloseHandle(snap)
    return pids


class ProcessReader:
    """Read-only view of another process's memory."""

    def __init__(self, pid: int):
        self.pid = pid
        self.handle = kernel32.OpenProcess(
            PROCESS_VM_READ | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not self.handle:
            raise _err(f"OpenProcess({pid})")

    # -- lifecycle
    def close(self) -> None:
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- info
    def image_path(self) -> str:
        buf = ctypes.create_unicode_buffer(1024)
        n = wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(self.handle, 0, buf, ctypes.byref(n)):
            raise _err("QueryFullProcessImageNameW")
        return buf.value

    def image_size(self) -> int:
        return os.path.getsize(self.image_path())

    def is_32bit(self) -> bool:
        wow = wintypes.BOOL()
        kernel32.IsWow64Process(self.handle, ctypes.byref(wow))
        return bool(wow)

    # -- reads
    def read(self, addr: int, size: int) -> bytes:
        buf = (ctypes.c_char * size)()
        got = ctypes.c_size_t()
        if not kernel32.ReadProcessMemory(self.handle, ctypes.c_void_p(addr), buf, size,
                                          ctypes.byref(got)) or got.value != size:
            raise _err(f"ReadProcessMemory(0x{addr:08X}, {size})")
        return buf.raw

    def u8(self, addr: int) -> int:
        return self.read(addr, 1)[0]

    def i8(self, addr: int) -> int:
        return struct.unpack("<b", self.read(addr, 1))[0]

    def u16(self, addr: int) -> int:
        return struct.unpack("<H", self.read(addr, 2))[0]

    def i16(self, addr: int) -> int:
        return struct.unpack("<h", self.read(addr, 2))[0]

    def u32(self, addr: int) -> int:
        return struct.unpack("<I", self.read(addr, 4))[0]

    def i32(self, addr: int) -> int:
        return struct.unpack("<i", self.read(addr, 4))[0]

    def cstr(self, addr: int, maxlen: int = 64, encoding: str = "cp1252") -> str:
        raw = self.read(addr, maxlen)
        return raw.split(b"\0", 1)[0].decode(encoding, "replace")


# --------------------------------------------------------------------------- windows
@dataclass
class WinInfo:
    hwnd: int
    cls: str
    title: str
    visible: bool
    enabled: bool
    rect: tuple[int, int, int, int]
    owner: int
    ctrl_id: int = 0

    def as_dict(self) -> dict:
        return {"hwnd": self.hwnd, "class": self.cls, "text": self.title,
                "visible": self.visible, "enabled": self.enabled,
                "rect": list(self.rect), "owner": self.owner, "ctrl_id": self.ctrl_id}


def _class(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_text(hwnd) -> str:
    """WM_GETTEXT with timeout: works for controls in another process too."""
    n = ctypes.c_size_t()
    if not user32.SendMessageTimeoutW(hwnd, WM_GETTEXTLENGTH, 0, 0, 0x2, 500, ctypes.byref(n)):
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        return buf.value
    size = int(n.value) + 1
    buf = ctypes.create_unicode_buffer(size)
    user32.SendMessageTimeoutW(hwnd, WM_GETTEXT, size, ctypes.addressof(buf), 0x2, 500,
                               ctypes.byref(n))
    return buf.value


def _info(hwnd) -> WinInfo:
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return WinInfo(
        hwnd=int(hwnd or 0), cls=_class(hwnd), title=window_text(hwnd),
        visible=bool(user32.IsWindowVisible(hwnd)), enabled=bool(user32.IsWindowEnabled(hwnd)),
        rect=(r.left, r.top, r.right, r.bottom),
        owner=int(user32.GetWindow(hwnd, GW_OWNER) or 0),
        ctrl_id=int(user32.GetDlgCtrlID(hwnd)))


def top_windows(pid: int, visible_only: bool = True) -> list[WinInfo]:
    out: list[WinInfo] = []

    @WNDENUMPROC
    def cb(hwnd, _):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and (not visible_only or user32.IsWindowVisible(hwnd)):
            out.append(_info(hwnd))
        return True

    user32.EnumWindows(cb, 0)
    return out


def child_windows(hwnd: int) -> list[WinInfo]:
    out: list[WinInfo] = []

    @WNDENUMPROC
    def cb(h, _):
        out.append(_info(h))
        return True

    user32.EnumChildWindows(hwnd, cb, 0)
    return out


def window_thread(hwnd: int) -> int:
    return user32.GetWindowThreadProcessId(hwnd, None)


def gui_focus(hwnd_in_thread: int) -> tuple[int, int]:
    """(active, focus) windows of the GUI thread that owns hwnd_in_thread."""
    gti = GUITHREADINFO()
    gti.cbSize = ctypes.sizeof(gti)
    if not user32.GetGUIThreadInfo(window_thread(hwnd_in_thread), ctypes.byref(gti)):
        return 0, 0
    return int(gti.hwndActive or 0), int(gti.hwndFocus or 0)


def click_button(hwnd: int) -> None:
    user32.PostMessageW(hwnd, BM_CLICK, 0, 0)


GCL_CBWNDEXTRA = -18
user32.GetClassLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetClassLongW.restype = wintypes.DWORD
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long


def control_info_ptr(hwnd: int) -> int:
    """Civ2 stores its TControlInfo* at window extra offset (cbWndExtra - 8)
    (Civ2UIA UiaPatchCommon.pas:177). Reading another process's window long is allowed."""
    extra = user32.GetClassLongW(hwnd, GCL_CBWNDEXTRA)
    ptr = user32.GetWindowLongW(hwnd, extra - 8) & 0xFFFFFFFF if extra >= 8 else 0
    if not ptr:
        # Civ2UIA Patch64Bit keeps the pointer at offset 0 on 64-bit Windows
        ptr = user32.GetWindowLongW(hwnd, 0) & 0xFFFFFFFF
    return ptr


WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON = 0x0200, 0x0201, 0x0202, 0x0001
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]


def click_window(hwnd: int, x: int | None = None, y: int | None = None) -> None:
    """Post a left click at client (x, y) of hwnd (default: centre). No focus or cursor move."""
    r = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    if x is None:
        x = (r.right - r.left) // 2
    if y is None:
        y = (r.bottom - r.top) // 2
    lp = (y << 16) | (x & 0xFFFF)
    user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lp)
    user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp)
    time.sleep(0.05)
    user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lp)


# --------------------------------------------------------------------------- keyboard
# Virtual-key codes for keys Civ2 uses. Extended keys need bit 24 in lParam.
VK = {
    "BACK": 0x08, "TAB": 0x09, "ENTER": 0x0D, "SHIFT": 0x10, "CTRL": 0x11, "ALT": 0x12,
    "ESC": 0x1B, "SPACE": 0x20, "PGUP": 0x21, "PGDN": 0x22, "END": 0x23, "HOME": 0x24,
    "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28, "INSERT": 0x2D, "DELETE": 0x2E,
    **{f"NUM{i}": 0x60 + i for i in range(10)},
    **{f"F{i}": 0x6F + i for i in range(1, 13)},
    **{chr(c): c for c in range(ord("A"), ord("Z") + 1)},
    **{str(d): 0x30 + d for d in range(10)},
}
_EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}
_MOD_FLAG = {"SHIFT": "SHIFT", "CTRL": "CTRL", "ALT": "ALT"}


def parse_key(spec: str) -> tuple[list[int], int]:
    """'shift+c' -> ([VK_SHIFT], VK_C). Case-insensitive."""
    parts = [p.strip().upper() for p in spec.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key")
    *mods, key = parts
    for m in mods:
        if m not in _MOD_FLAG:
            raise ValueError(f"unknown modifier {m!r}")
    if key not in VK:
        raise ValueError(f"unknown key {key!r}; known: {sorted(VK)}")
    return [VK[m] for m in mods], VK[key]


def _lparam(vk: int, up: bool, alt: bool = False) -> int:
    scan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
    lp = 1 | (scan << 16)
    if vk in _EXTENDED:
        lp |= 1 << 24
    if alt:
        lp |= 1 << 29
    if up:
        lp |= (1 << 30) | (1 << 31)
    return lp


def post_key(hwnd: int, spec: str, hold: float = 0.02) -> None:
    """Post WM_KEYDOWN/(WM_CHAR)/WM_KEYUP to hwnd. Does not need focus.

    Modifiers are posted as their own key events; note that GetKeyState() in the
    target will not see them, so for shift/ctrl combos prefer send_input().
    """
    mods, vk = parse_key(spec)
    if mods:
        # the target would see only the bare key (e.g. "S" = Sleep): refuse
        raise ValueError(f"{spec}: cannot post modifier keys; use the menu accelerator or sendinput")
    for m in mods:
        user32.PostMessageW(hwnd, WM_KEYDOWN, m, _lparam(m, False))
    user32.PostMessageW(hwnd, WM_KEYDOWN, vk, _lparam(vk, False))
    ch = _char_for(vk, shift=VK["SHIFT"] in mods)
    if ch and VK["CTRL"] not in mods:
        user32.PostMessageW(hwnd, WM_CHAR, ord(ch), _lparam(vk, False))
    time.sleep(hold)
    user32.PostMessageW(hwnd, WM_KEYUP, vk, _lparam(vk, True))
    for m in reversed(mods):
        user32.PostMessageW(hwnd, WM_KEYUP, m, _lparam(m, True))


def _char_for(vk: int, shift: bool) -> str | None:
    if 0x41 <= vk <= 0x5A:
        c = chr(vk)
        return c if shift else c.lower()
    if 0x30 <= vk <= 0x39:
        return chr(vk)
    if vk in (0x0D, 0x20, 0x1B, 0x08):
        return {0x0D: "\r", 0x20: " ", 0x1B: "\x1b", 0x08: "\b"}[vk]
    return None


def send_input(hwnd: int, spec: str, hold: float = 0.03) -> None:
    """Real keyboard input via SendInput. Brings hwnd to the foreground first."""
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.05)
    mods, vk = parse_key(spec)
    seq = [(m, False) for m in mods] + [(vk, False)]
    up = [(vk, True)] + [(m, True) for m in reversed(mods)]

    def ev(code, is_up):
        i = INPUT(type=INPUT_KEYBOARD)
        flags = KEYEVENTF_KEYUP if is_up else 0
        if code in _EXTENDED:
            flags |= KEYEVENTF_EXTENDEDKEY
        i.u.ki = KEYBDINPUT(wVk=code, wScan=user32.MapVirtualKeyW(code, MAPVK_VK_TO_VSC),
                            dwFlags=flags, time=0, dwExtraInfo=0)
        return i

    for code, is_up in seq:
        user32.SendInput(1, ctypes.byref(ev(code, is_up)), ctypes.sizeof(INPUT))
    time.sleep(hold)
    for code, is_up in up:
        user32.SendInput(1, ctypes.byref(ev(code, is_up)), ctypes.sizeof(INPUT))


# --------------------------------------------------------------------------- menus & dialogs
WM_COMMAND = 0x0111
MF_BYPOSITION = 0x0400
IDOK, IDYES = 1, 6
user32.GetMenu.argtypes = [wintypes.HWND]
user32.GetMenu.restype = wintypes.HMENU
user32.GetMenuItemCount.argtypes = [wintypes.HMENU]
user32.GetMenuItemCount.restype = ctypes.c_int
user32.GetSubMenu.argtypes = [wintypes.HMENU, ctypes.c_int]
user32.GetSubMenu.restype = wintypes.HMENU
user32.GetMenuItemID.argtypes = [wintypes.HMENU, ctypes.c_int]
user32.GetMenuItemID.restype = wintypes.UINT
user32.GetMenuStringW.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.LPWSTR, ctypes.c_int, wintypes.UINT]
user32.GetMenuStringW.restype = ctypes.c_int


def menu_tree(hwnd: int, hmenu=None, depth: int = 0) -> list[dict]:
    """Read another process's menu bar (menus are system objects, readable cross-process)."""
    hmenu = hmenu or user32.GetMenu(hwnd)
    out = []
    if not hmenu or depth > 3:
        return out
    for i in range(max(user32.GetMenuItemCount(hmenu), 0)):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetMenuStringW(hmenu, i, buf, 256, MF_BYPOSITION)
        sub = user32.GetSubMenu(hmenu, i)
        item = {"text": buf.value.replace("&", ""), "id": None if sub else user32.GetMenuItemID(hmenu, i)}
        if sub:
            item["children"] = menu_tree(hwnd, sub, depth + 1)
        out.append(item)
    return out


def find_menu_id(hwnd: int, label: str) -> int | None:
    """First menu item whose text (before the TAB/accelerator) matches label, case-insensitive."""
    want = label.lower()

    def walk(items):
        for it in items:
            name = it["text"].split("\t")[0].strip().lower()
            if it.get("id") not in (None, 0xFFFFFFFF) and name == want:
                return it["id"]
            if it.get("children"):
                r = walk(it["children"])
                if r is not None:
                    return r
        return None

    return walk(menu_tree(hwnd))


def post_command(hwnd: int, cmd_id: int) -> None:
    user32.PostMessageW(hwnd, WM_COMMAND, cmd_id, 0)


def set_text(hwnd: int, text: str) -> bool:
    buf = ctypes.create_unicode_buffer(text)
    res = ctypes.c_size_t()
    return bool(user32.SendMessageTimeoutW(hwnd, WM_SETTEXT, 0, ctypes.addressof(buf), 0x2, 2000,
                                           ctypes.byref(res)))


def file_dialogs(pid: int) -> list[WinInfo]:
    return [w for w in top_windows(pid) if w.cls == "#32770"]


def fill_file_dialog(dlg: int, path: str) -> dict:
    """Type a path into a Windows Open/Save dialog and press its OK button."""
    kids = child_windows(dlg)
    edits = [k for k in kids if k.cls == "Edit" and k.visible]
    # the file-name box: control id 1148 (cmb13 edit) or 1152 (edt1); else the first visible Edit
    edit = next((k for k in edits if k.ctrl_id in (1148, 1152)), edits[0] if edits else None)
    ok = next((k for k in kids if k.cls == "Button" and k.ctrl_id == IDOK), None)
    if edit is None or ok is None:
        return {"ok": False, "error": "file dialog controls not found",
                "controls": [(k.cls, k.ctrl_id, k.title[:30]) for k in kids][:40]}
    set_text(edit.hwnd, path)
    time.sleep(0.2)
    user32.PostMessageW(ok.hwnd, BM_CLICK, 0, 0)
    return {"ok": True, "edit": edit.hwnd, "button": ok.hwnd}


def click_yes(dlg: int) -> bool:
    yes = next((k for k in child_windows(dlg) if k.cls == "Button" and k.ctrl_id == IDYES), None)
    if yes:
        user32.PostMessageW(yes.hwnd, BM_CLICK, 0, 0)
    return yes is not None


def _norm_accel(text: str) -> frozenset | None:
    parts = [p.strip().upper() for p in text.replace("-", "+").split("+") if p.strip()]
    if not parts:
        return None
    alias = {"CONTROL": "CTRL"}
    return frozenset(alias.get(p, p) for p in parts)


def find_menu_by_accel(hwnd: int, spec: str) -> dict | None:
    """Menu item whose accelerator (text after TAB, e.g. 'Ctrl+S', 'Shift+D') equals spec."""
    want = _norm_accel(spec)

    def walk(items):
        for it in items:
            if "\t" in it["text"] and it.get("id") not in (None, 0xFFFFFFFF):
                if _norm_accel(it["text"].split("\t", 1)[1]) == want:
                    return it
            if it.get("children"):
                r = walk(it["children"])
                if r:
                    return r
        return None

    return walk(menu_tree(hwnd))


# --------------------------------------------------------------------------- real mouse
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND


def real_click(hwnd: int, x: int | None = None, y: int | None = None, restore: bool = True) -> None:
    """A real left click (moves the mouse) at client (x, y) of hwnd. Needed for Civ2's own
    scrollbars, which ignore posted mouse messages (seen live on the Tax Rate dialog)."""
    r = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    if x is None:
        x = (r.right - r.left) // 2
    if y is None:
        y = (r.bottom - r.top) // 2
    pt = wintypes.POINT(x, y)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    old = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(old))
    top = user32.GetAncestor(hwnd, 2) or hwnd          # GA_ROOT
    user32.SetForegroundWindow(top)
    time.sleep(0.05)
    user32.SetCursorPos(pt.x, pt.y)
    time.sleep(0.03)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.06)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.03)
    if restore:
        user32.SetCursorPos(old.x, old.y)


# --------------------------------------------------------------------------- Stage 2: writes
PROCESS_VM_WRITE, PROCESS_VM_OPERATION = 0x0020, 0x0008
kernel32.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
kernel32.WriteProcessMemory.restype = wintypes.BOOL


class ProcessWriter:
    """Write access to the game's DATA (never code; CIV2.EXE on disk is untouched).
    Only used by civ2api.memwrite, which allows a short whitelist of fields."""

    def __init__(self, pid: int):
        self.pid = pid
        self.handle = kernel32.OpenProcess(
            PROCESS_VM_WRITE | PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_QUERY_LIMITED_INFORMATION,
            False, pid)
        if not self.handle:
            raise _err(f"OpenProcess(write, {pid})")

    def write(self, addr: int, data: bytes) -> None:
        buf = ctypes.create_string_buffer(data, len(data))
        n = ctypes.c_size_t()
        if not kernel32.WriteProcessMemory(self.handle, ctypes.c_void_p(addr), buf, len(data), ctypes.byref(n)) \
                or n.value != len(data):
            raise _err(f"WriteProcessMemory(0x{addr:08X}, {len(data)})")

    def close(self):
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None
