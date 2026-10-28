"""
Unit tasks ("orders") executed by the API, not by Civ2's own goto.

    runner = TaskRunner(session); runner.start()
    runner.add(unit_id, x, y)          # goto, fastest path (roads, rivers, railroad)

When a unit with a task becomes the game's active unit, the runner plans a path
from where the unit is now, sends ONE keystroke for the first step, checks that
the unit really moved, and repeats. It re-plans every step, so new roads, units
or ZOC are taken into account. It stops and waits when:
  * a popup is open (e.g. a goody hut result)  -> status "paused_popup"
  * the next tile needs more MP than is left   -> presses SPACE (unit done this turn)
  * the unit vanished or could not move twice   -> status "failed"
Observed move costs are compared with the planner's costs and logged, so the
river/road assumptions are checked against the real game.
"""
from __future__ import annotations

import itertools
import threading
import time

from . import events as E
from . import memmap as M
from .journal import note as J
from .pathfind import Planner
from .session import DIRECTIONS

ACTIVE = ("pending", "running", "waiting_next_turn", "paused_popup")
_ids = itertools.count(1)


class TaskRunner:
    def __init__(self, session, auto_cycle: bool = False):
        self.s = session
        self.g = session.game
        self.tasks: dict[int, dict] = {}       # unit_id -> task
        self.enabled = True
        self.auto_cycle = auto_cycle            # press W on task-less units to reach task units
        self.cost_log: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._last_cycle = 0.0
        self.status = {"message": "", "active_unit": None}

    # ------------------------------------------------------------------ planning
    def _unit_info(self, u: dict) -> dict:
        ut = self.g.unit_types[u["type"]] if u["type"] < len(self.g.unit_types) else {}
        return {**u, "domain": ut.get("domain", "ground"), "abilities": ut.get("abilities", 0),
                "full_mp": ut.get("move_points", self.g.road_mult)}

    def planner(self, st: dict) -> Planner:
        # Reading the 10 000-tile map and building the graph takes ~0.5 s; the campaign asks
        # for dozens of plans per tick. Reuse one planner while the units have not moved.
        key = (st.get("turn"), tuple((u["id"], u["x"], u["y"]) for u in st["units"]),
               tuple((c["x"], c["y"]) for c in st["cities"]))
        cached = getattr(self, "_planner_cache", None)
        if cached and cached[0] == key and time.time() - cached[1] < 20:
            return cached[2]
        pl = self._build_planner(st)
        self._planner_cache = (key, time.time(), pl)
        return pl

    def _build_planner(self, st: dict) -> Planner:
        ms = st["map"]
        tiles = self.g.read_tiles(0, 0, ms["width2"], ms["height"])
        flat = bool(self.g.p.u16(M.GAME) & 0x8000)
        units = [self._unit_info(u) for u in st["units"]]
        return Planner(tiles, ms["width2"], ms["height"], units, st["cities"], st["human_civ"],
                       self.g.road_mult, flat, getattr(self.g.rules, "terrain_move", None))

    def plan(self, unit_id: int, x: int, y: int, st: dict | None = None) -> dict:
        st = st or self.s.state()
        u = next((u for u in st["units"] if u["id"] == unit_id), None)
        if u is None:
            return {"ok": False, "error": f"unit {unit_id} not found"}
        info = self._unit_info(u)
        res = self.planner(st).plan(info, (x, y), info["full_mp"], u.get("move_points_left"))
        res.update(unit_id=unit_id, from_=[u["x"], u["y"]], to=[x, y], unit=u["type_name"])
        return res

    # ------------------------------------------------------------------ task CRUD
    def add(self, unit_id: int, x: int, y: int, then: str | None = None) -> dict:
        st = self.s.state()
        u = next((u for u in st["units"] if u["id"] == unit_id), None)
        if u is None:
            return {"ok": False, "error": f"unit {unit_id} not found"}
        if u["owner"] != st["human_civ"]:
            return {"ok": False, "error": "not your unit"}
        p = self.plan(unit_id, x, y, st)
        if not p["ok"]:
            return p
        old = self.tasks.get(unit_id)
        if old and old["status"] in ACTIVE and old["goal"] == [x, y]:
            return {"ok": True, "task": self._public(old), "plan": p, "duplicate": True,
                    "message": "this task already exists"}
        t = {"task_id": next(_ids), "type": "goto", "unit_id": unit_id, "unit": u["type_name"],
             "key": [u["index"], u["type"], u["owner"]], "then": then,
             "goal": [x, y], "status": "pending", "created": time.time(), "steps_done": 0,
             "eta_turns": p["turns"], "fails": 0, "history": [], "note": None}
        with self._lock:
            self.tasks[unit_id] = t
        self._event("task_created", task=self._public(t), plan_turns=p["turns"],
                    path=[[s["x"], s["y"]] for s in p["steps"]])
        left = u.get("move_points_left") or 0
        active = st.get("active_unit") == u["index"]
        if left <= 0:
            msg = "no moves left this turn — will start next turn"
        elif active:
            msg = "starting now"
        else:
            msg = "will go when the game activates this unit" + ("" if self.auto_cycle else " (turn on auto-cycle to switch to it right away)")
        return {"ok": True, "task": self._public(t), "plan": p, "message": msg, "moves_now": left > 0 and active}

    def cancel(self, unit_id: int) -> dict:
        with self._lock:
            t = self.tasks.get(unit_id)
            if not t:
                return {"ok": False, "error": "no task"}
            t["status"] = "cancelled"
        self._event("task_cancelled", task=self._public(t))
        return {"ok": True}

    def list(self) -> list[dict]:
        with self._lock:
            return [self._public(t) for t in sorted(self.tasks.values(), key=lambda t: t["task_id"])]

    def _public(self, t: dict) -> dict:
        return {k: v for k, v in t.items() if k != "history"} | {"last_steps": t["history"][-5:]}

    # ------------------------------------------------------------------ loop
    def start(self):
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="civ2-tasks", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(0.4):
            if not self.enabled:
                continue
            try:
                self.tick()
            except Exception as e:  # never let the runner die
                self._event("task_runner_error", error=f"{type(e).__name__}: {e}")
                time.sleep(2)

    def tick(self):
        st = self.s.state()
        if not st:
            return
        with self._lock:
            live = {uid: t for uid, t in self.tasks.items() if t["status"] in ACTIVE}
        au0 = next((u for u in st["units"] if u["index"] == st.get("active_unit")), None)
        self.status["active_unit"] = None if au0 is None else {
            "id": au0["id"], "unit": au0["type_name"], "x": au0["x"], "y": au0["y"],
            "moves_left": au0["moves_left"], "has_task": au0["id"] in live}
        if not live:
            self.status["message"] = ""
            return
        if self.s.close_city_window():
            self.status["message"] = "closed the City Window"
            return
        ids = {u["id"] for u in st["units"]}
        units = {u["id"]: u for u in st["units"]}
        now = time.time()
        for uid, t in list(live.items()):
            if uid in ids:
                t.pop("missing_since", None)
                continue
            # sliding units have x < 0 for a moment: give a missing unit a few seconds
            t.setdefault("missing_since", now)
            if now - t["missing_since"] < 4.0:
                live.pop(uid)
                continue
            # a loaded game renumbers units: follow the same slot/type/owner
            k = tuple(t.get("key") or ())
            same = next((u for u in st["units"] if (u["index"], u["type"], u["owner"]) == k), None)
            live.pop(uid)
            if same is not None and same["id"] not in self.tasks:
                with self._lock:
                    self.tasks.pop(uid, None)
                    t["unit_id"] = same["id"]
                    self.tasks[same["id"]] = t
                live[same["id"]] = t
                self._event("task_unit_renumbered", old=uid, new=same["id"])
            else:
                self._finish(t, "failed", "unit no longer exists")
        for uid, t in live.items():
            u = units.get(uid)
            if u is not None and (u.get("move_points_left") or 0) <= 0 and t["status"] in ("pending", "running"):
                t["status"] = "waiting_next_turn"
                t["note"] = "no moves left this turn — will continue next turn"
        ready = [t for uid, t in live.items() if (units.get(uid) or {}).get("move_points_left", 0) > 0]
        if st.get("popup"):
            self.status["message"] = "popup open — will continue once it is answered"
        elif au0 is not None and au0["id"] not in live:
            self.status["message"] = (
                f"Active in game: {au0['type_name']} #{au0['id']} (no task). "
                + (f"{len(ready)} tasked unit(s) with moves are waiting — "
                   + ("auto-cycle will switch" if self.auto_cycle else "select that unit in the game or turn on auto-cycle")
                   if ready else "Tasked units are out of moves this turn"))
        else:
            self.status["message"] = ""
        if st.get("popup"):
            for t in live.values():
                if t["status"] == "running":
                    t["status"] = "paused_popup"
            return
        if not st.get("unit_selected") or st.get("active_unit") is None:
            return
        au = next((u for u in st["units"] if u["index"] == st["active_unit"]), None)
        if au is None or au["owner"] != st["human_civ"]:
            return
        t = live.get(au["id"])
        if t is None:
            self._maybe_cycle(st, live)
            return
        self._step(t, au, st)

    def _maybe_cycle(self, st, live):
        if not self.auto_cycle or time.time() - self._last_cycle < 1.0:
            return
        waiting = [u for u in st["units"] if u["id"] in live and (u.get("move_points_left") or 0) > 0
                   and live[u["id"]].get("skipped_turn") != st["turn"]]
        if waiting:
            self._last_cycle = time.time()
            with self.s._lock:
                self.s.press("W")

    def _step(self, t: dict, au: dict, st: dict):
        if t.get("then") == "found_city" and t.get("site_checked") != (st["turn"], tuple(t["goal"])) \
                and getattr(self, "site_ok", None):
            t["site_checked"] = (st["turn"], tuple(t["goal"]))
            if not self.site_ok(t["goal"]):
                new = self.site_replan(au["id"])
                if not new:
                    return self._finish(t, "failed", f"{t['goal']} now overlaps, no other free site")
                self._event("city_site_changed", unit_id=au["id"], old=t["goal"], new=new)
                J(self.s, "runner", "change city site",
                  f"({t['goal'][0]},{t['goal'][1]}) now overlaps another city's 21 tiles (a new city appeared "
                  f"from a village) → next best non-overlapping site ({new[0]},{new[1]})", unit_id=au["id"])
                t["note"] = f"{t['goal']} now overlaps → {new}"
                t["goal"] = new
                t["site_checked"] = (st["turn"], tuple(new))
        if [au["x"], au["y"]] == t["goal"]:
            if t.get("then") == "found_city":
                return self._found_city(t, au, st)
            self._finish(t, "done", f"arrived in {t['steps_done']} steps")
            return
        p = self.plan(au["id"], *t["goal"], st=st)
        if not p["ok"]:
            self._finish(t, "failed", p["error"])
            return
        t["eta_turns"] = p["turns"]
        s0 = p["steps"][0]
        if s0["turn"] > 0:
            # Not enough MP for the next tile. Civ2 still lets a unit with partial MP try
            # (random); a failed try only ends its moves, same as SPACE. Try once per turn.
            left = au.get("move_points_left") or 0
            if left > 0 and t.get("attempt_turn") != st["turn"]:
                t["attempt_turn"] = st["turn"]
                if self._do_move(t, au, st, s0, p, attempt=True):
                    return
            if t.get("skipped_turn") != st["turn"]:
                t["skipped_turn"] = st["turn"]
                t["status"] = "waiting_next_turn"
                t["note"] = f"next tile ({s0['x']},{s0['y']}) costs {s0['cost']} MP, left {left}"
                J(self.s, "runner", "skip (SPACE)",
                  f"{au['type_name']} #{au['id']}: next tile ({s0['x']},{s0['y']}) needs {s0['cost']} MP, "
                  f"only {left} MP left → skipping the turn, will continue next turn", unit_id=au["id"])
                self._event("task_waiting", task=self._public(t))
                st2 = self.s.state()
                if st2.get("active_unit") == au["index"] and not st2.get("popup"):
                    with self.s._lock:
                        self.s.press("SPACE")
            return
        self._do_move(t, au, st, s0, p)

    def _do_move(self, t, au, st, s0, p, attempt=False) -> bool:
        """Press the key for step s0; return True if the unit reached s0."""
        t["status"] = "running"
        before = (au["x"], au["y"], au["moves_spent_raw"], st["turn"])
        with self.s._lock:
            self.s.press(DIRECTIONS[s0["dir"]])
        moved = self._wait_moved(au["id"], before[:2], timeout=2.5)
        st2 = self.s.state()
        u2 = next((u for u in st2["units"] if u["id"] == au["id"]), None)
        if u2 is None:
            if st2.get("popup"):
                t["status"] = "paused_popup"
                return True
            self._finish(t, "failed", "unit disappeared after move")
            return True
        if (u2["x"], u2["y"]) == (s0["x"], s0["y"]):
            t["steps_done"] += 1
            t["fails"] = 0
            observed = (u2["moves_spent_raw"] - before[2]) if st2["turn"] == before[3] else None
            rec = {"from": [before[0], before[1]], "to": [u2["x"], u2["y"]], "via": s0["via"],
                   "terrain": s0["terrain"], "predicted": s0["cost"], "observed": observed,
                   "attempt": attempt}
            t["history"].append(rec)
            self.cost_log.append(rec)
            capped = u2["moves_spent_raw"] >= self._unit_info(u2)["full_mp"]
            if observed is not None and observed != s0["cost"] and not (observed < s0["cost"] and capped):
                self._event("task_cost_mismatch", unit_id=au["id"], **rec)
            J(self.s, "runner", f"move {s0['dir']}", 
              f"{au['type_name']} #{au['id']} ({before[0]},{before[1]})→({u2['x']},{u2['y']}): fewest turns/MP path (Dijkstra) "
              f"to goal ({t['goal'][0]},{t['goal'][1]}); this step {s0['via']} "
              f"{s0.get('terrain','')}, predicted {s0['cost']} MP, actual {observed}; {len(p['steps']) - 1} steps left, "
              f"~{p['turns']} turns" + (" (attempt with insufficient MP)" if attempt else ""),
              unit_id=au["id"], to=[u2["x"], u2["y"]])
            self._event("task_step", unit_id=au["id"], unit=au["type_name"], to=[u2["x"], u2["y"]],
                        via=s0["via"], predicted=s0["cost"], observed=observed, attempt=attempt,
                        moves_left=u2["moves_left"], remaining_steps=len(p["steps"]) - 1)
            if [u2["x"], u2["y"]] == t["goal"]:
                if t.get("then") == "found_city":
                    t["status"], t["note"] = "pending", "arrived — founding a city"
                else:
                    self._finish(t, "done", f"arrived in {t['steps_done']} steps")
            elif st2.get("popup"):
                t["status"] = "paused_popup"
            return True
        if st2.get("popup"):
            t["status"] = "paused_popup"
            t["note"] = "popup after move attempt"
            return True
        if attempt:
            return False            # partial-MP try failed: caller waits for next turn
        if not moved:
            t["fails"] += 1
            t["note"] = f"move {s0['dir']} to ({s0['x']},{s0['y']}) did not happen"
            if t["fails"] >= 2:
                self._finish(t, "failed", t["note"])
        return False

    def _found_city(self, t, au, st):
        """At the goal with moves left: Orders/Build New City, accept the name popup."""
        if (au.get("move_points_left") or 0) <= 0:
            t["status"], t["note"] = "waiting_next_turn", "arrived — will found a city next turn"
            return
        me = st["human_civ"]
        t["status"] = "running"
        J(self.s, "runner", "build city (B)",
          f"Settlers #{au['id']} reached its chosen site ({t['goal'][0]},{t['goal'][1]}) with moves left. "
          + (t.get("why") or "site chosen by city_sites"), unit_id=au["id"], at=t["goal"])
        with self.s._lock:
            self.s.press("B")
        t0, popups = time.time(), []
        while time.time() - t0 < 12:
            time.sleep(0.4)
            self.s.refresh()
            st2 = self.s.state()
            p = st2.get("popup")
            if p:
                popups.append(p.get("title"))
                self.s.answer_dialog(button="ok")
                continue
            if any(c["owner"] == me and [c["x"], c["y"]] == t["goal"] for c in st2["cities"]):
                time.sleep(0.6)
                self.s.refresh()
                if not self.s.state().get("popup"):
                    break
        city = next((c for c in self.s.state()["cities"] if c["owner"] == me and [c["x"], c["y"]] == t["goal"]), None)
        if city:
            # the game opens the City Window for a new city; it blocks every later order
            time.sleep(0.5)
            with self.s._lock:
                self.s.press("ESC", target=self.s.main_hwnd())
            time.sleep(0.5)
            self.s.refresh()
            self._finish(t, "done", f"city founded: {city['name']}")
            self._event("city_built", unit_id=au["id"], name=city["name"], at=t["goal"], popups=popups)
        else:
            t["fails"] += 1
            t["note"] = f"no city was founded (popup: {popups})"
            if t["fails"] >= 2:
                self._finish(t, "failed", t["note"])

    def _wait_moved(self, uid, xy, timeout):
        t0 = time.time()
        while time.time() - t0 < timeout:
            time.sleep(0.15)
            self.s.refresh()
            st = self.s.state()
            u = next((u for u in st["units"] if u["id"] == uid), None)
            if st.get("popup") or u is None or (u["x"], u["y"]) != tuple(xy):
                time.sleep(0.2)
                self.s.refresh()
                return True
        return False

    def _finish(self, t, status, note):
        t["status"], t["note"] = status, note
        self._event(f"task_{status}", task=self._public(t))

    def _event(self, kind, **data):
        self.s.log.extend([E._ev(kind, **data)])
