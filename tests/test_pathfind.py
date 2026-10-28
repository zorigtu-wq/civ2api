"""Pathfinder tests on synthetic maps. python -m unittest tests/test_pathfind.py"""
import sys
import types
import unittest
from pathlib import Path

sys.modules.setdefault("civ2api.win32", types.ModuleType("civ2api.win32"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from civ2api.pathfind import Planner  # noqa: E402

W2, H = 40, 30


def tile(x, y, terrain="Grassland", river=False, road=False, rail=False):
    return {"x": x, "y": y, "terrain": terrain, "river": river, "road": road, "railroad": rail}


def world(fn=lambda x, y: {}):
    return [tile(x, y, **fn(x, y)) for y in range(H) for x in range(W2) if (x + y) % 2 == 0]


def unit(x, y, mp=3, **kw):
    return {"id": 1, "x": x, "y": y, "owner": 1, "domain": "ground", "abilities": 0, **kw}


class T(unittest.TestCase):
    def plan(self, tiles, u, goal, full=3, left=None, units=(), cities=(), flat=True):
        p = Planner(tiles, W2, H, [u, *units], list(cities), me=1, flat=flat)
        return p.plan(u, goal, full, left)

    def test_straight_grassland(self):
        r = self.plan(world(), unit(10, 10), (14, 10))      # 2 tiles east, 1 move/turn
        self.assertTrue(r["ok"])
        self.assertEqual([s["dir"] for s in r["steps"]], ["E", "E"])
        self.assertEqual(r["turns"], 2)

    def test_road_beats_detour(self):
        # road along y=10: 6 tiles east costs 6/3 = 2 moves -> 1 turn for a 2-move unit
        tiles = world(lambda x, y: {"road": y == 10})
        r = self.plan(tiles, unit(10, 10), (22, 10), full=6)
        self.assertEqual(r["turns"], 1)
        self.assertTrue(all(s["via"] == "road" for s in r["steps"]))

    def test_river_used(self):
        tiles = world(lambda x, y: {"river": y == 12, "terrain": "Forest" if y != 12 else "Grassland"})
        p = Planner(tiles, W2, H, [unit(10, 12)], [], me=1, flat=True, river_as_road=True)
        r = p.plan(unit(10, 12), (20, 12), 3)
        self.assertEqual(r["turns"], 2)                       # 5 river steps: 5/3 MP -> 2 turns
        self.assertTrue(all(s["via"] == "river" for s in r["steps"]))

    def test_river_off(self):
        tiles = world(lambda x, y: {"river": y == 12})
        r = Planner(tiles, W2, H, [unit(10, 12)], [], me=1, flat=True).plan(unit(10, 12), (20, 12), 3)
        self.assertEqual(r["turns"], 5)                       # MGE default: no river bonus

    def test_ocean_blocks_ground(self):
        tiles = world(lambda x, y: {"terrain": "Ocean"} if x in (15, 16) else {})
        r = self.plan(tiles, unit(10, 10), (20, 10))
        self.assertFalse(r["ok"])

    def test_mountain_full_mp_rule(self):
        # 1-move unit can always enter a mountain with full MP (one turn per mountain)
        tiles = world(lambda x, y: {"terrain": "Mountains"} if x > 10 else {})
        r = self.plan(tiles, unit(10, 10), (14, 10))
        self.assertEqual(r["turns"], 2)

    def test_partial_mp_waits(self):
        # 2-move unit with 1 MP third left facing a forest (6 MP) must wait
        tiles = world(lambda x, y: {"terrain": "Forest"} if x == 12 else {})
        r = self.plan(tiles, unit(10, 10), (12, 10), full=6, left=1)
        self.assertEqual(r["steps"][0]["turn"], 1)

    def test_foreign_unit_blocks_and_zoc(self):
        enemy = {"id": 9, "x": 13, "y": 9, "owner": 2, "domain": "ground"}
        r = self.plan(world(), unit(10, 10), (16, 10), units=[enemy])
        self.assertTrue(r["ok"])
        path = [(s["x"], s["y"]) for s in r["steps"]]
        self.assertNotIn((13, 9), path)
        # no two consecutive tiles both adjacent to the enemy
        zoc = {(13 + dx, 9 + dy) for dx, dy in [(0, -2), (1, -1), (2, 0), (1, 1), (0, 2), (-1, 1), (-2, 0), (-1, -1)]}
        full = [(10, 10)] + path
        for a, b in zip(full, full[1:]):
            self.assertFalse(a in zoc and b in zoc, (a, b))

    def test_avoids_huts_on_the_way(self):
        tiles = world()
        for t in tiles:
            t["hut"] = (t["x"], t["y"]) == (12, 10)
        r = self.plan(tiles, unit(10, 10), (14, 10))
        self.assertNotIn((12, 10), [(s["x"], s["y"]) for s in r["steps"]])
        r2 = self.plan(tiles, unit(10, 10), (12, 10))          # the hut itself may be the goal
        self.assertEqual([(s["x"], s["y"]) for s in r2["steps"]], [(12, 10)])

    def test_wrap_round_world(self):
        r = self.plan(world(), unit(1, 11), (37, 11), flat=False)  # shorter across the seam
        self.assertEqual(len(r["steps"]), 2)
        self.assertEqual(r["steps"][0]["dir"], "W")


if __name__ == "__main__":
    unittest.main()
