"""
City site finder.

Score of a site = the land a city there would work:
  * 21-tile city radius (verified against Rome's CityRadii bytes in memory)
  * tile yields from RULES.TXT @TERRAIN (base rows 0-10, specials 11-32)
  * special resources and grassland shields by Civ2's map-seed formulas
    (same source as the hut formula; specials matched the Pheasants on screen)
  * river: +1 trade;  Despotism: -1 on any yield above 2
  * city centre: gets the irrigation bonus where irrigation is allowed, min 1 shield
Rules: centre on land, not on a village; the 21 tiles must not overlap any existing
city's radius nor each other; reachable by the settler within `max_turns`.
"""
from __future__ import annotations

from itertools import product

TERRAIN = ["Desert", "Plains", "Grassland", "Forest", "Hills", "Mountains",
           "Tundra", "Glacier", "Swamp", "Jungle", "Ocean"]


def radius_offsets() -> list[tuple[int, int]]:
    out = []
    for da, db in product(range(-2, 3), repeat=2):
        if abs(da) == 2 and abs(db) == 2:
            continue
        out.append((da - db, da + db))
    return out


RADIUS = radius_offsets()


def special_at(x: int, y: int, seed: int) -> int:
    """-1 = none, 0/1 = which of the two specials of the terrain (Civ2-clone Tile.cs)."""
    a = (x + y) >> 1
    b = x - a
    c = 13 * (b >> 2) + 11 * ((x + y) >> 3) + seed
    if (a & 3) + 4 * (b & 3) != (c & 15):
        return -1
    d = 1 << ((seed >> 4) & 3)
    return 1 if (d & a) == (d & b) else 0


def grassland_shield(x: int, y: int) -> bool:
    r4 = (y // 2 + 2 * (y % 2)) % 4
    r3 = 8 - 2 * (r4 % 4)
    return (x - (y % 2) + r3) % 8 < 4


def parse_terrain_rows(rules_lines: list[str]) -> list[dict]:
    rows = []
    for l in rules_lines:
        f = [p.strip() for p in l.split(";")[0].split(",")]
        try:
            rows.append({"name": f[0], "food": int(f[3]), "shield": int(f[4]), "trade": int(f[5]),
                         "irrigate": f[6].lower() == "yes" if len(f) > 6 else False,
                         "irr_bonus": int(f[7]) if len(f) > 7 and f[7].lstrip("-").isdigit() else 0})
        except (IndexError, ValueError):
            rows.append({"name": f[0] if f else "?", "food": 0, "shield": 0, "trade": 0,
                         "irrigate": False, "irr_bonus": 0})
    return rows


class SiteFinder:
    def __init__(self, tiles: list[dict], width2: int, height: int, cities: list[dict],
                 terrain_rows: list[dict], seed: int, flat: bool = False, despotism: bool = True):
        self.T = {(t["x"], t["y"]): t for t in tiles}
        self.W2, self.H, self.flat = width2, height, flat
        self.rows, self.seed, self.desp = terrain_rows, seed, despotism
        self.taken: set = set()
        for c in cities:
            self.taken |= set(self.area((c["x"], c["y"])))
        for t in tiles:                       # tiles the game marks as inside some city's radius
            if t.get("city_radius_civ"):
                self.taken.add((t["x"], t["y"]))

    def norm(self, x, y):
        return ((x % self.W2) if not self.flat else x, y)

    def area(self, c) -> list[tuple]:
        out = []
        for dx, dy in RADIUS:
            x, y = self.norm(c[0] + dx, c[1] + dy)
            if 0 <= y < self.H:
                out.append((x, y))
        return out

    def tile_yield(self, xy, centre=False) -> dict:
        t = self.T.get(xy)
        if t is None:
            return {"food": 0, "shield": 0, "trade": 0}
        ti = TERRAIN.index(t["terrain"]) if t["terrain"] in TERRAIN else 2
        sp = -1 if t.get("resource_suppressed") else special_at(xy[0], xy[1], self.seed)
        row = self.rows[ti + 11 * (1 + sp)] if sp >= 0 and len(self.rows) > ti + 22 else self.rows[ti]
        base = self.rows[ti]
        f, s, tr = row["food"], row["shield"], row["trade"]
        if t["terrain"] == "Grassland" and sp < 0 and grassland_shield(*xy):
            s += 1
        if t.get("river"):
            tr += 1
        if t.get("road") and t["terrain"] in ("Desert", "Grassland", "Plains"):
            tr += 1
        if centre:
            if base["irrigate"]:
                f += base["irr_bonus"]
            s = max(s, 1)
        if self.desp:
            f, s, tr = (v - 1 if v > 2 else v for v in (f, s, tr))
        return {"food": f, "shield": s, "trade": tr, "special": sp >= 0}

    def score(self, c) -> dict | None:
        t = self.T.get(c)
        if t is None or t["terrain"] == "Ocean" or t.get("hut") or t.get("city"):
            return None
        area = self.area(c)
        if any(a in self.taken for a in area):
            return None
        vals, ocean, specials = [], 0, 0
        for a in area:
            if a == c:
                continue
            y = self.tile_yield(a)
            ocean += self.T.get(a, {}).get("terrain") == "Ocean"
            specials += bool(y.get("special"))
            vals.append(2.0 * y["food"] + 1.5 * y["shield"] + 1.0 * y["trade"])
        vals.sort(reverse=True)
        work = sum(vals[:6]) + 0.5 * sum(vals[6:12]) + 0.2 * sum(vals[12:])
        cy = self.tile_yield(c, centre=True)
        centre = 2.0 * cy["food"] + 1.5 * cy["shield"] + 1.0 * cy["trade"]
        bonus = (2.0 if t.get("river") else 0) + (1.0 if t["terrain"] == "Hills" else 0) \
            + (1.0 if 0 < ocean else 0) - max(0, ocean - 8) * 1.5
        return {"x": c[0], "y": c[1], "score": round(work + 1.5 * centre + bonus, 1),
                "centre": t["terrain"] + (" + river" if t.get("river") else ""), "centre_yield": cy,
                "specials": specials, "ocean": ocean, "area": area}


def choose(finder: SiteFinder, options: dict[int, list[dict]]) -> list[dict]:
    """Pick one site per settler, radii pairwise disjoint, maximising total value
    (score minus 1.5 per turn of travel). options: unit_id -> candidate list."""
    units = list(options)
    best: tuple = (float("-inf"), [])

    def val(c):
        return c["score"] - 1.5 * c["turns"]

    def rec(i, used: set, picked: list, total: float):
        nonlocal best
        if i == len(units):
            if total > best[0]:
                best = (total, list(picked))
            return
        any_ok = False
        for c in options[units[i]][:25]:
            a = set(c["area"])
            if a & used:
                continue
            any_ok = True
            picked.append({**c, "unit_id": units[i]})
            rec(i + 1, used | a, picked, total + val(c))
            picked.pop()
        if not any_ok:
            rec(i + 1, used, picked, total)
    rec(0, set(), [], 0.0)
    return best[1]
