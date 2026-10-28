"""
Goody-hut hunt with save/load retries.

    HutHunt(session, runner, unit_id, (x, y), min_moves=2, max_attempts=20, then=(x2, y2))

1. approach: a runner goto task brings the unit next to the hut (it never steps on
   other huts on the way)
2. when the unit is the game's active unit, next to the hut, with moves left:
   save the game to civ2api/saves/hut_<x>_<y>_<time>.sav
3. step onto the hut, read and close every popup, then look at memory:
   success = a NEW own unit whose type has >= min_moves movement
4. success: optionally send the original unit on to `then`; stop
   failure: load the save and try again (up to max_attempts); after the last
   failure the save is loaded once more so the game is back to "before the hut"
"""
from __future__ import annotations

import itertools
import os
import threading
import time

from .journal import note as J
from . import events as E
from . import gameio
from .pathfind import DIRS
from .session import DIRECTIONS

_ids = itertools.count(1)
LIVE = ("starting", "approaching", "waiting_active", "waiting_next_turn", "saving", "trying", "loading")


class HutHunt:
    def __init__(self, session, runner, saves_dir: str, unit_id: int, hut: tuple, min_moves: int = 2,
                 max_attempts: int = 20, then: tuple | None = None, auto_cycle: bool = True,
                 goal: str = "unit", keep_last: bool = False):
        self.s, self.r, self.g = session, runner, session.game
        self.saves_dir = saves_dir
        self.id = next(_ids)
        self.unit_id, self.hut = unit_id, tuple(hut)
        self.min_moves, self.max_attempts = min_moves, max_attempts
        self.then = tuple(then) if then else None
        self.auto_cycle = auto_cycle
        self.keep_last = keep_last            # out of attempts: keep the last outcome (village gone)
        self.goal = goal                      # "unit": a new unit with >= min_moves; "city": a new city
        self.status, self.message = "starting", ""
        self.attempts = 0
        self.outcomes: list[dict] = []
        self.save_path: str | None = None
        self.result: dict | None = None
        self.started = time.time()
        self._cancel = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"hut-hunt-{self.id}", daemon=True)

    # ------------------------------------------------------------------ public
    def start(self):
        self._thread.start()
        return self

    def cancel(self):
        self._cancel.set()

    def info(self) -> dict:
        return {"id": self.id, "unit_id": self.unit_id, "hut": list(self.hut), "goal": self.goal,
                "min_moves": self.min_moves,
                "max_attempts": self.max_attempts, "then": list(self.then) if self.then else None,
                "status": self.status, "message": self.message, "attempts": self.attempts,
                "save": self.save_path, "result": self.result, "outcomes": self.outcomes[-10:],
                "live": self.status in LIVE}

    # ------------------------------------------------------------------ helpers
    def _ev(self, kind, **kw):
        self.s.log.extend([E._ev(kind, hunt=self.id, **kw)])

    def _set(self, status, message=""):
        if (status, message) != (self.status, self.message):
            self.status, self.message = status, message
            self._ev("hunt_status", status=status, message=message, attempts=self.attempts)

    def _state(self):
        self.s.refresh()
        return self.s.state()

    def _unit(self, st, grace: float = 0.0):
        """The hunted unit. While a unit slides between tiles Civ2 sets its x < 0 for a
        moment, so with grace > 0 a missing unit is re-checked before giving up."""
        u = next((u for u in st["units"] if u["id"] == self.unit_id), None)
        t0 = time.time()
        while u is None and time.time() - t0 < grace:
            time.sleep(0.25)
            st = self._state()
            u = next((u for u in st["units"] if u["id"] == self.unit_id), None)
        return u

    def _dir_to_hut(self, u):
        W2 = self.s.state()["map"]["width2"]
        for d, (dx, dy) in DIRS.items():
            if ((u["x"] + dx) % W2, u["y"] + dy) == self.hut:
                return d
        return None

    def _hut_present(self) -> bool:
        x, y = self.hut
        return any(t["hut"] for t in self.g.read_tiles(x, y, 1, 1) if (t["x"], t["y"]) == self.hut)

    def _need_mp(self) -> int:
        return self.min_moves * self.g.road_mult

    def _sleep(self, t):
        return self._cancel.wait(t)

    # ------------------------------------------------------------------ main
    def _run(self):
        try:
            self._main()
        except Exception as e:
            self._set("failed", f"{type(e).__name__}: {e}")
        finally:
            self.r.enabled = True

    def _main(self):
        st = self._state()
        u = self._unit(st)
        if u is None or u["owner"] != st["human_civ"]:
            return self._set("failed", "unit not found / not yours")
        if not self._hut_present():
            return self._set("failed", f"no village at {self.hut}")
        if not self._approach():
            return
        # ---- save once
        self.r.enabled = False
        self._set("saving")
        name = f"hut_{self.hut[0]}_{self.hut[1]}_{time.strftime('%H%M%S')}.sav"
        res = gameio.save_game(self.s, os.path.join(self.saves_dir, name))
        if not res.get("ok"):
            return self._set("failed", f"save failed: {res.get('error')}")
        self.save_path = res["path"]
        u = self._unit(self._state())
        # Civ2 gives units NEW ids when a game is loaded; the slot index is kept
        self.key = (u["index"], u["type"], u["owner"])
        self._ev("hunt_saved", path=self.save_path)
        want = "a city" if self.goal == "city" else f"a unit with {self.min_moves}+ moves"
        J(self.s, "hunt", "save", f"village ({self.hut[0]},{self.hut[1]}) : restore point before entering; "
          f"if {want} does not appear, load this save and retry", save=os.path.basename(self.save_path))
        # ---- attempts
        while not self._cancel.is_set():
            if not self._ready_next_to_hut(timeout=20):
                return self._set("failed", "unit not active next to the village after save/load")
            self.attempts += 1
            self._set("trying", f"attempt {self.attempts}/{self.max_attempts}")
            out = self._enter_hut()
            self.outcomes.append(out)
            self._ev("hunt_attempt", attempt=self.attempts, outcome=out)
            got = (", ".join(c["name"] for c in out["cities"]) or ", ".join(
                f"{n['type']}({n['moves']:g})" for n in out.get("new_units", [])) or
                "; ".join(" ".join(pp.get("lines") or []) for pp in out.get("popups", [])) or "nothing")
            J(self.s, "hunt", "enter village",
              f"attempt {self.attempts}/{self.max_attempts} → result: {got}. " +
              ("Goal reached — keeping it." if out["success"] else
               ("Not the goal → loading the save." if self.attempts < self.max_attempts else
                "Limit reached → restoring and skipping this village.")),
              village=list(self.hut), attempt=self.attempts, success=out["success"])
            if out["success"]:
                self.result = out
                self.r.enabled = True
                what = ", ".join(c["name"] for c in out["cities"]) if self.goal == "city" \
                    else ", ".join(n["type"] for n in out["gained"])
                msg = f"gained: {what} ({self.attempts} attempts)"
                if self.then and out["unit_alive"]:
                    t = self.r.add(self.unit_id, *self.then)
                    msg += f"; then heading to ({self.then[0]},{self.then[1]}): {t.get('message') or t.get('error')}"
                return self._set("success", msg)
            if self.attempts >= self.max_attempts and self.keep_last and out.get("unit_alive") \
                    and not out.get("barbarians"):   # never keep barbarians or a lost unit
                self.result = out
                self.r.enabled = True
                J(self.s, "hunt", "keep last outcome",
                  f"goal not reached in {self.attempts} attempts; the task is to clear every village, so keeping the last "
                  "result (village is gone) and moving on to the next", village=list(self.hut))
                return self._set("kept", f"{self.attempts} attempts: goal not reached, kept the last result")
            if self.attempts >= self.max_attempts + (10 if self.keep_last else 0):
                self._load()
                want = "a city" if self.goal == "city" else f"a unit with {self.min_moves}+ moves"
                return self._set("gave_up", f"no {want} in {self.attempts} attempts; "
                                            "restored the state before the village")
            if self._repeating():
                self._ev("hunt_note", message="the same result keeps repeating (loading may restore the RNG state)")
            if not self._load():
                return
        self._set("cancelled", "cancelled")

    # ------------------------------------------------------------------ phases
    def _approach(self) -> bool:
        last_cycle = 0.0
        while not self._cancel.is_set():
            st = self._state()
            u = self._unit(st, grace=4.0)
            if u is None:
                self._set("failed", "unit disappeared")
                return False
            if not self._hut_present():
                self._set("failed", "village is gone (someone entered it)")
                return False
            if self._dir_to_hut(u) is None:
                p = self.r.plan(self.unit_id, *self.hut, st=st)
                if not p["ok"]:
                    self._set("failed", f"no path: {p['error']}")
                    return False
                stage = p["steps"][-2] if len(p["steps"]) >= 2 else None
                if stage is None:
                    self._set("failed", "cannot find a tile next to the village")
                    return False
                t = self.r.tasks.get(self.unit_id)
                if not t or t["status"] not in ("pending", "running", "waiting_next_turn", "paused_popup") \
                        or t["goal"] != [stage["x"], stage["y"]]:
                    self.r.add(self.unit_id, stage["x"], stage["y"])
                self._set("approaching", f"heading next to the village ({stage['x']},{stage['y']})")
            elif (u.get("move_points_left") or 0) <= 0:
                self._set("waiting_next_turn", "arrived next to it, out of moves — next turn")
            elif st.get("popup"):
                self._set("waiting_active", "popup open")
            elif st.get("active_unit") != u["index"]:
                self._set("waiting_active", "arrived next to it — waiting for the game to activate this unit")
                au = next((x for x in st["units"] if x["index"] == st.get("active_unit")), None)
                if self.auto_cycle and au and au["owner"] == st["human_civ"] and au["id"] not in self.r.tasks \
                        and time.time() - last_cycle > 1.5:
                    last_cycle = time.time()
                    with self.s._lock:
                        self.s.press("W")
            else:
                return True
            self._sleep(0.8)
        self._set("cancelled", "cancelled")
        return False

    def _ready_next_to_hut(self, timeout) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout and not self._cancel.is_set():
            st = self._state()
            u = self._unit(st)
            if u and self._dir_to_hut(u) and not st.get("popup") and st.get("active_unit") == u["index"] \
                    and (u.get("move_points_left") or 0) > 0:
                return True
            if st.get("popup") and st["popup"].get("section") in ("LOADOK", "SAVEOK"):
                self.s.answer_dialog(button="ok")
            elif u and st.get("active_unit") != u["index"] and not st.get("popup"):
                au = next((x for x in st["units"] if x["index"] == st.get("active_unit")), None)
                if au and au["owner"] == st["human_civ"]:
                    with self.s._lock:
                        self.s.press("W")
            self._sleep(0.6)
        return False

    def _enter_hut(self) -> dict:
        st = self._state()
        me = st["human_civ"]
        u = self._unit(st)
        before_ids = {x["id"] for x in st["units"] if x["owner"] == me}
        foreign_before = {x["id"] for x in st["units"] if x["owner"] != me}
        civ = next(c for c in st["civs"] if c["id"] == me)
        gold0, techs0 = civ["gold"], civ["techs"]
        cities_before = {(c["x"], c["y"]) for c in st["cities"] if c["owner"] == me}
        d = self._dir_to_hut(u)
        with self.s._lock:
            self.s.press(DIRECTIONS[d])
        popups, quiet_since = [], None
        t0 = time.time()
        while time.time() - t0 < 25:
            self._sleep(0.4)
            st = self._state()
            p = st.get("popup")
            if p:
                quiet_since = None
                popups.append({"title": p.get("title"), "lines": p.get("lines"), "section": p.get("section"),
                               "options": [o["text"] for o in p.get("options", [])]})
                if p.get("options"):
                    self.s.answer_dialog(option=0, button="ok")
                else:
                    self.s.answer_dialog(button="ok")
                continue
            quiet_since = quiet_since or time.time()
            if time.time() - quiet_since > 1.5:
                break
        st = self._state()
        civ = next(c for c in st["civs"] if c["id"] == me)
        new = [x for x in st["units"] if x["owner"] == me and x["id"] not in before_ids]
        need = self._need_mp()
        gained = []
        for x in new:
            ut = self.g.unit_types[x["type"]]
            gained.append({"id": x["id"], "type": x["type_name"], "moves": ut["moves"],
                           "x": x["x"], "y": x["y"], "ok": ut["move_points"] >= need})
        foreign_new = [x for x in st["units"] if x["owner"] != me and x["id"] not in foreign_before]
        u2 = self._unit(st, grace=2.0)
        new_cities = [{"name": c["name"], "x": c["x"], "y": c["y"], "size": c["size"]}
                      for c in st["cities"] if c["owner"] == me and (c["x"], c["y"]) not in cities_before]
        success = bool(new_cities) if self.goal == "city" else any(g["ok"] for g in gained)
        return {"attempt": self.attempts, "success": success, "cities": new_cities,
                "gained": [g for g in gained if g["ok"]], "new_units": gained,
                "gold": civ["gold"] - gold0, "techs": civ["techs"] - techs0,
                "barbarians": [{"type": x["type_name"], "x": x["x"], "y": x["y"]} for x in foreign_new],
                "unit_alive": u2 is not None, "popups": popups}

    def _load(self) -> bool:
        self._set("loading", f"loading the save (attempt {self.attempts})")
        self.r.enabled = False
        res = gameio.load_game(self.s, self.save_path)
        if not res.get("ok"):
            self._set("failed", f"load failed: {res.get('error')}")
            return False
        self._sleep(0.8)
        st = self._state()
        idx, typ, own = self.key
        u = next((x for x in st["units"] if (x["index"], x["type"], x["owner"]) == (idx, typ, own)), None)
        if u is None:
            self._set("failed", "unit not found after load")
            return False
        if u["id"] != self.unit_id:
            self._ev("hunt_note", message=f"load: unit id {self.unit_id} -> {u['id']}")
            self.unit_id = u["id"]
        return True

    def _repeating(self, n=3) -> bool:
        if len(self.outcomes) < n:
            return False
        sig = [(tuple(p["title"] or "" for p in o["popups"]), tuple(g["type"] for g in o["new_units"]),
                o["gold"], o["techs"]) for o in self.outcomes[-n:]]
        return len(set(sig)) == 1
