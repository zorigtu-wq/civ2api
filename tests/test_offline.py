"""
Offline test: builds a fake CIV2.EXE memory image with known units/cities/popup,
then checks that the reader decodes them. Runs on any OS (no Win32 calls).

    python -m unittest tests/test_offline.py
"""
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path

# ---- stub out the Windows-only module before importing the package
fake_win32 = types.ModuleType("civ2api.win32")
fake_win32.ProcessReader = object
fake_win32.find_pids = lambda *a: []
sys.modules["civ2api.win32"] = fake_win32
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from civ2api import events as E          # noqa: E402
from civ2api import memmap as M          # noqa: E402
from civ2api.rules import Rules          # noqa: E402
from civ2api.state import Civ2           # noqa: E402

RULES = """; test
@CIVILIZE
Advanced Flight, AdvFlight ...
Alphabet, ...

@UNITS
Settlers,      0,1.,1, 0,1,1,1,0,30, 1,5,  nil, 000000000000000
Engineers,     0,2.,1, 0,2,2,1,0,40, 1,5,  Explos, 000000000000000
Warriors,      0,1.,1, 1,1,1,1,0,10, 0,0,  nil, 000000000000000

@IMPROVE
Nothing, 0, 0, nil
Palace, 10, 0, Mas
Barracks, 4, 1, nil

@LEADERS
Caesar,Livia,0,1,1,Romans,Roman,0,0,0
Hammurabi,Ishtar,0,1,1,Babylonians,Babylonian,0,0,0
"""


class FakeMem:
    def __init__(self):
        self.mem = {}
        self.pid = 1234

    def put(self, addr, data):
        for i, b in enumerate(data):
            self.mem[addr + i] = b

    def read(self, addr, size):
        try:
            return bytes(self.mem.get(addr + i, 0) for i in range(size))
        except Exception:
            raise OSError("bad read")

    def u8(self, a): return self.read(a, 1)[0]
    def i32(self, a): return struct.unpack("<i", self.read(a, 4))[0]
    def i16(self, a): return struct.unpack("<h", self.read(a, 2))[0]
    def u16(self, a): return struct.unpack("<H", self.read(a, 2))[0]
    def u32(self, a): return struct.unpack("<I", self.read(a, 4))[0]

    def cstr(self, a, maxlen=64):
        return self.read(a, maxlen).split(b"\0", 1)[0].decode("cp1252")

    def close(self): pass


def build_memory():
    m = FakeMem()
    m.put(M.COSMIC, bytes([3]))
    # unit types: moves pre-multiplied (3,6,3), hp 10,20,10
    for i, (mv, hp) in enumerate([(3, 10), (6, 20), (3, 10)]):
        o = M.UNIT_TYPES + i * M.UNIT_TYPE_SIZE
        m.put(o + M.UT_MOVE, bytes([mv]))
        m.put(o + M.UT_HP, bytes([hp]))
        m.put(o + M.UT_ATT, bytes([i]))
    # game
    g = M.GAME
    m.put(g + M.G_TURN, struct.pack("<h", 42))
    m.put(g + M.G_ACTIVE_UNIT, struct.pack("<h", 1))
    m.put(g + M.G_HUMAN_CIV, bytes([1]))
    m.put(g + M.G_ACTIVE_PLAYERS, bytes([0b111]))
    m.put(g + M.G_TOTAL_UNITS, struct.pack("<h", 3))
    m.put(g + M.G_TOTAL_CITIES, struct.pack("<H", 1))
    m.put(M.HUMAN_CIV_INDEX, struct.pack("<i", 1))
    m.put(M.UNIT_SELECTED, struct.pack("<I", 1))

    def unit(i, uid, typ, owner, x, y, spent=0, dmg=0, vet=False, orders=-1):
        o = M.UNITS + i * M.UNIT_SIZE
        m.put(o + M.U_X, struct.pack("<HH", x, y))
        m.put(o + M.U_ATTR, struct.pack("<H", M.ATTR_VETERAN if vet else 0))
        m.put(o + M.U_TYPE, bytes([typ, owner & 0xFF, spent & 0xFF]))
        m.put(o + M.U_HP_LOST, bytes([dmg]))
        m.put(o + M.U_ORDERS, struct.pack("<bB", orders, 0xFF))
        m.put(o + M.U_ID, struct.pack("<i", uid))
    unit(0, 101, 0, 1, 10, 20)
    unit(1, 102, 2, 1, 12, 20, spent=1, dmg=3, vet=True)
    unit(2, 103, 2, 2, 30, 30, orders=2)
    # city
    c = M.CITIES
    m.put(c + M.C_X, struct.pack("<hh", 10, 20))
    m.put(c + M.C_OWNER, bytes([1, 3]))
    m.put(c + M.C_NAME, b"Roma\0")
    m.put(c + M.C_BUILDING, struct.pack("<b", -2))
    m.put(c + M.C_ID, struct.pack("<i", 7))
    # civ 1
    m.put(M.CIVS + M.CIV_SIZE + M.CV_GOLD, struct.pack("<i", 250))
    m.put(M.CIVS + M.CIV_SIZE + M.CV_LEADER, struct.pack("<H", 0))
    m.put(M.CIVS + M.CIV_SIZE + M.CV_GOVERNMENT, bytes([2]))
    m.put(M.CIVS + M.CIV_SIZE + M.CV_RESEARCHING, struct.pack("<h", 1))
    # map 4x2 (width2=8)
    m.put(M.MAP_HEADER, struct.pack("<hhh", 8, 2, 8))
    m.put(M.MAP_DATA_PTR, struct.pack("<I", 0x01000000))
    m.put(0x01000000 + 6 * 1, bytes([0x82, 0x11, 0, 0, 0b10, 0]))  # tile (x=2,y=0)
    # popup with title, two lines, two options, one button
    P = 0x02000000
    m.put(M.CURR_POPUP, struct.pack("<I", P))
    m.put(P + M.D_TITLE, struct.pack("<I", 0x02100000))
    m.put(0x02100000, b"Barbarians!\0")
    m.put(P + M.D_FIRST_TEXT_LINE, struct.pack("<I", 0x02200000))
    m.put(0x02200000 + 8, struct.pack("<I", 0x02300000))
    m.put(0x02200000 + 0x1C, struct.pack("<I", 0x02200100))
    m.put(0x02300000, b"Line one\0")
    m.put(0x02200100 + 8, struct.pack("<I", 0x02300100))
    m.put(0x02300100, b"Line two\0")
    m.put(P + M.D_FIRST_LIST_ITEM, struct.pack("<I", 0x02400000))
    m.put(0x02400000 + 8, struct.pack("<I", 0x02500000))
    m.put(0x02400000 + 0x10, struct.pack("<I", 0x02400100))
    m.put(0x02500000, b"Pay tribute\0")
    m.put(0x02400100 + 8, struct.pack("<I", 0x02500100))
    m.put(0x02500100, b"Fight\0")
    m.put(P + M.D_NUM_BUTTONS, struct.pack("<i", 1))
    m.put(P + M.D_BUTTON_TEXTS, struct.pack("<I", 0x02600000))
    m.put(0x02600000, b"OK\0")
    m.put(P + M.D_SELECTED_ITEM, struct.pack("<i", 0))
    return m


class T(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        (d / "RULES.TXT").write_text(RULES, encoding="cp1252")
        self.rules = Rules.load(d)
        self.m = build_memory()
        self.g = Civ2(self.m, self.rules, M.UNITS, [])

    def test_rules(self):
        self.assertEqual(self.rules.units, ["Settlers", "Engineers", "Warriors"])
        self.assertEqual(self.rules.improvements[2], "Barracks")
        self.assertEqual(self.rules.advances, ["Advanced Flight", "Alphabet"])
        self.assertEqual(self.rules.tribe(1)["plural"], "Babylonians")

    def test_scales(self):
        self.assertTrue(self.g.move_scaled)
        self.assertTrue(self.g.hp_scaled)

    def test_state(self):
        s = self.g.read_state()
        self.assertEqual(s["turn"], 42)
        self.assertEqual(len(s["units"]), 3)
        u = s["units"][1]
        self.assertEqual((u["id"], u["type_name"], u["x"], u["y"]), (102, "Warriors", 12, 20))
        self.assertEqual((u["hp"], u["hp_max"], u["move_points_left"]), (7, 10, 2))
        self.assertTrue(u["veteran"])
        self.assertEqual(s["units"][2]["orders"], "fortified")
        self.assertEqual(s["active_unit"], 1)
        c = s["cities"][0]
        self.assertEqual((c["name"], c["owner"], c["size"]), ("Roma", 1, 3))
        self.assertEqual(c["producing"]["name"], "Barracks")
        civ1 = s["civs"][1]
        self.assertEqual((civ1["gold"], civ1["government"], civ1["name"]), (250, "Monarchy", "Romans"))
        self.assertEqual(civ1["researching"], "Alphabet")
        p = s["popup"]
        self.assertEqual(p["title"], "Barbarians!")
        self.assertEqual(p["lines"], ["Line one", "Line two"])
        self.assertEqual([o["text"] for o in p["options"]], ["Pay tribute", "Fight"])
        self.assertEqual(p["buttons"][0]["text"], "OK")

    def test_tiles(self):
        t = self.g.read_tiles(0, 0, 8, 2)
        self.assertEqual(len(t), 8)
        t2 = next(x for x in t if x["x"] == 2 and x["y"] == 0)
        self.assertEqual(t2["terrain"], "Grassland")
        self.assertTrue(t2["river"] and t2["unit"] and t2["road"])
        self.assertEqual(t2["explored_mask"], 2)

    def test_huts(self):
        from civ2api.state import hut_pattern
        seed = 0
        # find a hut-pattern tile in the fixture map and make it unowned grassland
        cand = [(x, y) for y in range(2) for x in range(8) if (x + y) % 2 == 0 and hut_pattern(x, y, seed)]
        tiles = {(t["x"], t["y"]): t for t in self.g.read_tiles(0, 0, 8, 2)}
        for (x, y), t in tiles.items():
            self.assertEqual(t["hut"], False if t["owner_raw"] >> 4 != 0xF else (x, y) in cand)
        if cand:
            x, y = cand[0]; idx = y * 4 + x // 2
            self.m.put(0x01000000 + 6 * idx, bytes([2, 0, 0, 0, 0, 0xF0]))
            self.assertTrue(next(t for t in self.g.read_tiles(0, 0, 8, 2) if (t["x"], t["y"]) == (x, y))["hut"])
            self.m.put(0x01000000 + 6 * idx + 5, bytes([0x50]))   # entered by civ 5 -> hut gone
            self.assertFalse(next(t for t in self.g.read_tiles(0, 0, 8, 2) if (t["x"], t["y"]) == (x, y))["hut"])

    def test_diff(self):
        a = self.g.read_state()
        # enemy appears, my warrior moves and gets hurt, city captured
        self.m.put(M.UNITS + 3 * M.UNIT_SIZE + M.U_ID, struct.pack("<i", 104))
        self.m.put(M.GAME + M.G_TOTAL_UNITS, struct.pack("<h", 4))
        self.m.put(M.UNITS + 3 * M.UNIT_SIZE + M.U_TYPE, bytes([2, 3]))
        self.m.put(M.UNITS + 1 * M.UNIT_SIZE + M.U_X, struct.pack("<H", 13))
        self.m.put(M.UNITS + 1 * M.UNIT_SIZE + M.U_HP_LOST, bytes([5]))
        self.m.put(M.CITIES + M.C_OWNER, bytes([3]))
        b = self.g.read_state()
        kinds = sorted(e["kind"] for e in E.diff(a, b))
        self.assertEqual(kinds, ["city_lost", "foreign_unit_appeared", "unit_damaged", "unit_moved"])


if __name__ == "__main__":
    unittest.main()
