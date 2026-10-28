"""
Turn-aware shortest path for one unit on the Civ2 diamond map.

Costs are in "move points" (MP) = moves x road multiplier (3 in MGE), i.e. thirds.
  * terrain: RULES.TXT @TERRAIN move cost x 3
  * road -> road (a city counts as road): 1           (1/3 move)
  * railroad -> railroad:                 0
  * river: no movement bonus in MGE (measured 2026-10-27: river->river step cost the
    full terrain cost, 3 MP). RIVER_AS_ROAD can switch the bonus on for mods.
  * Alpine ability: every tile costs 1
Civ2 rules modelled:
  * a unit with full MP can always enter a land tile, whatever it costs
  * a unit with partial MP that is lower than the cost waits for the next turn
    (Civ2 may let it try with a probability; we plan conservatively)
  * ground units: no ocean; sea units: ocean or own city only
  * zones of control: a ground unit cannot go from a tile next to a foreign unit
    to another tile next to a foreign unit (unless the target holds an own unit/city)
  * tiles with foreign units or foreign cities are blocked (no attacks in a goto)

The planner minimises (turns, MP spent in the last turn) with a label-setting
Dijkstra over tiles, which is exact for this cost model.
"""
from __future__ import annotations

import heapq

DIRS = {"N": (0, -2), "NE": (1, -1), "E": (2, 0), "SE": (1, 1),
        "S": (0, 2), "SW": (-1, 1), "W": (-2, 0), "NW": (-1, -1)}
OCEAN = "Ocean"
RIVER_AS_ROAD = False
ABIL_IGNORE_ZOC = 0x0002
ABIL_ALPINE = 0x0200
DEFAULT_TERRAIN_MOVE = {"Desert": 1, "Plains": 1, "Grassland": 1, "Forest": 2, "Hills": 2,
                        "Mountains": 3, "Tundra": 1, "Glacier": 2, "Swamp": 2, "Jungle": 2,
                        "Ocean": 1}
TERRAIN_ORDER = ["Desert", "Plains", "Grassland", "Forest", "Hills", "Mountains",
                 "Tundra", "Glacier", "Swamp", "Jungle", "Ocean"]


class Planner:
    def __init__(self, tiles: list[dict], width2: int, height: int, units: list[dict],
                 cities: list[dict], me: int, road_mult: int = 3, flat: bool = False,
                 terrain_move: list[int] | None = None, river_as_road: bool = RIVER_AS_ROAD,
                 avoid_huts: bool = True):
        self.T = {(t["x"], t["y"]): t for t in tiles}
        self.W2, self.H, self.flat, self.me, self.rm = width2, height, flat, me, road_mult
        self.river_as_road = river_as_road
        self.huts = {(t["x"], t["y"]) for t in tiles if t.get("hut")} if avoid_huts else set()
        self.tmove = dict(DEFAULT_TERRAIN_MOVE)
        if terrain_move and len(terrain_move) >= 11:
            self.tmove = {n: terrain_move[i] for i, n in enumerate(TERRAIN_ORDER)}
        self.city_at = {(c["x"], c["y"]): c for c in cities}
        self.units_at: dict[tuple, list] = {}
        for u in units:
            self.units_at.setdefault((u["x"], u["y"]), []).append(u)
        foreign = [u for u in units if u["owner"] != me]
        self.zoc: set[tuple] = set()
        for u in foreign:
            if u.get("domain", "ground") == "sea":
                continue
            for dx, dy in DIRS.values():
                self.zoc.add(self.norm(u["x"] + dx, u["y"] + dy))

    # ------------------------------------------------------------------ geometry
    def norm(self, x: int, y: int):
        if not self.flat:
            x %= self.W2
        return (x, y)

    def neighbours(self, x: int, y: int):
        for d, (dx, dy) in DIRS.items():
            nx, ny = self.norm(x + dx, y + dy)
            if 0 <= ny < self.H and 0 <= nx < self.W2 and (nx, ny) in self.T:
                yield d, nx, ny

    # ------------------------------------------------------------------ rules
    def _own(self, xy) -> bool:
        c = self.city_at.get(xy)
        return any(u["owner"] == self.me for u in self.units_at.get(xy, [])) or \
            (c is not None and c["owner"] == self.me)

    def _blocked(self, xy, goal) -> str | None:
        c = self.city_at.get(xy)
        if c is not None and c["owner"] != self.me:
            return "foreign city"
        if any(u["owner"] != self.me for u in self.units_at.get(xy, [])):
            return "foreign unit"
        return None

    def step_cost(self, a, b, unit) -> int | None:
        ta, tb = self.T[a], self.T[b]
        dom = unit.get("domain", "ground")
        if dom == "sea":
            if tb["terrain"] != OCEAN and not (b in self.city_at and self.city_at[b]["owner"] == self.me):
                return None
            return self.rm
        if dom == "air":
            return self.rm
        if tb["terrain"] == OCEAN:
            return None
        if unit.get("abilities", 0) & ABIL_ALPINE:
            return 1
        road_a = ta["road"] or a in self.city_at
        road_b = tb["road"] or b in self.city_at
        if ta.get("railroad") and tb.get("railroad"):
            return 0
        if road_a and road_b:
            return 1
        if self.river_as_road and ta.get("river") and tb.get("river"):
            return 1
        return self.tmove.get(tb["terrain"], 1) * self.rm

    # ------------------------------------------------------------------ search
    def plan(self, unit: dict, goal: tuple, full_mp: int, mp_left: int | None = None,
             max_turns: int = 60) -> dict:
        start = (unit["x"], unit["y"])
        goal = self.norm(*goal)
        if goal not in self.T:
            return {"ok": False, "error": f"goal {goal} is off the map"}
        if start == goal:
            return {"ok": True, "steps": [], "turns": 0, "reached": True}
        why = self._blocked(goal, goal)
        if why:
            return {"ok": False, "error": f"goal blocked: {why}"}
        if unit.get("domain", "ground") == "ground" and self.T[goal]["terrain"] == OCEAN:
            return {"ok": False, "error": "goal is ocean"}
        full = max(full_mp, 1)
        spent0 = full - (full if mp_left is None else max(0, min(mp_left, full)))
        ignore_zoc = bool(unit.get("abilities", 0) & ABIL_IGNORE_ZOC) or unit.get("domain") != "ground"
        best = {start: (0, spent0)}
        prev: dict = {}
        heap = [(0, spent0, start)]
        while heap:
            turn, spent, cur = heapq.heappop(heap)
            if best.get(cur) != (turn, spent):
                continue
            if cur == goal:
                break
            if turn > max_turns:
                break
            for d, nx, ny in self.neighbours(*cur):
                nxt = (nx, ny)
                if nxt != goal and (self._blocked(nxt, goal) or nxt in self.huts):
                    continue
                if not ignore_zoc and cur in self.zoc and nxt in self.zoc and not self._own(nxt):
                    continue
                c = self.step_cost(cur, nxt, unit)
                if c is None:
                    continue
                if spent + c <= full:
                    lab = (turn, spent + c)
                elif spent == 0:
                    lab = (turn, full)                       # full MP: always allowed
                else:
                    lab = (turn + 1, min(c, full))           # wait for next turn
                if nxt not in best or lab < best[nxt]:
                    best[nxt] = lab
                    prev[nxt] = (cur, d, c, lab)
                    heapq.heappush(heap, (lab[0], lab[1], nxt))
        if goal not in best:
            return {"ok": False, "error": "no path (blocked by ocean, units or zones of control)"}
        steps = []
        node = goal
        while node != start:
            p, d, c, lab = prev[node]
            steps.append({"dir": d, "x": node[0], "y": node[1], "cost": c,
                          "turn": lab[0], "mp_after": lab[1],
                          "terrain": self.T[node]["terrain"],
                          "via": self._via(p, node, unit)})
            node = p
        steps.reverse()
        return {"ok": True, "steps": steps, "turns": best[goal][0] + 1,
                "first_turn_steps": sum(1 for s in steps if s["turn"] == 0), "reached": False}

    def reach(self, unit: dict, full_mp: int, mp_left: int | None = None, max_turns: int = 6) -> dict:
        """All tiles the unit can stand on within max_turns: {(x, y): turns_needed}."""
        start = (unit["x"], unit["y"])
        full = max(full_mp, 1)
        spent0 = full - (full if mp_left is None else max(0, min(mp_left, full)))
        ignore_zoc = bool(unit.get("abilities", 0) & ABIL_IGNORE_ZOC) or unit.get("domain") != "ground"
        best = {start: (0, spent0)}
        heap = [(0, spent0, start)]
        while heap:
            turn, spent, cur = heapq.heappop(heap)
            if best.get(cur) != (turn, spent) or turn >= max_turns:
                continue
            for d, nx, ny in self.neighbours(*cur):
                nxt = (nx, ny)
                if self._blocked(nxt, None) or nxt in self.huts:
                    continue
                if not ignore_zoc and cur in self.zoc and nxt in self.zoc and not self._own(nxt):
                    continue
                c = self.step_cost(cur, nxt, unit)
                if c is None:
                    continue
                if spent + c <= full:
                    lab = (turn, spent + c)
                elif spent == 0:
                    lab = (turn, full)
                else:
                    lab = (turn + 1, min(c, full))
                if lab[0] < max_turns and (nxt not in best or lab < best[nxt]):
                    best[nxt] = lab
                    heapq.heappush(heap, (lab[0], lab[1], nxt))
        return {xy: lab[0] + 1 for xy, lab in best.items()}

    def _via(self, a, b, unit) -> str:
        ta, tb = self.T[a], self.T[b]
        if unit.get("domain") != "ground":
            return unit.get("domain", "")
        if ta.get("railroad") and tb.get("railroad"):
            return "railroad"
        if (ta["road"] or a in self.city_at) and (tb["road"] or b in self.city_at):
            return "road"
        if self.river_as_road and ta.get("river") and tb.get("river"):
            return "river"
        return "terrain"
