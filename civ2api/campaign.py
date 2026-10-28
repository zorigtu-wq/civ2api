"""
Campaign: keep taking goody huts until we own `target` units with >= min_moves.

Hunters = own units whose type has >= min_moves (new ones join automatically).
Each hunter gets the nearest village nobody else is going to; the runner walks it
next to the village; when it is the active unit with moves left a HutHunt
(save -> enter -> load until a >= min_moves unit appears) runs. Only one HutHunt
at a time, because a load rewinds every unit.

Other units are never given orders: when one of them is the active unit the
campaign presses SPACE (Skip Turn) if skip_others is on. When nothing is left to
do this turn it sends "End Player Turn" through the menu.
Popups without options are closed with OK; a popup WITH options stops the
campaign (status "needs_decision") so a human can answer it.
"""
from __future__ import annotations

import os
import threading
import time

from . import events as E
from . import win32
from .hunt import HutHunt
from .journal import note as J
from .pathfind import DIRS

LIVE = ("running", "hunting", "needs_decision")


class Campaign:
    def __init__(self, session, runner, saves_dir, hunts_list, target=10, min_moves=2,
                 max_attempts=20, max_turns=40, max_turns_to_hut=6, skip_others=True, end_turns=True,
                 goal="unit", hunter_moves=None, keep_last=False):
        self.s, self.r, self.g = session, runner, session.game
        self.saves_dir, self.hunts = saves_dir, hunts_list
        self.target, self.min_moves, self.max_attempts = target, min_moves, max_attempts
        self.max_turns, self.max_turns_to_hut = max_turns, max_turns_to_hut
        self.skip_others, self.end_turns = skip_others, end_turns
        self.keep_last = keep_last
        self.hunter_moves = hunter_moves or min_moves   # who may hunt (1 = Settlers too)
        self.goal = goal          # "unit": own `target` units with >= min_moves; "city": gain `target` cities
        self.status, self.message = "running", ""
        self.assign: dict[tuple, tuple] = {}       # unit key -> hut
        self.failed_huts: set = set()
        self.log: list[str] = []
        self.start_turn = None
        self.gained: list[dict] = []
        self.pause = lambda: False
        self.happy_guard = lambda st: False
        self._cancel = threading.Event()
        self._thread = threading.Thread(target=self._run, name="campaign", daemon=True)
        self._idle_since = None

    # ------------------------------------------------------------------ public
    def start(self):
        self.r.auto_cycle = False      # the campaign does the cycling (SPACE) itself
        self._thread.start()
        return self

    def cancel(self):
        self._cancel.set()
        for h in self.hunts:
            if h.info()["live"]:
                h.cancel()

    def info(self):
        st = self.s.state() or {}
        return {"status": self.status, "message": self.message, "target": self.target, "goal": self.goal,
                "min_moves": self.min_moves, "have": self._count(st) if st else None,
                "hunters": self._hunter_view(st) if st else [], "gained": self.gained[-20:],
                "start_turn": self.start_turn, "turn": st.get("turn"), "log": self.log[-15:],
                "live": self.status in LIVE}

    # ------------------------------------------------------------------ helpers
    def _note(self, msg):
        self.log.append(time.strftime("%H:%M:%S ") + msg)
        self.s.log.extend([E._ev("campaign", message=msg)])

    def _set(self, status, msg=""):
        if (status, msg) != (self.status, self.message):
            self.status, self.message = status, msg
            self._note(f"{status}: {msg}")

    def _key(self, u):
        return (u["index"], u["type"], u["owner"])

    def _moves_ok(self, u, n):
        ut = self.g.unit_types[u["type"]]
        return ut["move_points"] >= n * self.g.road_mult and ut["domain"] == "ground"

    def _is_hunter_type(self, u):
        return self._moves_ok(u, self.hunter_moves)

    def _count(self, st):
        if self.goal == "city":
            return len(self.gained)
        me = st["human_civ"]
        return sum(1 for u in st["units"] if u["owner"] == me and self._moves_ok(u, self.min_moves))

    def _hunters(self, st):
        me = st["human_civ"]
        return [u for u in st["units"] if u["owner"] == me and self._is_hunter_type(u)]

    def _hunter_view(self, st):
        out = []
        for u in self._hunters(st):
            h = self.assign.get(self._key(u))
            out.append({"id": u["id"], "unit": u["type_name"], "x": u["x"], "y": u["y"],
                        "moves_left": u["moves_left"], "village": list(h) if h else None})
        return out

    def _adjacent(self, u, hut, W2):
        return any(((u["x"] + dx) % W2, u["y"] + dy) == tuple(hut) for dx, dy in DIRS.values())

    def _menu(self, label):
        main = self.s.main_hwnd()
        cid = win32.find_menu_id(main, label)
        if cid:
            win32.post_command(main, cid)
            return True
        return False

    # ------------------------------------------------------------------ main loop
    def _run(self):
        try:
            while not self._cancel.is_set():
                if self._tick() == "stop":
                    break
                self._cancel.wait(0.8)
            if self._cancel.is_set() and self.status in LIVE:
                self._set("cancelled", "cancelled")
        except Exception as e:
            self._set("failed", f"{type(e).__name__}: {e}")

    def _tick(self):
        if any(h.info()["live"] for h in self.hunts):
            return None                                  # a HutHunt is running
        if self.pause():
            return None                                  # the city job is using the game windows
        self.s.ensure_restored()
        self.s.refresh()
        st = self.s.state()
        if self.start_turn is None:
            self.start_turn = st["turn"]
        have = self._count(st)
        if have >= self.target:
            self._set("done", (f"{have} cities from villages" if self.goal == "city"
                               else f"{have} units with {self.min_moves}+ moves") + " — goal reached")
            return "stop"
        if st["turn"] - self.start_turn > self.max_turns:
            self._set("stopped", f"{self.max_turns} turns passed, {have}/{self.target}")
            return "stop"
        # ---- popups
        p = st.get("popup")
        if p:
            # option rows with no text (e.g. Domestic Advisor DISORDER) are informational
            auto = p.get("section") in ("PICKGOVT", "CARAVANMENU")          # answered by plan/rule
            if not auto and p.get("options") and any((o.get("text") or "").strip() for o in p["options"]):
                self._set("needs_decision", f"popup with choices: {p.get('title')} — please answer it from the console")
                return None
            self.s.answer_dialog(button="ok")
            return None
        if self.status == "needs_decision":
            self._set("running", "in progress")
        if self.s.close_city_window():
            self._blocked = getattr(self, "_blocked", 0) + 1
            if self._blocked > 6:
                self._set("needs_decision", "a game window will not close — please close it manually")
                time.sleep(5)
            else:
                self._note("closed an open window (City Window / picture)")
            return None
        self._blocked = 0
        me = st["human_civ"]
        W2 = st["map"]["width2"]
        # ---- assignments
        huts = {(h["x"], h["y"]) for h in self.g.huts()} - self.failed_huts
        hunters = self._hunters(st)
        keys = {self._key(u) for u in hunters}
        self.assign = {k: h for k, h in self.assign.items() if k in keys and h in huts}
        taken = set(self.assign.values())
        for u in hunters:
            k = self._key(u)
            if k in self.assign:
                continue
            best = None
            for h in sorted(huts - taken, key=lambda h: abs(h[0] - u["x"]) + abs(h[1] - u["y"]))[:8]:
                pl = self.r.plan(u["id"], *h, st=st)
                if pl.get("ok") and pl["turns"] <= self.max_turns_to_hut and (best is None or pl["turns"] < best[0]):
                    best = (pl["turns"], h)
            if best:
                self.assign[k] = best[1]
                taken.add(best[1])
                self._note(f"{u['type_name']} #{u['id']} → village {best[1]} ({best[0]} turns)")
                J(self.s, "campaign", "assign village",
                  f"{u['type_name']} #{u['id']} ({u['x']},{u['y']}): took the 8 nearest villages not claimed by another hunter, "
                  f"ran the pathfinder on each and picked the one with the fewest turns ({best[0]} turns ≤ {self.max_turns_to_hut})",
                  unit_id=u["id"], village=list(best[1]))
        if not self.assign and not any(t["status"] in ("pending", "running") for t in self.r.tasks.values()):
            self._set("stopped", f"no reachable village nearby ({have}/{self.target})")
            return "stop"
        # ---- send hunters next to their villages
        for u in hunters:
            hut = self.assign.get(self._key(u))
            if not hut or self._adjacent(u, hut, W2):
                continue
            t = self.r.tasks.get(u["id"])
            if t and t["status"] in ("pending", "running", "waiting_next_turn", "paused_popup") \
                    and self._adjacent({"x": t["goal"][0], "y": t["goal"][1]}, hut, W2):
                continue                 # already on its way next to this village: no re-plan
            pl = self.r.plan(u["id"], *hut, st=st)
            if not pl.get("ok") or len(pl["steps"]) < 2:
                continue
            stage = [pl["steps"][-2]["x"], pl["steps"][-2]["y"]]
            if not t or t["status"] not in ("pending", "running", "waiting_next_turn", "paused_popup") or t["goal"] != stage:
                self.r.add(u["id"], *stage)
        # ---- act on the active unit
        au = None
        if st.get("unit_selected") and st.get("active_unit") is not None:
            au = next((u for u in st["units"] if u["index"] == st["active_unit"]), None)
        if au is None:
            # nothing (alive) is active, e.g. right after a Settlers founded a city:
            # wake a unit that still has work this turn instead of ending the turn
            if self._wake(st, hunters, W2):
                return None
            return self._maybe_end_turn(st)
        self._idle_since = None
        if au["owner"] != me:
            return None
        hut = self.assign.get(self._key(au))
        if hut and self._adjacent(au, hut, W2) and (au.get("move_points_left") or 0) > 0:
            self._set("hunting", f"{au['type_name']} #{au['id']} → village {hut}")
            J(self.s, "campaign", "start village attempt",
              f"{au['type_name']} #{au['id']} village {tuple(hut)} is next to it, active, has moves → save → enter → "
              + ("load if no new city appears" if self.goal == "city" else f"load if no unit with {self.min_moves}+ moves appears"),
              unit_id=au["id"], village=list(hut))
            before = self._count(st)
            h = HutHunt(self.s, self.r, self.saves_dir, au["id"], hut, self.min_moves, self.max_attempts,
                        goal=self.goal, keep_last=self.keep_last)
            self.hunts.append(h.start())
            h._thread.join()
            res = h.info()
            if res["status"] == "success":
                r0 = res["result"]
                what = (r0.get("cities") or [{}])[0].get("name") if self.goal == "city" \
                    else (r0["gained"] or [{}])[0].get("type")
                self.gained.append({"village": list(hut), "unit": what, "attempts": res["attempts"]})
                self._note(f"gained: {self.gained[-1]['unit']} ({res['attempts']} attempts)")
            else:
                self.failed_huts.add(hut)
                self._note(f"village {hut}: {res['status']} {res['message']}")
                J(self.s, "campaign", "skip village", f"village {tuple(hut)}: {res['message']} → this village will "
                  "not be assigned again", village=list(hut))
            self.assign.pop(self._key(au), None)
            self.s.refresh()
            self._set("running", f"{self._count(self.s.state())}/{self.target}")
            return None
        if au["id"] in self.r.tasks and self.r.tasks[au["id"]]["status"] in (
                "pending", "running", "waiting_next_turn", "paused_popup"):
            return None                                  # runner moves it
        if self.skip_others:
            time.sleep(0.6)
            st2 = self.s.state()
            if st2.get("active_unit") == au["index"] and not st2.get("popup"):
                J(self.s, "campaign", "skip unit", f"{au['type_name']} #{au['id']}: no campaign task "
                  "(not a hunter, or nothing to do this turn) → Skip Turn, no orders given", unit_id=au["id"])
                self._menu("Skip Turn") or self.s.press("SPACE")
        return None

    def _wake(self, st, hunters, W2) -> bool:
        if time.time() - getattr(self, "_woke_at", 0) < 4.0:
            return True                                   # give the last activation time to land
        me = st["human_civ"]
        live = ("pending", "running", "paused_popup")
        cands = []
        for u in st["units"]:
            if u["owner"] != me or (u.get("move_points_left") or 0) <= 0:
                continue
            t = self.r.tasks.get(u["id"])
            hut = self.assign.get(self._key(u))
            if (t and t["status"] in live) or (hut and self._adjacent(u, hut, W2)):
                cands.append(u)
        tries = self.__dict__.setdefault("_wake_tries", {})
        cands = [u for u in cands if tries.get((u["id"], st["turn"]), 0) < 3]
        if not cands:
            return False
        u = cands[0]
        tries[(u["id"], st["turn"])] = tries.get((u["id"], st["turn"]), 0) + 1
        self._woke_at = time.time()
        J(self.s, "campaign", "activate unit", f"the game has no active unit, but {u['type_name']} #{u['id']} "
          "has a task and moves → View Pieces + cursor + Activate Unit", unit_id=u["id"])
        try:
            r = self.s.activate_unit(u["id"])
            self._note(f"{u['type_name']} #{u['id']} was inactive, so activated it: {r.get('ok', r)}")
        except Exception as e:
            self._note(f"activate {u['id']} error: {e}")
        return True

    def _maybe_end_turn(self, st):
        # only when it is clearly our turn (Game+0x1D = our civ; seen = 5 on our turn)
        if not self.end_turns or st.get("turn_civ_guess") != st["human_civ"]:
            self._idle_since = None
            return None
        if self.happy_guard(st):
            self._idle_since = None                      # city job fixes disorder first
            return None
        self._idle_since = self._idle_since or time.time()
        if time.time() - self._idle_since > 3.0:
            self._idle_since = None
            # save first (once per turn) so the turn can be rolled back, then end it:
            # ENTER with no unit selected (the menu command is ignored then, seen turn 22)
            from .turnsave import end_turn
            J(self.s, "campaign", "end turn", "no active unit (all moved/waiting), no popup, "
              "no city in disorder, idle for 3 seconds → save and end the turn")
            first = getattr(self, "_end_saved_turn", None) != st["turn"]
            r = end_turn(self.s, self.saves_dir, save=first)
            if first and r.get("save", {}) and r["save"].get("ok"):
                self._end_saved_turn = st["turn"]
            self._note(f"ending turn {st['turn']}" +
                       (f" (save: {os.path.basename(r['save']['path'])})" if first and r.get("save", {}).get("ok") else "")
                       + ("" if r.get("ok") else f" — {r.get('error')}"))
        return None
