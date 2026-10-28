"""
Save / load the running game through Civ2's own menu (WM_COMMAND) and the Windows
file dialog (Win32 controls). No keyboard focus needed.
"""
from __future__ import annotations

import os
import time

from . import win32


def _menu_id(session, label: str, default: int) -> int:
    try:
        return win32.find_menu_id(session.main_hwnd(), label) or default
    except Exception:
        return default


def _wait(cond, timeout: float, step: float = 0.15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = cond()
        if r:
            return r
        time.sleep(step)
    return None


def _dialog(session):
    d = win32.file_dialogs(session.game.p.pid)
    return d[0] if d else None


def save_game(session, path: str, timeout: float = 20) -> dict:
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    session.refresh()
    if session.state().get("popup"):
        return {"ok": False, "error": "a popup is open; answer it first"}
    if os.path.exists(path):
        return {"ok": False, "error": f"{path} already exists (refusing to overwrite)"}
    win32.post_command(session.main_hwnd(), _menu_id(session, "Save Game", 684))
    dlg = _wait(lambda: _dialog(session), 8)
    if not dlg:
        session.refresh()
        return {"ok": False, "error": "save dialog did not open", "popup": session.state().get("popup")}
    r = win32.fill_file_dialog(dlg.hwnd, path)
    if not r["ok"]:
        return r
    # the dialog closes; if a "replace?" confirmation shows up instead, say yes
    t0 = time.time()
    while time.time() - t0 < 4:
        d = _dialog(session)
        if d is None:
            break
        if d.hwnd != dlg.hwnd:
            win32.click_yes(d.hwnd)
        time.sleep(0.2)
    ok = _wait(lambda: os.path.exists(path) and os.path.getsize(path) > 1000, timeout)
    def saveok():
        session.refresh()
        p = session.state().get("popup")
        return p if p and p.get("section") == "SAVEOK" else None
    if _wait(saveok, 5):
        session.answer_dialog(button="ok")
    time.sleep(0.3)
    session.refresh()
    return {"ok": bool(ok), "path": path, "size": os.path.getsize(path) if ok else None,
            "popup": session.state().get("popup")}


def load_game(session, path: str, timeout: float = 30) -> dict:
    path = os.path.abspath(path)
    if not os.path.exists(path):
        return {"ok": False, "error": f"{path} not found"}
    session.refresh()
    if session.state().get("popup"):
        return {"ok": False, "error": "a popup is open; answer it first"}
    win32.post_command(session.main_hwnd(), _menu_id(session, "Load Game", 685))
    # Civ2 may ask for confirmation first (own popup) before the file dialog
    def ready():
        session.refresh()
        if _dialog(session):
            return "dialog"
        if session.state().get("popup"):
            return "popup"
        return None
    what = _wait(ready, 8)
    if what == "popup":
        session.answer_dialog(button="ok")
        what = _wait(lambda: _dialog(session) and "dialog", 8)
    if what != "dialog":
        return {"ok": False, "error": "load dialog did not open", "popup": session.state().get("popup")}
    r = win32.fill_file_dialog(_dialog(session).hwnd, path)
    if not r["ok"]:
        return r
    # after loading, Civ2 shows the LOADOK popup ("Difficulty level ...")
    def loaded():
        session.refresh()
        p = session.state().get("popup")
        return p if (p and not _dialog(session)) else None
    p = _wait(loaded, timeout)
    if not p:
        return {"ok": False, "error": "no popup after load (did it load?)"}
    info = {"title": p.get("title"), "lines": p.get("lines"), "section": p.get("section")}
    if p.get("section") == "LOADOK" or (p.get("title") or "").startswith("Difficulty"):
        session.answer_dialog(button="ok")
    time.sleep(0.5)
    session.refresh()
    st = session.state()
    return {"ok": True, "path": path, "loadok": info, "turn": st["turn"], "popup": st.get("popup")}
