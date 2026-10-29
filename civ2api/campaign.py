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
                 goal="unit", hunter_moves=None, keep_last=False, near_city=2, explore=True):
        self.s, self.r, self.g = session, runner, session.game
        self.saves_dir, self.hunts = saves_dir, hunts_list
        self.target, self.min_moves, self.max_attempts = target, min_moves, max_attempts
        self.max_turns, self.max_turns_to_hut = max_turns, max_turns_to_hut
        self.skip_others, self.end_turns = skip_others, end_turns
        self.keep_last = keep_last
        self.near_city = near_city   # goal "city": a village this close to a city never becomes a city -> "tech"
        self.techs_gained: list[dict] = []
        self.explore = explore       # hunters with no village to go to uncover the dark (unexplored) land
        self.exploring: dict[int, list] = {}       # unit id -> target
        self.no_hut: dict[tuple, int] = {}         # unit key -> turn it had no reachable village
        self.sleep_idle = True                     # idle units: go to the nearest own city and sentry
        self.terraform = True                      # Settlers/Engineers irrigate worked tiles
        self.auto_workers = True                   # first choice: the game's own "Automate" order
        self.roads = True                          # Engineers also build the city road/railroad network
        self.road_max = 60
        self.road_plan = None
        self.road_status: dict = {}
        self.road_jobs: dict[int, list] = {}       # unit id -> tile
        self.road_busy: dict[tuple, tuple] = {}    # tile -> (unit id, turn the order was given)
        self.rail_known = lambda: False
        self.wonder_cities = lambda: []
        self.workers: dict[int, list] = {}         # unit id -> tile to irrigate
        self.bad_tiles: set = set()
        self.homing: dict[int, int] = {}           # unit id -> turn it was sent to a city
        self.explore_tried: dict[tuple, int] = {}  # unit key -> turn it last found nothing to explore
        self.explored = None                       # {"land": n, "explored": n, "pct": x}
        self._tiles = None
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
                "techs_gained": self.techs_gained[-20:], "near_city": self.near_city,
                "explore": self.explore, "exploring": {str(k): v for k, v in self.exploring.items()},
                "explored": self.explored, "roads": self.road_status,
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

    WORKERS = ("Settlers", "Engineers")
    NOT_HUNTERS = ("Settlers", "Engineers", "Caravan", "Freight", "Diplomat", "Spy")   # Freight: wonder carriers

    def _is_hunter_type(self, u):
        return u["type_name"] not in self.NOT_HUNTERS and self._moves_ok(u, self.hunter_moves)

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

    def _dist(self, a, b, W2):
        dx = abs(a[0] - b[0])
        dx = min(dx, W2 - dx)
        dy = abs(a[1] - b[1])
        return max(dy, (dx + dy) // 2)

    def _explore(self, st, hunters, W2):
        """Hunters with no village: go to the nearest reachable land tile that uncovers the
        most unexplored tiles (the tile and its 8 neighbours), spread at least 4 tiles apart."""
        live = ("pending", "running", "waiting_next_turn", "paused_popup")
        turn = st["turn"]
        self.exploring = {k: v for k, v in self.exploring.items()
                          if (self.r.tasks.get(k) or {}).get("status") in live}
        # a carrier that was sent exploring by mistake (Freight has 2 moves): call it back
        for u in st["units"]:
            if u["id"] in self.exploring and u["type_name"] in self.NOT_HUNTERS:
                self.exploring.pop(u["id"], None)
                self.r.cancel(u["id"])
        tries = self.__dict__.setdefault("_explore_tries", {})
        idle = [u for u in hunters if self._key(u) not in self.assign and u["id"] not in self.exploring
                and (self.r.tasks.get(u["id"]) or {}).get("status") not in live
                and self.explore_tried.get(self._key(u)) != turn
                and tries.get((u["id"], turn), 0) < 3]           # a target that fails at once: not again this turn
        if not idle:
            return
        if self._tiles is None or self._tiles[0] != turn or time.time() - self._tiles[1] > 60:
            ms = st["map"]
            self._tiles = (turn, time.time(), self.g.read_tiles(0, 0, ms["width2"], ms["height"]))
        tiles = self._tiles[2]
        bit = 1 << st["human_civ"]
        land = [t for t in tiles if t["terrain"] != "Ocean"]
        dark = {(t["x"], t["y"]) for t in tiles if not (t["explored_mask"] & bit)}
        n_exp = sum(1 for t in land if t["explored_mask"] & bit)
        self.explored = {"land": len(land), "explored": n_exp, "pct": round(100 * n_exp / max(1, len(land)), 1),
                         "dark_tiles": len(dark)}
        if not dark:
            return
        planner = self.r.planner(st)
        taken = list(self.exploring.values())
        for u in idle:
            info = self.r._unit_info(u)
            reach = planner.reach(info, info["full_mp"], u.get("move_points_left"), 8)
            best = None
            for xy, turns in reach.items():
                gain = sum(1 for dx, dy in [(0, 0)] + list(DIRS.values())
                           if ((xy[0] + dx) % W2, xy[1] + dy) in dark)
                if gain < 2 or any(self._dist(xy, t, W2) < 4 for t in taken):
                    continue
                k = (turns, -gain)
                if best is None or k < best[0]:
                    best = (k, xy)
            if best is None:
                self.explore_tried[self._key(u)] = turn
                continue
            (turns, ngain), xy = best
            tries[(u["id"], turn)] = tries.get((u["id"], turn), 0) + 1
            r = self.r.add(u["id"], *xy)
            if r.get("ok"):
                self.exploring[u["id"]] = list(xy)
                taken.append(xy)
                self._note(f"{u['type_name']} #{u['id']} → unexplored area {xy} ({turns} turns, reveals {-ngain} tiles)")
                J(self.s, "campaign", "explore",
                  f"{u['type_name']} #{u['id']}: no village to go to → nearest unexplored area {xy} ({turns} turns); "
                  f"going there reveals {-ngain} unexplored tiles; ≥4 tiles from other explorers "
                  f"({self.explored['pct']}% of the map explored)", unit_id=u["id"], target=list(xy))
            else:
                self.explore_tried[self._key(u)] = turn

    IRRIGABLE = {"Grassland": 5, "Plains": 5, "Desert": 5, "Hills": 10, "Tundra": 10}   # RULES.TXT turns
    CARDINAL = ((0, -2), (2, 0), (0, 2), (-2, 0))

    def _terraform(self, au, st) -> bool:
        """Settlers/Engineers (player: "engineer hiihed anhaar"): irrigate the city tiles that
        are worked and lack irrigation, water next to them (river on the tile, or ocean / river /
        irrigation / a city on a N-E-S-W neighbour). On the target tile -> I; else go there."""
        if self.roads and self._road_work(au, st):
            return True
        if self.auto_workers:
            # player's tip: the game's own settler automation. NOT Ctrl+K: in this game Ctrl+K is
            # "Toggle Cheat Mode" (Kingdom menu, seen live). Find the Orders item by its text.
            def walk(items):
                for it in items:
                    name = (it.get("text") or "").split("\t")[0].strip().lower()
                    if it.get("id") not in (None, 0xFFFFFFFF) and name.startswith("automate"):
                        return it
                    r = walk(it.get("children") or [])
                    if r:
                        return r
                return None
            it = walk(win32.menu_tree(self.s.main_hwnd()))
            if it:
                win32.post_command(self.s.main_hwnd(), it["id"])
                J(self.s, "campaign", "automate worker", f"{au['type_name']} #{au['id']}: game's own automation "
                  f"('{it['text'].split(chr(9))[0]}') → the game picks irrigation, roads and mines itself",
                  unit_id=au["id"])
                return True
        W2 = st["map"]["width2"]
        turn = st["turn"]
        if self._tiles is None or self._tiles[0] != turn or time.time() - self._tiles[1] > 60:
            ms = st["map"]
            self._tiles = (turn, time.time(), self.g.read_tiles(0, 0, ms["width2"], ms["height"]))
        T = {(t["x"], t["y"]): t for t in self._tiles[2]}
        me = st["human_civ"]
        mine = [c for c in st["cities"] if c["owner"] == me]
        cxy = {(c["x"], c["y"]) for c in st["cities"]}
        from .memmap import CITY_TILES
        worked = {}
        for c in mine:
            for i in c.get("worked_tiles", []):
                if i < 20:
                    dx, dy = CITY_TILES[i]
                    worked[((c["x"] + dx) % W2, c["y"] + dy)] = c["name"]

        def watered(xy):
            t = T.get(xy)
            if t and t["river"]:
                return True
            for dx, dy in self.CARDINAL:
                n = T.get(((xy[0] + dx) % W2, xy[1] + dy))
                if n and (n["terrain"] == "Ocean" or n["river"] or n["irrigation"] or n["farmland"]
                          or (n["x"], n["y"]) in cxy):
                    return True
            return False
        target = self.workers.get(au["id"])
        here = (au["x"], au["y"])
        if target and tuple(target) == here:
            t = T.get(here)
            if t and not t["irrigation"] and t["terrain"] in self.IRRIGABLE:
                J(self.s, "campaign", "irrigate", f"{au['type_name']} #{au['id']} {here}: {t['terrain']}, water nearby → "
                  f"Irrigation (+1 food, tile worked by {worked.get(here, 'a city')})", unit_id=au["id"], tile=list(here))
                self.s.press("I")
                self.workers.pop(au["id"], None)
                self.bad_tiles.add(here)          # not again even if the order did not take
                return True
            self.workers.pop(au["id"], None)
        taken = {tuple(v) for v in self.workers.values()}
        cands = [xy for xy in worked if xy not in taken and xy not in self.bad_tiles and xy not in cxy
                 and (T.get(xy) or {}).get("terrain") in self.IRRIGABLE and not T[xy]["irrigation"]
                 and not T[xy]["mine"] and watered(xy)]
        if not cands:
            return False
        cands.sort(key=lambda xy: (self._dist(here, xy, W2), self.IRRIGABLE[T[xy]["terrain"]]))
        for xy in cands[:6]:
            if xy == here:
                self.workers[au["id"]] = list(xy)
                return self._terraform(au, st)
            r = self.r.add(au["id"], *xy)
            if r.get("ok"):
                self.workers[au["id"]] = list(xy)
                J(self.s, "campaign", "go irrigate", f"{au['type_name']} #{au['id']} → {xy} ({T[xy]['terrain']}, "
                  f"worked by {worked[xy]}, not irrigated, water nearby), {r['plan']['turns']} turns",
                  unit_id=au["id"], tile=list(xy))
                return True
            self.bad_tiles.add(xy)
        return False

    def _road_work(self, au, st) -> bool:
        """Road / railroad network between our cities (roadnet.plan, a minimum spanning tree),
        built by at most `road_max` Engineers that come free; the rest keep irrigating.
        On the tile: R (road; on a road with Railroad known the same order lays railroad)."""
        from . import roadnet
        if au["type_name"] != "Engineers":
            return False
        W2 = st["map"]["width2"]
        turn = st["turn"]
        if self._tiles is None or self._tiles[0] != turn or time.time() - self._tiles[1] > 60:
            ms = st["map"]
            self._tiles = (turn, time.time(), self.g.read_tiles(0, 0, ms["width2"], ms["height"]))
        T = {(t["x"], t["y"]): t for t in self._tiles[2]}
        me = st["human_civ"]
        rail = bool(self.rail_known())
        if self.road_plan is None or self.road_plan[0] != turn:
            mine = [c for c in st["cities"] if c["owner"] == me]
            net = roadnet.plan(self._tiles[2], mine, W2, start=set(self.wonder_cities()))
            self.road_plan = (turn, net)
            todo = [p for p in net["tiles"] if not T[p]["road"] or (rail and not T[p]["railroad"])]
            self.road_status = {"tiles": len(net["tiles"]), "links": len(net["edges"]), "todo": len(todo),
                                "road": sum(1 for p in net["tiles"] if T[p]["road"]),
                                "railroad": sum(1 for p in net["tiles"] if T[p]["railroad"]), "rail_known": rail}
        net = self.road_plan[1]

        def need(p):
            t = T.get(p)
            return bool(t) and (not t["road"] or (rail and not t["railroad"]))
        live = ("pending", "running", "waiting_next_turn", "paused_popup")
        # tiles somebody is working on (a road takes a few turns): keep them out of the queue
        self.road_busy = {p: v for p, v in self.road_busy.items() if turn - v[1] <= 6 and need(p)}
        self.road_jobs = {k: v for k, v in self.road_jobs.items()
                          if (self.r.tasks.get(k) or {}).get("status") in live or k == au["id"]}
        here = (au["x"], au["y"])
        tgt = self.road_jobs.pop(au["id"], None)
        if tgt and tuple(tgt) == here and need(here):
            t = T[here]
            what = "Railroad" if t["road"] else "Road"
            J(self.s, "campaign", "build road", f"Engineer #{au['id']} {here}: {what} (network linking the cities, "
              f"{t['terrain']}{' + river' if t['river'] else ''})", unit_id=au["id"], tile=list(here))
            self.s.press("R")
            self.road_busy[here] = (au["id"], turn)
            return True
        if len({k for k in self.road_jobs}) + len({v[0] for v in self.road_busy.values()}) >= self.road_max:
            return False                       # enough Engineers on roads: this one irrigates
        queue = [p for p in net["tiles"][:400] if need(p) and p not in self.road_busy
                 and p not in {tuple(v) for v in self.road_jobs.values()}]
        if not queue:
            return False
        queue.sort(key=lambda p: self._dist(here, p, W2))
        for p in queue[:5]:
            if p == here:
                self.road_jobs[au["id"]] = list(p)
                return self._road_work(au, st)
            r = self.r.add(au["id"], *p)
            if r.get("ok"):
                self.road_jobs[au["id"]] = list(p)
                J(self.s, "campaign", "go build road", f"Engineer #{au['id']} → {p} on the city-linking road network "
                  f"({r['plan']['turns']} turns); up to {self.road_max} Engineers on roads, the rest irrigate",
                  unit_id=au["id"], tile=list(p))
                return True
        return False

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
            if "select unit to activate" in (p.get("title") or "").lower() and p.get("options"):
                J(self.s, "campaign", "answer popup", "Select Unit To Activate: several units on one tile → picked the "
                  "first (the runner selects tasked units itself afterwards)")
                self.s.answer_dialog(option=0, button="ok")
                return None
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
        turn = st["turn"]
        for u in hunters:
            k = self._key(u)
            if k in self.assign or self.no_hut.get(k) == turn:
                continue                 # no reachable village for this unit this turn: do not re-plan every tick
            best = None
            for h in sorted(huts - taken, key=lambda h: abs(h[0] - u["x"]) + abs(h[1] - u["y"]))[:8]:
                pl = self.r.plan(u["id"], *h, st=st)
                if pl.get("ok") and pl["turns"] <= self.max_turns_to_hut and (best is None or pl["turns"] < best[0]):
                    best = (pl["turns"], h)
            if not best:
                self.no_hut[k] = turn
            if best:
                self.assign[k] = best[1]
                taken.add(best[1])
                self._note(f"{u['type_name']} #{u['id']} → village {best[1]} ({best[0]} turns)")
                J(self.s, "campaign", "assign village",
                  f"{u['type_name']} #{u['id']} ({u['x']},{u['y']}): took the 8 nearest villages not claimed by another hunter, "
                  f"ran the pathfinder on each and picked the one with the fewest turns ({best[0]} turns ≤ {self.max_turns_to_hut})",
                  unit_id=u["id"], village=list(best[1]))
        if self.explore:
            self._explore(st, hunters, W2)
        if not self.assign and not any(t["status"] in ("pending", "running", "waiting_next_turn", "paused_popup")
                                       for t in self.r.tasks.values()):
            self._set("stopped", f"no reachable village and no unexplored area left ({have}/{self.target})")
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
            hut_goal, near = self.goal, None
            if self.goal == "city":
                near = min(((max(abs(c["y"] - hut[1]), (min(abs(c["x"] - hut[0]), W2 - abs(c["x"] - hut[0]))
                                                        + abs(c["y"] - hut[1])) // 2), c["name"])
                            for c in st["cities"]), default=None)
                if near and near[0] <= self.near_city:
                    hut_goal = "tech"
                    J(self.s, "campaign", "village goal tech",
                      f"village {tuple(hut)} is {near[0]} tiles from {near[1]} — villages next to a city never "
                      f"become cities (≤{self.near_city}), so goal: a technology; load if none",
                      village=list(hut), city=near[1], dist=near[0])
            h = HutHunt(self.s, self.r, self.saves_dir, au["id"], hut, self.min_moves, self.max_attempts,
                        goal=hut_goal, keep_last=self.keep_last)
            self.hunts.append(h.start())
            h._thread.join()
            res = h.info()
            if res["status"] == "success" and hut_goal == "tech":
                r0 = res["result"]
                self.techs_gained.append({"village": list(hut), "techs": r0.get("techs"), "attempts": res["attempts"]})
                self._note(f"gained: +{r0.get('techs')} tech ({res['attempts']} attempts)")
            elif res["status"] == "success":
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
            if st2.get("active_unit") == au["index"] and not st2.get("popup") and au["type_name"] in self.WORKERS \
                    and self.terraform:
                if self._terraform(au, st2):
                    return None
            if st2.get("active_unit") == au["index"] and not st2.get("popup") and self.sleep_idle:
                # player's rule "ajilgui unit caravanuudiig oirhon hotod oruulj untuul"
                mine = [c for c in st2["cities"] if c["owner"] == me]
                here = next((c for c in mine if (c["x"], c["y"]) == (au["x"], au["y"])), None)
                if here:
                    J(self.s, "campaign", "sleep unit", f"{au['type_name']} #{au['id']}: idle in {here['name']} "
                      "→ Sentry (sleeps; not selected every turn, woken when given a task)",
                      unit_id=au["id"], city=here["name"])
                    self.s.press("S")
                    return None
                if mine and self.homing.get(au["id"]) != st2["turn"]:
                    W2 = st2["map"]["width2"]
                    c = min(mine, key=lambda c: self._dist((au["x"], au["y"]), (c["x"], c["y"]), W2))
                    r = self.r.add(au["id"], c["x"], c["y"])
                    self.homing[au["id"]] = st2["turn"]
                    if r.get("ok"):
                        J(self.s, "campaign", "go to city", f"{au['type_name']} #{au['id']}: idle → heading to the nearest city, "
                          f"{c['name']} ({r['plan']['turns']} turns), will sleep there", unit_id=au["id"],
                          city=c["name"])
                        return None
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
