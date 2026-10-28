"""
Turn saves: one save per turn, so any turn can be rolled back.

  * TurnSaver (background): at the start of each of our turns, as soon as nothing is
    open (no popup, no City Window, no village hunt / save-load in progress), saves
    saves/turnNNN_start_HHMMSS.sav. This also covers turns ended by hand in the game.
  * end_turn(): saves saves/turnNNN_end_HHMMSS.sav, then ends the turn
    (ENTER when no unit is selected, else Orders/End Player Turn). The campaign and
    POST /end_turn use it.
Saves are never overwritten (gameio.save_game refuses), so every turn stays loadable.
"""
from __future__ import annotations

import os
import threading
import time

from . import gameio
from .journal import note as J


def turn_save(session, saves_dir: str, tag: str) -> dict:
    st = session.state() or {}
    name = f"turn{int(st.get('turn', 0)):03d}_{tag}_{time.strftime('%H%M%S')}.sav"
    with session._lock:                       # no other UI work in between
        return gameio.save_game(session, os.path.join(saves_dir, name))


def end_turn(session, saves_dir: str, save: bool = True) -> dict:
    session.refresh()
    st = session.state()
    if st.get("popup"):
        return {"ok": False, "error": "a popup is open; answer it first"}
    if session.close_city_window():
        time.sleep(0.4)
    saved = turn_save(session, saves_dir, "end") if save else None
    if saved is not None and not saved.get("ok"):
        return {"ok": False, "error": "save failed, turn not ended", "save": saved}
    turn0 = st["turn"]
    session.refresh()
    st = session.state()
    J(session, "save", "end turn", "saved before ending the turn" if saved else "turn ended",
      file=os.path.basename(saved["path"]) if saved and saved.get("ok") else None)
    with session._lock:
        if st.get("unit_selected"):
            session.menu("End Player Turn")
        else:
            session.press("ENTER", target=session.main_hwnd())
    return {"ok": True, "turn": turn0, "save": saved}


class TurnSaver:
    def __init__(self, session, saves_dir: str, busy=lambda: False):
        self.s, self.dir, self.busy = session, saves_dir, busy
        self.enabled = True
        self.last_turn = None
        self.saves: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="turnsaver", daemon=True)

    def start(self):
        st = self.s.state() or {}
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def info(self):
        return {"enabled": self.enabled, "last_turn": self.last_turn, "saves": self.saves[-10:]}

    def _run(self):
        while not self._stop.wait(3.0):
            try:
                if not self.enabled:
                    continue
                st = self.s.state()
                if not st or st.get("turn") == self.last_turn:
                    continue
                cw = self.s.city_window()                 # focus on a child window...
                if cw and cw == self.s.map_window():      # ...the map itself is fine
                    cw = None
                if st.get("turn_civ_guess") != st.get("human_civ") or st.get("popup") \
                        or cw or self.busy():
                    continue
                r = turn_save(self.s, self.dir, "start")
                if r.get("ok"):
                    J(self.s, "save", "turn start save", "restore point at the start of every turn (never overwritten)",
                      file=os.path.basename(r["path"]))
                    self.last_turn = st["turn"]
                    self.saves.append({"turn": st["turn"], "path": r["path"], "at": time.strftime("%H:%M:%S")})
            except Exception as e:                       # keep running; report in info()
                self.saves.append({"error": f"{type(e).__name__}: {e}", "at": time.strftime("%H:%M:%S")})
                self.saves[:] = self.saves[-20:]
