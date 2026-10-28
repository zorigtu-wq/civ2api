"""
Session = one attached game + background poller + event log + actions.

Actions are keystrokes only (no code injection). After each action the session
re-reads memory until the state settles, diffs it, and returns the new events.
"""
from __future__ import annotations

import threading
import time

from . import events as E
from . import win32
from .journal import note as J
from .state import Civ2

DIRECTIONS = {"N": "NUM8", "NE": "NUM9", "E": "NUM6", "SE": "NUM3",
              "S": "NUM2", "SW": "NUM1", "W": "NUM4", "NW": "NUM7"}
# Civ2 coordinates are "diamond doubled": moving E is x+2, N is y-2, NE is x+1,y-1.
DIR_DELTA = {"N": (0, -2), "NE": (1, -1), "E": (2, 0), "SE": (1, 1),
             "S": (0, 2), "SW": (-1, 1), "W": (-2, 0), "NW": (-1, -1)}

# Civ2 keyboard commands (from the in-game Orders menu; verify each in your build)
COMMANDS = {
    "wait": "W", "skip": "SPACE", "sentry": "S", "fortify": "F", "build_city": "B",
    "road": "R", "irrigate": "I", "mine": "M", "transform": "O", "clean_pollution": "P",
    "pillage": "SHIFT+P", "home_city": "H", "disband": "SHIFT+D", "unload": "U",
    "center": "C", "end_turn": "CTRL+N", "enter": "ENTER", "escape": "ESC",
}


class Session:
    def __init__(self, game: Civ2, poll_interval: float = 0.25, key_method: str = "post"):
        self.game = game
        self.log = E.EventLog()
        self.poll_interval = poll_interval
        self.key_method = key_method          # "post" (no focus needed) or "sendinput"
        # tech names in order; research popups answered with "ok" take the first one offered
        self.government_plan: list[str] = ["Monarchy"]
        # Trade, then everything Democracy needs (prerequisites from RULES.TXT @CIVILIZE)
        self.research_plan: list[str] = [
            "Masonry", "Pottery", "Trade", "Writing", "Literacy", "The Republic", "Banking", "Horseback Riding", "The Wheel",
            "Masonry", "Construction", "Engineering", "Invention", "Democracy", "Mysticism", "Philosophy",
            "Polytheism", "Monotheism", "Mathematics", "Astronomy", "University", "Pottery", "Seafaring"]
        self._lock = threading.RLock()
        self._last: dict | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_target: int | None = None
        self._pending_gone: dict = {}
        self.refresh()

    # ------------------------------------------------------------------ state
    def refresh(self) -> list[dict]:
        with self._lock:
            cur = self.game.read_state()
            evs = E.diff(self._last, cur)
            prev_popup = (self._last or {}).get("popup")
            if cur["popup"] and (not prev_popup or prev_popup["ptr"] != cur["popup"]["ptr"]
                                 or prev_popup["lines"] != cur["popup"]["lines"]):
                evs.append(E._ev("popup_opened", popup=cur["popup"]))
            elif prev_popup and not cur["popup"]:
                evs.append(E._ev("popup_closed", title=prev_popup.get("title")))
            self._last = cur
            evs = self._debounce(evs)
            self.log.extend(evs)
            return evs

    GONE = ("my_unit_lost", "foreign_unit_gone")
    BORN = ("my_unit_created", "foreign_unit_appeared")

    def _debounce(self, evs: list[dict], hold: float = 1.5) -> list[dict]:
        """Civ2 sets a unit's x < 0 for a moment while it slides between tiles, which
        looks like lost + created. Hold 'gone' events briefly and cancel them if the
        same unit id comes back; report the net move instead."""
        now = time.time()
        out = []
        for e in evs:
            uid = (e.get("unit") or {}).get("id")
            if e["kind"] in self.GONE:
                self._pending_gone[uid] = (e, now)
                continue
            if e["kind"] in self.BORN and uid in self._pending_gone:
                old = self._pending_gone.pop(uid)[0]["unit"]
                new = e["unit"]
                if (old["x"], old["y"]) != (new["x"], new["y"]):
                    out.append(E._ev("unit_moved", id=uid, owner=new["owner"], type=new.get("type_name"),
                                     frm=[old["x"], old["y"]], to=[new["x"], new["y"]]))
                continue
            out.append(e)
        for uid, (e, t) in list(self._pending_gone.items()):
            if now - t >= hold:
                out.append(e)
                del self._pending_gone[uid]
        return out

    def state(self) -> dict:
        with self._lock:
            return self._last

    def start_polling(self):
        if self._thread:
            return
        def loop():
            while not self._stop.wait(self.poll_interval):
                try:
                    self.refresh()
                except Exception as e:  # game closed / transient read failure
                    self.log.extend([E._ev("reader_error", error=str(e))])
                    time.sleep(1.0)
        self._thread = threading.Thread(target=loop, name="civ2-poller", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------------ windows
    def windows(self) -> list[dict]:
        return [w.as_dict() for w in win32.top_windows(self.game.p.pid)]

    def main_hwnd(self) -> int:
        wins = win32.top_windows(self.game.p.pid)
        if not wins:
            raise RuntimeError("no visible CIV2 window")
        # the frame is the window titled "Civilization II ..."; a full-screen picture the game
        # shows after a discovery (seen live, untitled 1707x1071) must not be taken for it
        titled = [w for w in wins if (w.title or "").startswith("Civilization II")]
        return max(titled or wins, key=lambda w: (w.rect[2] - w.rect[0]) * (w.rect[3] - w.rect[1])).hwnd

    def key_target(self) -> int:
        """Popup open -> the popup window; otherwise the game thread's focus window."""
        popup = (self._last or {}).get("popup")
        if popup and popup.get("hwnd"):
            return popup["hwnd"]
        main = self.main_hwnd()
        active, focus = win32.gui_focus(main)
        return focus or active or main

    def ensure_restored(self) -> bool:
        """A minimized game ignores clicks on City Window buttons (seen live 15:4x: every city
        failed while the frame sat at -32000,-32000). Restore it (SC_RESTORE) before UI work."""
        main = self.main_hwnd()
        if win32.user32.IsIconic(main):
            win32.user32.SendMessageW(main, 0x0112, 0xF120, 0)   # WM_SYSCOMMAND, SC_RESTORE
            time.sleep(0.8)
            return True
        return False

    def set_rates(self, tax: int, lux: int, sci: int, max_steps: int = 14) -> dict:
        """Kingdom/Tax Rate. Civ2's sliders ignore posted input, so this uses real mouse
        clicks (moves the cursor for a moment; the game window must be on screen). One arrow
        click per dialog, then OK, then the rates are read back from memory (TCiv+0x13/+0x14)."""
        def rates():
            self.refresh()
            st = self._last
            c = next(c for c in st["civs"] if c["id"] == st["human_civ"])
            return (c["tax_rate"], 10 - c["tax_rate"] - c["science_rate"], c["science_rate"])
        target = (tax, lux, sci)
        if sum(target) != 10:
            return {"ok": False, "error": "tax+lux+sci must be 10 (tens of percent)"}
        self.ensure_restored()
        steps = []
        for _ in range(max_steps):
            cur = rates()
            if cur == target:
                return {"ok": True, "rates": cur, "steps": steps}
            before = {w.hwnd for w in win32.top_windows(self.game.p.pid)}
            self.menu("Tax Rate")
            dlg = None
            for _ in range(15):
                time.sleep(0.2)
                new = [w for w in win32.top_windows(self.game.p.pid) if w.hwnd not in before and not w.title]
                if new:
                    dlg = new[0].hwnd
                    break
            if not dlg:
                return {"ok": False, "error": "Tax Rate dialog did not open", "rates": cur,
                        "popup": (self._last or {}).get("popup")}
            kids = win32.child_windows(dlg)
            bars = sorted([k for k in kids if k.cls == "MSScrollBarClass"], key=lambda k: k.rect[1])
            okb = [k for k in kids if k.cls == "MSControlClass" and k.rect[2] - k.rect[0] > 200]
            if len(bars) != 3 or not okb:
                return {"ok": False, "error": "unexpected Tax Rate dialog layout"}
            i = next((i for i in range(3) if cur[i] > target[i]), None)
            side = "left"
            if i is None:
                i, side = next(i for i in range(3) if cur[i] < target[i]), "right"
            b = bars[i]
            w, h = b.rect[2] - b.rect[0], b.rect[3] - b.rect[1]
            win32.real_click(b.hwnd, 10 if side == "left" else w - 10, h // 2)
            time.sleep(0.4)
            win32.click_window(okb[0].hwnd)
            time.sleep(0.8)
            steps.append(f"{['tax', 'lux', 'sci'][i]} {side}")
        return {"ok": rates() == target, "rates": rates(), "steps": steps}

    def close_city_window(self) -> bool:
        """A City Window (opened when a city is founded, by hand or from a village) takes the
        keyboard focus, so unit keys go to it and nothing moves. Seen live: with no popup the
        game's focus window is the main window; while the City Window is up the focus is a
        child window. ESC posted to the main window closes it."""
        with self._lock:
            if (self._last or {}).get("popup"):
                return False
            main = self.main_hwnd()
            active, focus = win32.gui_focus(main)
            pics = [w for w in win32.top_windows(self.game.p.pid) if not w.title and w.hwnd != main]
            big = [w for w in pics if (w.rect[2] - w.rect[0]) > 1000 and (w.rect[3] - w.rect[1]) > 700]
            if big:
                # full-screen picture (advance discovered etc.): keys are ignored, a click closes it
                win32.click_window(big[0].hwnd)
            elif active and active != main:
                # an untitled modal window owned by the game that is not a text popup
                # (seen live: the picture shown after a discovery). ENTER closes it.
                self.press("ENTER", target=active)
            elif self.city_window():
                self.press("ESC", target=main)
            elif focus and focus != main and focus != self.map_window() and \
                    any(w.hwnd == focus and w.cls == "MSWindowClass" for w in win32.child_windows(main)):
                # another game window (seen live: City Status left open) holds the keyboard
                # focus, so unit keys went to it and nothing moved for 30 minutes
                self.press("ESC", target=focus)
            else:
                return False
        time.sleep(0.4)
        self.refresh()
        return True

    def diag(self) -> dict:
        main = self.main_hwnd()
        active, focus = win32.gui_focus(main)
        popup = (self._last or {}).get("popup")
        out = {"main": main, "gui_active": active, "gui_focus": focus,
               "key_target": self.key_target(), "key_method": self.key_method,
               "main_children": [w.as_dict() for w in win32.child_windows(main)][:40]}
        if popup and popup.get("hwnd"):
            kids = []
            for w in win32.child_windows(popup["hwnd"]):
                d = w.as_dict()
                try:
                    extra = win32.user32.GetClassLongW(w.hwnd, win32.GCL_CBWNDEXTRA)
                    d["cb_wnd_extra"] = extra
                    d["longs"] = [win32.user32.GetWindowLongW(w.hwnd, i) & 0xFFFFFFFF
                                  for i in range(0, max(extra, 0), 4)]
                    ci = win32.control_info_ptr(w.hwnd)
                    d["control_info"] = hex(ci)
                    d["control_type"] = self.game.p.u32(ci) if ci else None
                except Exception as e:
                    d["ci_error"] = str(e)
                kids.append(d)
            out["popup_children"] = kids
        return out

    # ------------------------------------------------------------------ actions
    def press(self, spec: str, target: int | None = None):
        mods, _ = win32.parse_key(spec)
        if mods and self.key_method != "sendinput":
            # A posted key message carries no Ctrl/Shift/Alt state, so the game would see the
            # bare letter ("S" = Sleep!). Use the menu item with this accelerator instead.
            item = win32.find_menu_by_accel(self.main_hwnd(), spec)
            if item is None:
                raise ValueError(f"{spec}: modifier keys cannot be posted and no menu item has this shortcut")
            win32.post_command(self.main_hwnd(), item["id"])
            self.last_target = ("menu", item["text"])
            return self.last_target
        target = target or self.key_target()
        if self.key_method == "sendinput":
            win32.send_input(target, spec)
        else:
            win32.post_key(target, spec)
        self.last_target = target
        return target

    def act(self, action: dict, settle: float = 0.4, timeout: float = 3.0) -> dict:
        """Run one action and return {"ok", "events", "state_summary", ...}."""
        kind = action.get("action") or action.get("type")
        keys: list[str]
        note = None
        with self._lock:
            self.refresh()
            before = self._last
            if kind == "key":
                keys = action["keys"] if isinstance(action.get("keys"), list) else [action["key"]]
            elif kind == "move":
                d = action["dir"].upper()
                if d not in DIRECTIONS:
                    return {"ok": False, "error": f"dir must be one of {list(DIRECTIONS)}"}
                uid = action.get("unit_id")
                au = self._active_unit(before)
                if uid is not None and (au is None or au["id"] != uid):
                    return {"ok": False, "error": "unit is not the active unit",
                            "active_unit": au}
                keys = [DIRECTIONS[d]] * int(action.get("steps", 1))
            elif kind in COMMANDS:
                keys = [COMMANDS[kind]]
            elif kind == "command":
                keys = [COMMANDS[action["name"]]]
            else:
                return {"ok": False, "error": f"unknown action {kind!r}",
                        "known": ["key", "move", "command", *COMMANDS]}
            if before.get("popup") and kind not in ("key", "enter", "escape"):
                note = "a popup is open; keys go to the popup. Use answer_dialog first."
            seq0 = self.log.last_seq()
            for k in keys:
                try:
                    self.press(k)
                except ValueError as e:
                    return {"ok": False, "error": str(e), "keys": keys}
                time.sleep(action.get("gap", 0.15))
        self._settle(settle, timeout)
        evs = self.log.since(seq0)
        out = {"ok": True, "keys": keys, "events": evs, "active_unit": self._active_unit(self._last),
               "popup": self._last.get("popup")}
        if note:
            out["note"] = note
        return out

    def answer_dialog(self, option: int | None = None, button: str | None = None,
                      settle: float = 0.4, timeout: float = 3.0) -> dict:
        """Select a list option by index (arrow keys) and/or confirm with a button."""
        with self._lock:
            self.refresh()
            popup = self._last.get("popup")
            if not popup:
                return {"ok": False, "error": "no popup open"}
            if option is None and (button or "ok").lower() == "ok" and popup.get("section") == "CARAVANMENU":
                # a Caravan entered a city: help build the Wonder there (player's rule)
                rows = [r["name"] for r in popup.get("list") or []] or \
                    [o.get("text") or "" for o in popup.get("options") or []]
                hit = next((i for i, r in enumerate(rows) if "wonder" in r.lower()), None)
                if hit is not None:
                    option = hit
                    J(self, "city", "caravan: help wonder",
                      f"options: {', '.join(rows)} → '{rows[hit]}' (rule: caravans go to wonders)")
            if option is None and (button or "ok").lower() == "ok" and self.government_plan \
                    and popup.get("section") == "PICKGOVT":
                # after a revolution: take the planned government if it is offered
                rows = [r["name"] for r in popup.get("list") or []] or \
                    [o.get("text") or "" for o in popup.get("options") or []]
                low = [r.lower() for r in rows]
                for g in self.government_plan:
                    if g.lower() in low:
                        option = low.index(g.lower())
                        J(self, "research", f"government {g}",
                          f"offered: {', '.join(rows)}. Plan: {self.government_plan} → {g}")
                        break
            if option is None and (button or "ok").lower() == "ok" and self.research_plan \
                    and (popup.get("title") or "").startswith("What discovery") and popup.get("list"):
                # research choice: take the first tech of the plan the list offers
                names = [r["name"].lower() for r in popup["list"]]
                for tech in self.research_plan:
                    if tech.lower() in names:
                        option = names.index(tech.lower())
                        self.log.extend([E._ev("research_choice", tech=tech)])
                        J(self, "research", f"research {tech}",
                          f"offered: {', '.join(r['name'] for r in popup['list'])}. First match in the "
                          f"research plan (Monarchy → Currency/Trade → Philosophy → Monotheism) is {tech}")
                        break
            seq0 = self.log.last_seq()
            ctl = next((c for c in popup.get("option_controls") or [] if c["index"] == option), None) \
                if option is not None else None
            if ctl:
                win32.click_window(ctl["hwnd"])
                time.sleep(0.25)
                self.refresh()
                option = None   # handled by clicking the option row
            radios = popup.get("radios")
            if option is not None and radios and radios.get("items"):
                item = next((r for r in radios["items"] if r["index"] == option), None)
                if item is None:
                    return {"ok": False, "error": f"no option {option}", "popup": popup}
                if radios["selected"] != option:
                    if item.get("hwnd"):
                        win32.click_window(item["hwnd"])
                    else:
                        step = "DOWN" if option > radios["selected"] else "UP"
                        for _ in range(abs(option - radios["selected"])):
                            self.press(step)
                            time.sleep(0.08)
                    time.sleep(0.2)
                    self.refresh()
                    sel = ((self._last.get("popup") or {}).get("radios") or {}).get("selected")
                    if sel != option:
                        return {"ok": False, "error": "radio selection did not land",
                                "wanted": option, "selected": sel, "popup": self._last.get("popup")}
                option = None   # handled
            lb = popup.get("list") or []
            if option is not None and lb and not popup.get("has_listbox") and popup.get("hwnd"):
                # a short list drawn as one clickable row control per item (e.g. PICKGOVT,
                # seen live turn 20): arrow keys do nothing there, a click on the row does
                btn = {b.get("hwnd") for b in popup.get("buttons", [])}
                rows = sorted([w for w in win32.child_windows(popup["hwnd"])
                               if w.visible and w.cls == "MSControlClass" and w.hwnd not in btn],
                              key=lambda w: w.rect[1])
                if len(rows) == len(lb) and 0 <= option < len(rows):
                    win32.click_window(rows[option].hwnd)
                    time.sleep(0.3)
                    option = None
            if option is not None and lb:
                # list-box popup: the selected row is read from the row list itself
                if not 0 <= option < len(lb):
                    return {"ok": False, "error": f"no row {option}", "popup": popup}
                cur = next((r["index"] for r in lb if r["selected"]), 0)
                step = "DOWN" if option > cur else "UP"
                for _ in range(abs(option - cur)):
                    self.press(step)
                    time.sleep(0.08)
                time.sleep(0.15)
                self.refresh()
                lb2 = (self._last.get("popup") or {}).get("list") or []
                now = next((r["index"] for r in lb2 if r["selected"]), None)
                if now != option:
                    return {"ok": False, "error": "selection did not land on option",
                            "wanted": option, "selected_row": now, "popup": self._last.get("popup")}
                option = None
            if option is not None:
                cur = popup.get("selected_item")
                cur = 0 if cur is None or cur < 0 or cur > 1000 else cur
                step = "DOWN" if option > cur else "UP"
                for _ in range(abs(option - cur)):
                    self.press(step)
                    time.sleep(0.08)
                time.sleep(0.1)
                self.refresh()
                sel = (self._last.get("popup") or {}).get("selected_item")
                if sel != option:
                    return {"ok": False, "error": "selection did not land on option",
                            "wanted": option, "selected_item": sel, "popup": self._last.get("popup")}
            b = (button or "ok").lower()
            self.press({"ok": "ENTER", "cancel": "ESC"}.get(b, b))
        self._settle(settle, timeout)
        method = "key"
        cur = self._last.get("popup")
        if cur and cur["ptr"] == popup["ptr"] and cur["lines"] == popup["lines"]:
            # key was ignored: click the matching button control instead
            btns = [x for x in cur.get("buttons", []) if x.get("hwnd")]
            pick = next((x for x in btns if x["std_type"] == b or (x["text"] or "").lower() == b),
                        btns[0] if len(btns) == 1 else None)
            if pick:
                win32.click_window(pick["hwnd"])
                method = f"click hwnd {pick['hwnd']}"
                self._settle(settle, timeout)
        closed = self._last.get("popup") is None or self._last["popup"]["ptr"] != popup["ptr"] \
            or self._last["popup"]["lines"] != popup["lines"]
        if closed and popup.get("section") == "CITYMODAL1" and not self._last.get("popup"):
            # "You must close the City Window": the City Window (opened e.g. after Build New
            # City) is still up and blocks orders. ESC posted to the main window closes it.
            with self._lock:
                self.press("ESC", target=self.main_hwnd())
            self._settle(settle, timeout)
            method += " + ESC (City Window)"
        return {"ok": closed, "method": method, "events": self.log.since(seq0),
                "popup": self._last.get("popup"),
                **({} if closed else {"error": "popup still open", "diag": self.diag()})}

    def activate_unit(self, unit_id: int, max_steps: int = 60) -> dict:
        """Make a specific own unit the active one (also wakes a sleeping/fortified unit):
        View Pieces mode -> walk the map cursor onto the unit with the numpad (cursor is
        read back from memory after every step) -> Orders/Activate Unit."""
        def menu(label):
            cid = win32.find_menu_id(self.main_hwnd(), label)
            if not cid:
                raise ValueError(f"menu item {label!r} not found")
            win32.post_command(self.main_hwnd(), cid)
        with self._lock:
            self.refresh()
            st = self._last
            u = next((x for x in st["units"] if x["id"] == unit_id), None)
            if u is None:
                return {"ok": False, "error": "unit not found"}
            if st.get("popup"):
                return {"ok": False, "error": "a popup is open"}
            if st.get("active_unit") == u["index"] and st.get("unit_selected"):
                return {"ok": True, "already": True}
            menu("View Pieces")
            time.sleep(0.3)
            steps = 0
            while steps < max_steps:
                self.refresh()
                cx, cy = self._last["cursor"]
                if (cx, cy) == (u["x"], u["y"]):
                    break
                def dist(x, y):
                    da, db = (u["x"] - x + u["y"] - y) / 2, (u["y"] - y - (u["x"] - x)) / 2
                    return max(abs(da), abs(db))
                d = min(DIR_DELTA, key=lambda k: dist(cx + DIR_DELTA[k][0], cy + DIR_DELTA[k][1]))
                win32.post_key(self.key_target(), DIRECTIONS[d])
                time.sleep(0.12)
                steps += 1
            self.refresh()
            if tuple(self._last["cursor"]) != (u["x"], u["y"]):
                menu("Move Pieces")
                return {"ok": False, "error": f"cursor did not reach the unit ({self._last['cursor']})"}
            menu("Activate Unit")
            time.sleep(0.5)
        self._settle(0.3, 2.0)
        st = self._last
        ok = st.get("active_unit") == u["index"]
        return {"ok": ok, "steps": steps, "active_unit": self._active_unit(st),
                **({} if ok else {"error": "unit did not become active", "popup": st.get("popup")})}

    # ------------------------------------------------------------------ cities
    def menu(self, label: str):
        cid = win32.find_menu_id(self.main_hwnd(), label)
        if not cid:
            raise ValueError(f"menu item {label!r} not found")
        win32.post_command(self.main_hwnd(), cid)

    def _cursor_to(self, x: int, y: int, max_steps: int = 80) -> bool:
        """Walk the View Pieces cursor to (x, y) with numpad keys (cursor read from memory)."""
        for _ in range(max_steps):
            self.refresh()
            cx, cy = self._last["cursor"]
            if (cx, cy) == (x, y):
                return True
            def dist(ax, ay):
                da, db = (x - ax + y - ay) / 2, (y - ay - (x - ax)) / 2
                return max(abs(da), abs(db))
            d = min(DIR_DELTA, key=lambda k: dist(cx + DIR_DELTA[k][0], cy + DIR_DELTA[k][1]))
            win32.post_key(self.key_target(), DIRECTIONS[d])
            time.sleep(0.12)
        self.refresh()
        return tuple(self._last["cursor"]) == (x, y)

    def map_window(self) -> int:
        kids = [w for w in win32.child_windows(self.main_hwnd())
                if w.cls == "MSWindowClass" and w.visible]
        return max(kids, key=lambda w: (w.rect[2] - w.rect[0]) * (w.rect[3] - w.rect[1])).hwnd

    def city_window(self) -> int | None:
        """The open City Window, found by its shape: a visible child window of the frame
        holding at least 6 wide buttons (Buy, Change, Info, Map, Rename, Happy, View, Exit).
        Keyboard focus is not a reliable sign (seen live: focus back on the frame while the
        City Window stayed open, and on the map after a click)."""
        kids = [w for w in win32.child_windows(self.main_hwnd()) if w.visible]
        btns = [w for w in kids if w.cls == "MSControlClass" and w.rect[2] - w.rect[0] > 60]
        cands = []
        for w in kids:
            if w.cls != "MSWindowClass":
                continue
            r = w.rect
            n = sum(1 for b in btns if r[0] <= b.rect[0] and b.rect[2] <= r[2]
                    and r[1] <= b.rect[1] and b.rect[3] <= r[3])
            if n >= 6:
                cands.append(w)
        # the map window contains the City Window's rectangle too: take the smallest
        return min(cands, key=lambda w: (w.rect[2] - w.rect[0]) * (w.rect[3] - w.rect[1])).hwnd \
            if cands else None

    def open_city(self, city_id: int) -> dict:
        """Open a City Window without the mouse: View Pieces -> cursor onto the city ->
        Center View -> post a click at the centre of the map window."""
        with self._lock:
            self.refresh()
            st = self._last
            c = next((x for x in st["cities"] if x["id"] == city_id), None)
            if c is None:
                return {"ok": False, "error": "city not found"}
            if st.get("popup"):
                return {"ok": False, "error": "a popup is open"}
            if self.city_window():
                self.press("ESC", target=self.main_hwnd())
                time.sleep(0.4)
            self.menu("View Pieces")
            time.sleep(0.3)
            if not self._cursor_to(c["x"], c["y"]):
                self.menu("Move Pieces")
                return {"ok": False, "error": f"cursor did not reach the city ({self._last['cursor']})"}
            self.menu("Center View")
            time.sleep(0.5)
            win32.click_window(self.map_window())
            time.sleep(0.8)
            cw = self.city_window()
        self.refresh()
        return {"ok": bool(cw), "city": c["name"], "city_window": cw,
                **({} if cw else {"error": "City Window did not open", "popup": self._last.get("popup")})}

    # ------------------------------------------------------------------ helpers
    def _active_unit(self, st: dict | None) -> dict | None:
        if not st or st.get("active_unit") is None:
            return None
        idx = st["active_unit"]
        return next((u for u in st["units"] if u["index"] == idx), None)

    def _settle(self, quiet: float, timeout: float):
        """Re-read until no new events for `quiet` seconds (or timeout)."""
        t0 = time.time()
        last_change = t0
        while time.time() - t0 < timeout:
            time.sleep(0.1)
            if self.refresh():
                last_change = time.time()
            if time.time() - last_change >= quiet:
                break
