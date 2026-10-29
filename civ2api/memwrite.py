"""
Stage 2 (approved by the player on 2026-10-27): change a few game fields by writing the
game's memory directly, instead of driving the City Window / Tax Rate dialog.

Only these fields are ever written (whitelist), each followed by a read-back check:
  * City+0x39  Building        i8   what the city builds (unit type >= 0, improvement = -id)
  * City+0x30  Workers         u32  bits 0-19 worked tiles (bit 20 = centre), bits 26-31 = specialists
  * City+0x16  Specialists     u32  2 bits per specialist, 1 = entertainer
  * Civ+0x13 / +0x14  science / tax rate (tens of percent); luxury is the rest
  * Game+0x02 option bits Wonder Movies / Throne Room only (a movie froze the game twice)
CIV2.EXE on disk and the game's code are never touched. Every write goes to the decision
protocol (actor "memwrite") with the old and new value.

Notes
  * Changing production this way skips the game's 50% shield penalty for switching between
    unit and improvement, so it is slightly kinder than the UI.
  * The happy/unhappy counts the game keeps (City+0x52/0x53) are refreshed by the game itself
    (turn processing, city screen). After making entertainers we remember the city for the
    rest of the turn instead of trusting the stale counts.
"""
from __future__ import annotations

import json
import struct
import threading
import time

from . import memmap as M
from . import win32
from .journal import note as J

CARAVANS = ("Caravan", "Freight")      # Freight replaces Caravan after The Corporation (Leonardo upgrades them)
MAX_RATE = {"Anarchy": 6, "Despotism": 6, "Tribal": 6, "Monarchy": 7, "Communism": 8,
            "Fundamentalism": 8, "Republic": 8, "Democracy": 9}
CIV_ADVANCES = 0x58          # u8[13] bitset of known advances (SAV layout, verified live:
                             # Alphabet/Ceremonial Burial/Code of Laws/Currency/Monarchy bits)


class MemWriter:
    def __init__(self, session):
        self.s = session
        self.g = session.game
        self.w = win32.ProcessWriter(self.g.p.pid)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ low level
    def _write(self, addr: int, data: bytes, what: str, why: str, old=None, new=None):
        with self._lock:
            self.w.write(addr, data)
            back = self.g.p.read(addr, len(data))
        ok = back == data
        J(self.s, "memwrite", what, why, addr=f"0x{addr:08X}", old=old, new=new, verified=ok)
        if not ok:
            raise RuntimeError(f"read-back mismatch at 0x{addr:08X}")

    def _city_addr(self, city: dict) -> int:
        return M.CITIES + city["index"] * M.CITY_SIZE

    def _civ_addr(self, civ: int) -> int:
        return M.CIVS + civ * M.CIV_SIZE

    # ------------------------------------------------------------------ knowledge
    def known(self, civ: int) -> set[str]:
        bits = self.g.p.read(self._civ_addr(civ) + CIV_ADVANCES, 13)
        codes = getattr(self.g.rules, "advance_codes", [])
        return {codes[i] for i in range(min(len(codes), 104)) if bits[i // 8] >> (i % 8) & 1}

    WONDER_FIRST = 39                 # improvement ids 39.. are Wonders (Pyramids = 39)
    WONDER_CITIES = M.GAME + 0xFE     # TGame.WonderCities i16[28], -1 = not built

    def wonders_built(self) -> dict[int, int]:
        vals = struct.unpack("<28h", self.g.p.read(self.WONDER_CITIES, 56))
        return {self.WONDER_FIRST + k: v for k, v in enumerate(vals) if v != -1}

    def can_build(self, city: dict, name: str, known: set[str]) -> tuple[bool, str]:
        r = self.g.rules
        low = name.lower()
        for i, n in enumerate(r.improvements):
            if n.lower() == low:
                if i == 0:
                    return False, "Nothing"
                if i >= self.WONDER_FIRST and i in self.wonders_built():
                    return False, f"{n}: already built (wonder)"
                if i in city.get("improvements", []):
                    return False, f"{n} already exists"
                pre = (getattr(r, "improve_prereq", []) + [""] * 100)[i]
                if pre and pre.lower() not in ("nil", "no") and pre not in known:
                    return False, f"{n}: requires {pre}"
                if pre.lower() == "no":
                    return False, f"{n}: cannot be built under these rules"
                return True, f"{n}: available"
        for t, n in enumerate(r.units):
            if n.lower() == low:
                pre = (getattr(r, "unit_prereq", []) + [""] * 100)[t]
                until = (getattr(r, "unit_until", []) + [""] * 100)[t]
                if pre and pre.lower() not in ("nil",) and pre not in known:
                    return False, f"{n}: requires {pre}"
                if pre.lower() == "no":
                    return False, f"{n}: cannot be built under these rules"
                if until and until.lower() not in ("nil", "no") and until in known:
                    return False, f"{n}: obsolete ({until})"
                return True, f"{n}: available"
        return False, f"{name}: not in RULES.TXT"

    def _code(self, name: str) -> int:
        r = self.g.rules
        for i, n in enumerate(r.improvements):
            if n.lower() == name.lower():
                return -i
        for t, n in enumerate(r.units):
            if n.lower() == name.lower():
                return t
        raise ValueError(name)

    # ------------------------------------------------------------------ actions
    def set_production(self, city: dict, names: list[str], known: set[str] | None = None,
                       policy: str = "") -> dict:
        known = known if known is not None else self.known(city["owner"])
        reasons = []
        pick = None
        for n in names:
            ok, why = self.can_build(city, n, known)
            reasons.append(why)
            if ok:
                pick = n
                break
        if pick is None:
            return {"ok": False, "city": city["name"], "error": "; ".join(reasons)}
        if city["producing"]["name"].lower() == pick.lower():
            return {"ok": True, "city": city["name"], "producing": pick, "changed": False}
        code = self._code(pick)
        self._write(self._city_addr(city) + M.C_BUILDING, struct.pack("<b", code), "set production",
                    f"{city['name']}: {city['producing']['name']} → {pick}. {policy} Checks: {'; '.join(reasons)}",
                    old=city["producing"]["name"], new=pick)
        return {"ok": True, "city": city["name"], "producing": pick, "changed": True}

    def make_entertainers(self, city: dict, n: int, why: str) -> dict:
        """Take `n` workers off their tiles (outer ring first) and make them entertainers."""
        base = self._city_addr(city)
        raw = self.g.p.read(base, M.CITY_SIZE)
        workers = struct.unpack_from("<I", raw, M.C_WORKED)[0]
        spec = struct.unpack_from("<I", raw, M.C_SPECIALISTS)[0]
        tiles = [i for i in range(20) if workers >> i & 1]          # bit 20 = centre, keep it
        take = sorted(tiles, reverse=True)[:max(0, n)]
        if not take:
            return {"ok": False, "city": city["name"], "error": "no working citizens"}
        count = (workers >> 26) & 0x3F
        new_workers = workers
        for i in take:
            new_workers &= ~(1 << i)
        new_count = count + len(take)
        new_workers = (new_workers & ~(0x3F << 26)) | (new_count << 26)
        new_spec = spec
        for k in range(count, new_count):
            new_spec = (new_spec & ~(3 << (2 * k))) | (1 << (2 * k))      # 1 = entertainer
        self._write(base + M.C_SPECIALISTS, struct.pack("<I", new_spec), "specialists",
                    f"{city['name']}: {why}", old=hex(spec), new=hex(new_spec))
        self._write(base + M.C_WORKED, struct.pack("<I", new_workers), "worker → entertainer",
                    f"{city['name']}: {len(take)} worker(s) on tile {take} made entertainers. {why}",
                    old=hex(workers), new=hex(new_workers))
        return {"ok": True, "city": city["name"], "entertainers_added": len(take), "tiles": take}

    def entertainer_to_worker(self, city: dict, cities: list[dict], finder, W2: int, why: str) -> dict:
        """Put one entertainer back to work on the best free tile (most food, then shields).
        A tile is free if no city (ours or foreign) works it."""
        base = self._city_addr(city)
        raw = self.g.p.read(base, M.CITY_SIZE)
        workers = struct.unpack_from("<I", raw, M.C_WORKED)[0]
        spec = struct.unpack_from("<I", raw, M.C_SPECIALISTS)[0]
        count = (workers >> 26) & 0x3F
        if count == 0 or (spec >> (2 * (count - 1))) & 3 != 1:
            return {"ok": False, "city": city["name"], "error": "no entertainers"}
        def xy(c, i):
            dx, dy = M.CITY_TILES[i]
            return ((c["x"] + dx) % W2, c["y"] + dy)
        used = {xy(c, i) for c in cities for i in c.get("worked_tiles", [])}
        best = None
        for i in range(20):
            if workers >> i & 1:
                continue
            t = xy(city, i)
            if t in used or finder.T.get(t) is None:
                continue
            y = finder.tile_yield(t)
            key = (y["food"], y["shield"], y["trade"])
            if best is None or key > best[0]:
                best = (key, i, t)
        if best is None or best[0][0] <= 0:
            return {"ok": False, "city": city["name"], "error": "no free tile with food"}
        _, i, t = best
        new_workers = (workers | (1 << i)) & ~(0x3F << 26) | ((count - 1) << 26)
        new_spec = spec & ~(3 << (2 * (count - 1)))
        self._write(base + M.C_WORKED, struct.pack("<I", new_workers), "entertainer → worker",
                    f"{city['name']}: {why}. Free tile {i} {t}: food {best[0][0]}, shields {best[0][1]}, "
                    f"trade {best[0][2]} (not worked by another city, most food)",
                    old=hex(workers), new=hex(new_workers))
        self._write(base + M.C_SPECIALISTS, struct.pack("<I", new_spec), "specialists",
                    f"{city['name']}: one entertainer fewer", old=hex(spec), new=hex(new_spec))
        return {"ok": True, "city": city["name"], "tile": i, "xy": t, "yield": best[0]}

    def rename_city(self, city: dict, name: str, why: str) -> dict:
        """City+0x20 char[16]: the player asked for unique names ("adil nertei tsegtsel")."""
        raw = name.encode("latin-1", "replace")[:15]
        data = raw + bytes(16 - len(raw))
        addr = self._city_addr(city) + M.C_NAME
        old = self.g.p.read(addr, 16).split(b"\0")[0].decode("latin-1")
        self._write(addr, data, "rename city", f"{old} → {name}: {why}", old=old, new=name)
        return {"ok": True, "old": old, "new": name}

    # Game+0x02 GraphicAndGameOptions bits (Civ2UIA Civ2Types.pas:983-1004)
    OPTIONS = {"wonder_movies": 0x00010000, "throne_room": 0x00020000}

    def set_option(self, name: str, on: bool, why: str) -> dict:
        bit = self.OPTIONS[name]
        a = M.GAME + 0x02
        old = struct.unpack("<I", self.g.p.read(a, 4))[0]
        new = (old | bit) if on else (old & ~bit)
        if new != old:
            self._write(a, struct.pack("<I", new), f"game option {name}={'on' if on else 'off'}", why,
                        old=hex(old), new=hex(new))
        return {"ok": True, "option": name, "on": bool(new & bit)}

    def set_rates(self, civ: dict, tax: int, lux: int, sci: int, why: str) -> dict:
        if tax + lux + sci != 10:
            return {"ok": False, "error": "tax+lux+sci must be 10"}
        cap = MAX_RATE.get(civ.get("government"), 6)
        if max(tax, lux, sci) > cap:
            return {"ok": False, "error": f"{civ.get('government')}: at most {cap}0% per category"}
        a = self._civ_addr(civ["id"])
        old = (civ["tax_rate"], 10 - civ["tax_rate"] - civ["science_rate"], civ["science_rate"])
        self._write(a + M.CV_SCIENCE_RATE, bytes([sci, tax]), "set tax rates",
                    f"Tax {tax}0% · Lux {lux}0% · Sci {sci}0% ({why})", old=list(old), new=[tax, lux, sci])
        return {"ok": True, "rates": [tax, lux, sci]}


class CityManager:
    """Every few seconds during our turn (memory only, no windows):
      * production: city without `after.built` -> `items`, with it -> `after.items`
        (first buildable of each list, checked against RULES.TXT prerequisites);
      * happiness: unhappy > happy -> (unhappy - happy) workers become entertainers,
        once per city per turn (the game refreshes the counts at turn processing)."""

    def __init__(self, session, writer: MemWriter, items, after=None, happy=True, wonders=None,
                 runner=None):
        self.s, self.mw = session, writer
        self.items, self.after, self.happy = items, after, happy
        # {"cities": [city index, ...], "list": [wonder names in priority order]}
        self.wonders, self.runner = wonders, runner
        self.gov_rates = {"Democracy": (4, 4, 2)}   # rates set once when we switch to that government
        self.starve_done: dict[str, int] = {}      # city key -> turn the starvation guard acted
        self._can_open_wonder_city = False
        self.cap_on = False
        self.buy_wonders = True
        self.gold_reserve = 500
        self.libraries_per_turn = 10
        self.engineers_per_city = True
        self.factory_min_shields = 8
        self.aqueduct_at = 7        # Aqueduct needed above 8 (RULES.TXT @COSMIC); build from 7
        self.sewer_at = 11          # Sewer System needed above 12; build from 11
        self._eng_logged: set = set()
        self.engineers_status: dict = {}
        self.hunt_live = lambda: False
        self.caravan_reserve = 6
        self.caravan_target = None
        self.caravan_balance: dict = {}
        self.alt_items = ["Library", "MarketPlace", "Factory", "Granary", "Courthouse"]
        self.spare_caravans = 0
        self.wonder_need: dict = {}
        self._finder = None
        self.wonder_plan: dict = {}
        self.fed: dict = {}
        self.feed = True
        self.checked_turn = None
        self.gold_prev = None
        self.reports: list[dict] = []
        self.report_path = None
        self.goals = None
        self.huts_left = lambda: None
        self.caravans: dict = {}
        self.status, self.results, self.fixed = "running", {}, {}
        self.log: list[str] = []
        self._cancel = threading.Event()
        self._thread = threading.Thread(target=self._run, name="citymanager", daemon=True)
        self.busy = False            # never blocks the UI, kept for the campaign's checks

    def start(self):
        self._thread.start()
        return self

    def cancel(self):
        self._cancel.set()

    def info(self):
        return {"status": self.status, "items": self.items, "after": self.after, "happy": self.happy,
                "wonders": self.wonders, "wonder_plan": self.wonder_plan, "caravans": self.caravans,
                "wonder_need": self.wonder_need, "spare_caravans": self.spare_caravans,
                "caravan_balance": self.caravan_balance, "engineers": self.engineers_status, "caravans_waiting": getattr(self, "caravans_waiting", 0),
                "turn_reports": self.reports[-10:],
                "results": self.results, "fixed_this_turn": self.fixed, "log": self.log[-20:]}

    def _note(self, m):
        self.log.append(time.strftime("%H:%M:%S ") + m)
        self.log[:] = self.log[-80:]

    def tick(self):
        # Loading a save (village hunts) restores the option bits, so keep them off every tick
        for opt in ("throne_room", "wonder_movies"):
            self.mw.set_option(opt, False, "full-screen windows (Throne Room / wonder movie) freeze the game; "
                                           "loading a save turns the option back on, so turned it off again")
        st = self.s.state()
        if not st or st.get("turn_civ_guess") != st.get("human_civ"):
            return
        me = st["human_civ"]
        known = self.mw.known(me)
        turn = st["turn"]
        self.fixed = {k: v for k, v in self.fixed.items() if v[0] == turn}
        demo = next(x for x in st["civs"] if x["id"] == me)["government"] in ("Democracy", "Republic")
        wplan = self._plan_wonders(st, known) if self.wonders else {}
        cap = self._caravan_cap(st) if self.wonders else False
        eng = self._engineer_producers(st, known, wplan)
        for c in st["cities"]:
            if c["owner"] != me or self._cancel.is_set():
                continue
            rule = "base"
            items = self.items
            if self.after and self.after.get("built") in c.get("improvement_names", []):
                rule, items = "after", self.after["items"]
            if c["index"] in wplan:
                rule, items = "wonder", [wplan[c["index"]]] + list(items)
            elif c["index"] in eng:
                rule, items = "engineers", ["Engineers"] + [i for i in items if i not in CARAVANS]
            if "Caravan" in items and "Freight" not in items:
                items = [x for i in items for x in ((i, "Freight") if i == "Caravan" else (i,))]   # after Corporation
            if cap and any(i in CARAVANS for i in items) and rule != "wonder":
                # enough caravans for every wonder we can build now: build city improvements instead
                rule, items = "caravan_cap", [i for i in items if i == "Engineers"] + list(self.alt_items) + \
                    [i for i in items if i not in ("Barracks", "Engineers")]
            if "Factory" in items and (c.get("shield_total") or 0) < self.factory_min_shields:
                # Factory: +25% shields for 4 gold a turn (Adam Smith does not pay it) -> only real producers
                items = [i for i in items if i != "Factory"]
            # growth caps from RULES.TXT @COSMIC: Aqueduct to exceed size 8, Sewer System to exceed 12.
            # Start them a size early so rapture growth (+1 a turn) does not stall at the cap.
            if rule != "wonder":
                names_built = c.get("improvement_names", [])
                if c["size"] >= self.aqueduct_at and "Aqueduct" not in names_built:
                    items = ["Aqueduct"] + [i for i in items if i != "Aqueduct"]
                elif c["size"] >= self.sewer_at and "Sewer System" not in names_built:
                    items = ["Sewer System"] + [i for i in items if i != "Sewer System"]
            try:
                r = self.mw.set_production(c, items, known,
                                           policy=f"Policy: cities without {self.after.get('built') if self.after else ''} build "
                                                  f"{self.items}, cities with it build {self.after['items'] if self.after else ''}.")
                self.results[c["name"]] = r.get("producing") or r.get("error")
                if r.get("changed"):
                    self._note(f"{c['name']}: → {r['producing']}")
            except Exception as e:
                self.results[c["name"]] = f"error: {e}"
            key = f"{c['index']}:{c['name']}"
            if self.happy and self._starve_guard(c, st, key, turn):
                continue
            done = self.fixed.get(key)
            if done and c["entertainers"] < done[1]:
                done = None          # a village hunt loaded an older save: our write is gone
            m = 1 if (demo and c["unhappy"] > 0) or c["disorder"] else 0   # Democracy: one citizen of margin
            if self.happy and c["unhappy"] + m > c["happy"] and c["unhappy"] > 0 and not done:
                n = c["unhappy"] + m - c["happy"]
                try:
                    r = self.mw.make_entertainers(
                        c, n, f"unhappy {c['unhappy']} vs happy {c['happy']}" +
                              (" (Democracy: 2 turns of disorder topple the government → 1 citizen margin)" if m else "") +
                              " → no disorder at end of turn; rule: while unhappy outnumber happy, do not put citizens back to work")
                    self.fixed[key] = (turn, c["entertainers"] + r.get("entertainers_added", 0))
                    if r.get("ok"):
                        self._note(f"{c['name']}: +{r['entertainers_added']} entertainer")
                except Exception as e:
                    self._note(f"{c['name']}: {e}")

    def _starve_guard(self, c, st, key, turn) -> bool:
        """Alternate between disorder and hunger (player's rule): keep entertainers while the
        food box lasts; when the box would run out at the end of this turn (box + net < 0, the
        city would shrink), put entertainers back on food tiles and accept disorder for this
        turn. Under Democracy two disorder turns in a row bring anarchy, so a city that is
        already in disorder keeps its entertainers there."""
        net = c["food_surplus_raw"] - 2 * c["size"]
        if not c["entertainers"] or net >= 0 or c["food"] + net >= 0:
            return False
        if self.starve_done.get(key) == turn:
            return True
        civ = next(x for x in st["civs"] if x["id"] == st["human_civ"])
        if civ["government"] in ("Democracy", "Republic"):
            return False      # Democracy falls after two disorder turns: never trade hunger for disorder there
        if self._finder is None or self._finder[0] != turn:
            from .server import site_finder
            self._finder = (turn, site_finder(self.s, st))
        finder, W2 = self._finder[1], st["map"]["width2"]
        ent, moved = c["entertainers"], []
        while ent > 0 and net < 0:
            r = self.mw.entertainer_to_worker(
                c, st["cities"], finder, W2,
                f"food store {c['food']}, {net} per turn → food runs out at end of this turn and the city shrinks. "
                f"Player's rule: pick the lesser evil — accept one turn of disorder rather than let the city shrink")
            if not r.get("ok"):
                break
            ent -= 1
            net += r["yield"][0]
            moved.append(r["tile"])
        self.starve_done[key] = turn
        self.fixed[key] = (turn, ent)          # the happiness rule leaves this city alone this turn
        if moved:
            self._note(f"{c['name']}: about to run out of food → {len(moved)} entertainer(s) back to work (tiles {moved}), "
                       f"accepting disorder this turn")
        return True

    def _feed(self, st):
        """Starving city (food made < 2 x size) with an entertainer and some happiness margin
        (happy > unhappy): one entertainer goes back to the best free food tile, once per
        city per turn. The game re-counts happiness at turn processing; if it then shows
        unhappy > happy, the happiness rule takes a worker back."""
        me, turn = st["human_civ"], st["turn"]
        demo = next(x for x in st["civs"] if x["id"] == me)["government"] in ("Democracy", "Republic")
        # player's rule "entertaineriin toog tsool": every city with a happiness margin gets one
        # entertainer back to work per turn (starving cities first), not only starving ones
        todo = [c for c in st["cities"] if c["owner"] == me and c["entertainers"] > 0
                and c["happy"] >= c["unhappy"] + (2 if demo else 1)
                and not c["disorder"]
                and self.fed.get(f"{c['index']}:{c['name']}") != turn]
        todo.sort(key=lambda c: (c["food_surplus_raw"] >= 2 * c["size"], -(c["happy"] - c["unhappy"])))
        if not todo:
            return
        from .server import site_finder           # tiles + yields (RULES.TXT, specials)
        finder = site_finder(self.s, st)
        W2 = st["map"]["width2"]
        for c in todo:
            key = f"{c['index']}:{c['name']}"
            try:
                r = self.mw.entertainer_to_worker(
                    c, st["cities"], finder, W2,
                    (f"starving: food {c['food_surplus_raw']} < needed {2 * c['size']} (size {c['size']}×2), "
                     if c["food_surplus_raw"] < 2 * c["size"] else "fewer entertainers (player's rule): ") +
                    f"happy {c['happy']} ≥ unhappy {c['unhappy']} + {2 if demo else 1}, so one entertainer goes back to work "
                    f"(one per city per turn)")
                self.fed[key] = turn
                self.fixed[f"{c['index']}:{c['name']}"] = (turn, c["entertainers"] - 1)
                if r.get("ok"):
                    self._note(f"{c['name']}: entertainer → tile {r['tile']} (food {r['yield'][0]})")
            except Exception as e:
                self._note(f"{c['name']}: feed error {e}")

    def _plan_wonders(self, st, known) -> dict:
        """Give each wonder city a different wonder: city k prefers list[k], then the rest of
        the list in order; skip wonders that need a missing advance or are built already."""
        names = self.wonders["list"]
        cities = {c["index"]: c for c in st["cities"] if c["owner"] == st["human_civ"]}
        plan, taken = {}, set()
        # sticky: a city keeps the wonder it is already building (its shields stay on it)
        for idx in self.wonders["cities"]:
            c = cities.get(idx)
            p = (c or {}).get("producing") or {}
            if c and p.get("kind") == "improvement" and p.get("name") in names and p["name"] not in taken:
                ok, _ = self.mw.can_build(c, p["name"], known)
                if ok:
                    plan[idx] = p["name"]
                    taken.add(p["name"])
        for k, idx in enumerate(self.wonders["cities"]):
            c = cities.get(idx)
            if not c or idx in plan:
                continue
            order = ([names[k]] + [n for n in names if n != names[k]]) if k < len(names) else list(names)
            for n in order:
                if n in taken:
                    continue
                ok, _ = self.mw.can_build(c, n, known)
                if ok:
                    plan[idx] = n
                    taken.add(n)
                    break
        self._open_wonder_city(st, known, taken)
        if plan != self.wonder_plan:
            J(self.s, "city", "wonder plan",
              "Each wonder city builds a different wonder; priority (happiness/growth first): " + ", ".join(names) +
              ". Wonders lacking the tech or already built are skipped → " +
              ", ".join(f"{cities[i]['name']}: {n}" for i, n in plan.items()))
            self.wonder_plan = plan
        self._dispatch_caravans(st, cities)
        return plan

    def _engineer_producers(self, st, known, wplan) -> set:
        """Engineers (player: "neg hotod 1 engineer hiigeed barilga"): once Explosives is known,
        every city builds one Engineer of its own (home city), then buildings. A city waits
        until it can afford it: size >= 2 (an Engineer takes 1 size) and food +2 to spare
        (it eats 2 food per turn under Democracy)."""
        if "Exp" not in known or not self.engineers_per_city:
            return set()
        me = st["human_civ"]
        homes = {u["home_city"] for u in st["units"] if u["owner"] == me and u["type_name"] == "Engineers"}
        mine = [c for c in st["cities"] if c["owner"] == me]
        out = {c["index"] for c in mine if c["index"] not in homes and c["index"] not in wplan and c["size"] >= 2
               and c["food_surplus_raw"] - 2 * c["size"] >= 2 and not c["disorder"]}
        waiting = sum(1 for c in mine if c["index"] not in homes and c["index"] not in out and c["index"] not in wplan)
        self.engineers_status = {"cities_with_engineer": len(homes & {c["index"] for c in mine}),
                                 "building_now": len(out), "waiting_for_food_or_size": waiting}
        if out - self._eng_logged:
            new = out - self._eng_logged
            self._eng_logged |= new
            J(self.s, "city", "engineers", f"{len(new)} cities build their own Engineer (1 per city, then buildings; "
              f"size ≥2 and food above +2, so they can support it). {waiting} cities without enough food/size wait")
        return out

    def _caravan_cap(self, st) -> bool:
        """Match caravan output to what the wonders can use (player's rule "caravanii hurddaa
        tohiruulj"): caravans that exist or are half-built vs caravans the buildable wonders
        still need plus a reserve for the next wonders. Over it -> stop making caravans;
        back under half the reserve -> make them again."""
        me = st["human_civ"]
        need = sum(v["caravans_needed"] for v in self.wonder_need.values())
        # wonders the next techs of the research plan unlock (player: "ireeduin heregtseendee tohiruul")
        r = self.s.game.rules
        names, codes = r.advances, getattr(r, "advance_codes", [])
        soon = {codes[names.index(t)] for t in (getattr(self.s, "research_plan", []) or [])[:3]
                if t in names and names.index(t) < len(codes)}
        built = self.mw.wonders_built()
        planned = set(self.wonder_plan.values())
        upcoming = []
        for wid in range(self.mw.WONDER_FIRST, len(r.improvements)):
            n = r.improvements[wid]
            if wid in built or n in planned or n not in self.wonders.get("list", []):
                continue
            if (getattr(r, "improve_prereq", []) + [""] * 100)[wid] in soon:
                upcoming.append(n)
                need += -(-r.improve_cost[wid] // 50)
        units = sum(1 for u in st["units"] if u["owner"] == me and u["type_name"] in CARAVANS)
        part = sum(c["shields"] for c in st["cities"] if c["owner"] == me and c["producing"]["name"] in CARAVANS) / 50
        have = units + part
        res = self.caravan_reserve
        was = self.cap_on
        if not was and have >= need + res:
            self.cap_on = True
        elif was and have < need + res // 2:
            self.cap_on = False
        self.caravan_target = need + res
        self.caravan_balance = {"need": need, "upcoming_wonders": upcoming, "reserve": res, "target": need + res,
                                "caravans": units, "in_production": round(part, 1),
                                "surplus": max(0, units - (need + res)), "cap_on": self.cap_on}
        if self.cap_on != was:
            J(self.s, "city", "caravan cap",
              (f"Enough caravans: {units} ready + {part:.0f} in production ≥ {need} needed for wonders + {res} reserve → "
               f"stop building caravans and build {self.alt_items} (finished caravans wait for the next wonder)")
              if self.cap_on else
              (f"Not enough caravans: {units} + {part:.0f} < {need} + {res // 2} → build caravans again"), **self.caravan_balance)
        return self.cap_on

    def _wonder_need(self, c):
        """Caravans (50 shields each) still needed to finish the wonder this city builds."""
        r = self.s.game.rules
        name = self.wonder_plan.get(c["index"]) or c["producing"]["name"]
        wid = next((i for i, n in enumerate(r.improvements) if n == name), None)
        cost = r.improve_cost[wid] if wid is not None and wid < len(getattr(r, "improve_cost", [])) else 0
        shields = c["shields"] if c["producing"]["name"] == name else 0
        return max(0, -(-(cost - shields) // 50)), cost, shields

    def _dispatch_caravans(self, st, cities):
        """Caravans by need (player's rule "caravanuudaa zov huviarla"): each wonder city gets
        just the caravans its wonder still needs (cost - shields, 50 per caravan), highest
        wonder priority first, nearest caravans first. Caravans heading to a city that already
        has enough are re-routed. Spare caravans make the manager open another wonder city;
        when no more can be opened they go to the nearest wonder city (the shields then go to
        that city's next wonder)."""
        if not self.runner or not self.wonder_plan:
            return
        live = ("pending", "running", "waiting_next_turn", "paused_popup")
        W2 = st["map"]["width2"]
        targets = [cities[i] for i in self.wonder_plan if i in cities]
        if not targets:
            return
        names = self.wonders.get("list", [])
        prio = lambda c: names.index(self.wonder_plan[c["index"]]) if self.wonder_plan[c["index"]] in names else 99
        targets.sort(key=prio)
        by_xy = {(c["x"], c["y"]): c for c in targets}

        def dist(u, c):
            dx = abs(u["x"] - c["x"]); dx = min(dx, W2 - dx); dy = abs(u["y"] - c["y"])
            return max(dy, (dx + dy) // 2)
        cvs = [u for u in st["units"] if u["owner"] == st["human_civ"] and u["type_name"] in CARAVANS]
        going = {}
        for u in cvs:
            t = self.runner.tasks.get(u["id"])
            if t and t["status"] in live and tuple(t["goal"]) in by_xy:
                going.setdefault(by_xy[tuple(t["goal"])]["index"], []).append(u)
            elif t and t["status"] in live and str(u["id"]) not in self.caravans:
                continue                       # on some other errand (not sent by us)
            elif t and t["status"] in live:
                going.setdefault(None, []).append(u)   # sent by us to a city that no longer builds a wonder
            elif (u["x"], u["y"]) in by_xy:
                continue                       # inside a wonder city: the CARAVANMENU popup handles it
            else:
                going.setdefault(None, []).append(u)
        free = list(going.get(None, []))
        keep = {}
        self.wonder_need = {}
        for c in targets:
            need, cost, shields = self._wonder_need(c)
            mine = sorted(going.get(c["index"], []), key=lambda u: dist(u, c))
            keep[c["index"]] = mine[:need]
            free += mine[need:]
            self.wonder_need[f"{c['name']}#{c['index']}"] = {"wonder": self.wonder_plan[c["index"]], "cost": cost, "shields": shields,
                                           "caravans_needed": need, "caravans_going": len(mine)}
        moved = 0
        for c in targets:
            wn = self.wonder_need[f"{c['name']}#{c['index']}"]
            need = wn["caravans_needed"]
            while len(keep[c["index"]]) < need and free:
                u = min(free, key=lambda u: dist(u, c))
                free.remove(u)
                keep[c["index"]].append(u)
                t = self.runner.tasks.get(u["id"])
                if t and t["status"] in live and t["goal"] == [c["x"], c["y"]]:
                    continue
                r = self.runner.add(u["id"], c["x"], c["y"])
                old = self.caravans.get(str(u["id"]))
                self.caravans[str(u["id"])] = c["name"]
                moved += 1
                J(self.s, "city", "send caravan" if not old else "reroute caravan",
                  f"Caravan #{u['id']} → {c['name']} ({self.wonder_plan[c['index']]}): the wonder needs "
                  f"{need} caravans ({wn['shields']}/"
                  f"{wn['cost']} shields, 50 per caravan); first by priority, nearest caravan"
                  + (f" (was heading to {old}, which now has enough)" if old and old != c["name"] else ""),
                  unit_id=u["id"], ok=r.get("ok"))
        self.spare_caravans = len(free)
        if free and not self._can_open_wonder_city:
            # every buildable wonder has its caravans: the rest wait where they are for the next wonder
            held = 0
            for u in free:
                t = self.runner.tasks.get(u["id"])
                if t and t["status"] in live and tuple(t["goal"]) in by_xy:
                    self.runner.cancel(u["id"])
                    held += 1
                    J(self.s, "city", "hold caravan",
                      f"Caravan #{u['id']}: every wonder already has enough caravans on the way (so no shields are wasted) "
                      f"→ waiting here; assigned from the nearest when the next wonder starts",
                      unit_id=u["id"])
            self.caravans_waiting = len(free)

    def _open_wonder_city(self, st, known, taken):
        """Spare caravans and a wonder nobody builds yet: make one more city a wonder city
        (most shields per turn, size >= 3, not in disorder, not a wonder city already)."""
        self._can_open_wonder_city = False
        maxc = int(self.wonders.get("max_cities", 8))
        if len(self.wonders["cities"]) >= maxc:
            return
        me = st["human_civ"]
        mine = [c for c in st["cities"] if c["owner"] == me and c["index"] not in self.wonders["cities"]
                and not c["disorder"]]
        cands = sorted(mine, key=lambda c: (-(c.get("shield_total") or 0), -c["size"], c["index"]))
        for n in self.wonders["list"]:
            if n in taken:
                continue
            for c in cands[:40]:
                ok, _ = self.mw.can_build(c, n, known)
                if ok:
                    self._can_open_wonder_city = True
                    if getattr(self, "spare_caravans", 0) > 0:
                        self.wonders["cities"] = list(self.wonders["cities"]) + [c["index"]]
                        J(self.s, "city", "open wonder city",
                          f"no wonder for {self.spare_caravans} spare caravans (the others have enough) → {c['name']} "
                          f"(shields/turn {c.get('shield_total')}, size {c['size']}) builds {n}; caravans assigned "
                          f"from the next round", city=c["name"], wonder=n)
                        self.spare_caravans = 0
                    return

    def _gov_check(self, st):
        """Every tick: when the government changes to one in gov_rates, set those rates once."""
        civ = next(c for c in st["civs"] if c["id"] == st["human_civ"])
        gov, prev = civ["government"], getattr(self, "gov_prev", None)
        self.gov_prev = gov
        want = (self.gov_rates or {}).get(gov)
        if prev is not None and gov != prev and want and not st.get("popup"):
            r = self.mw.set_rates(civ, want[0], want[1], want[2],
                                  f"government {prev} → {gov}: player-approved policy Tax/Lux/Sci "
                                  f"{want[0]}0/{want[1]}0/{want[2]}0 (lux → rapture growth)")
            self.gov_action = f"{gov}: rates {want}" if r.get("ok") else f"could not set rates: {r.get('error')}"
        elif prev is not None and gov != prev and want:
            self.gov_prev = prev           # popup open: try again next tick

    def _buy_wonders(self, st):
        """Once per turn (player's rule "wonderoo yaraltai duusgahiin tuld mongoor hudaldaj avj
        bolno"): buy wonders in priority order while the price leaves `gold_reserve` gold. A wonder
        whose box is still empty costs double, so it waits for its first caravan."""
        from .buy import buy, estimate
        me = st["human_civ"]
        gold = next(x for x in st["civs"] if x["id"] == me)["gold"]
        cities = {c["index"]: c for c in st["cities"] if c["owner"] == me}
        names = self.wonders.get("list", [])
        order = sorted(self.wonder_plan.items(), key=lambda kv: names.index(kv[1]) if kv[1] in names else 99)
        r = self.s.game.rules
        bought = []
        for idx, name in order:
            c = cities.get(idx)
            if not c or c["producing"]["name"] != name:
                continue
            if name in self.wonders.get("no_buy", []):
                continue          # player: no gold on war ("daind mongo zartsuulj barag hereggui")
            wid = next((i for i, n in enumerate(r.improvements) if n == name), None)
            cost = r.improve_cost[wid] if wid is not None and wid < len(r.improve_cost) else 0
            rem = cost - c["shields"]
            if rem <= 0 or c["shields"] == 0 or self.__dict__.get("buy_skip", {}).get(idx, 0) >= 2:
                continue
            est = estimate(rem, True, True)
            if est > gold - self.gold_reserve:
                continue
            self.busy = True
            was = self.runner.enabled if self.runner else None
            if self.runner:
                self.runner.enabled = False        # no unit moves while the City Window is used
            time.sleep(1.5)
            try:
                res = buy(self.s, idx, gold - self.gold_reserve,
                          why=f"priority {names.index(name) + 1 if name in names else '?'}, {rem} shields left; "
                              f"keeps the {self.gold_reserve} gold reserve")
            finally:
                self.busy = False
                if self.runner:
                    self.runner.enabled = was
            if res.get("bought"):
                gold = res["gold_after"]
                bought.append(f"{c['name']} {name} {res['price']}g")
                self._note(f"bought: {c['name']} {name} {res['price']} gold")
            elif not res.get("ok"):
                self._note(f"buy {c['name']}: {res.get('error')}")
                skip = self.__dict__.setdefault("buy_skip", {})
                skip[idx] = skip.get(idx, 0) + 1
                if skip[idx] < 2:
                    return bought            # one more try on a later tick, then this city is skipped
                continue
        # research first (player: "research taldaa iluu anhaariya"): with gold left over, buy
        # Libraries (2 gold per missing shield), biggest cities first, a few per turn
        # growth caps first (Aqueduct / Sewer in cities about to hit size 8 / 12), then Library, MarketPlace
        rank = {"Aqueduct": 0, "Sewer System": 0, "Library": 1, "MarketPlace": 2}
        libs = sorted([c for c in cities.values() if c["producing"]["name"] in rank
                       and c["shields"] > 0 and c["index"] not in self.wonder_plan],
                      key=lambda c: (rank[c["producing"]["name"]], -c["size"], -c["shields"]))
        n = 0
        for c in libs:
            if n >= self.libraries_per_turn:
                break
            rem = r.improve_cost[r.improvements.index(c["producing"]["name"])] - c["shields"]
            if rem <= 0 or estimate(rem, True, False) > gold - self.gold_reserve:
                continue
            self.busy = True
            was = self.runner.enabled if self.runner else None
            if self.runner:
                self.runner.enabled = False
            time.sleep(1.5)
            try:
                res = buy(self.s, c["index"], gold - self.gold_reserve,
                          why=f"{c['producing']['name']} (Library + MarketPlace in every city — player's rule), "
                              f"size {c['size']}; after wonders, keeps the "
                              f"{self.gold_reserve} gold reserve")
            finally:
                self.busy = False
                if self.runner:
                    self.runner.enabled = was
            if res.get("bought"):
                gold = res["gold_after"]
                bought.append(f"{c['name']} {c['producing']['name']} {res['price']}g")
                n += 1
            elif not res.get("ok"):
                break
        self.buy_fail = 0
        self.buy_skip = {}
        self.bought_turn = st["turn"]
        self.last_buys = bought
        return bought

    def _after_tick(self, st):
        self._gov_check(st)
        if self.buy_wonders and getattr(self, "bought_turn", None) != st["turn"] and self.wonder_plan \
                and not self.hunt_live() and not st.get("popup") and self.checked_turn == st["turn"]:
            try:
                self._buy_wonders(st)
            except Exception as e:
                self._note(f"buy error: {type(e).__name__}: {e}")
                self.bought_turn = st["turn"]
        if self.feed:
            self._feed(st)
        if st["turn"] != self.checked_turn:
            self.checked_turn = st["turn"]
            self._turn_check(st)

    def _turn_check(self, st):
        """Once per turn: unhappiness, starvation and money. Money rule: gold is low or
        falling -> move 10% from science to tax; plenty and rising -> move 10% back."""
        me = st["human_civ"]
        civ = next(c for c in st["civs"] if c["id"] == me)
        cs = [c for c in st["cities"] if c["owner"] == me]
        gold = civ["gold"]
        prev = self.gold_prev
        self.gold_prev = gold
        riot = [c["name"] for c in cs if c["unhappy"] > c["happy"]]
        disorder = [c["name"] for c in cs if c["disorder"]]
        starving = [c["name"] for c in cs if c["food_surplus_raw"] < 2 * c["size"]]
        tax, sci = civ["tax_rate"], civ["science_rate"]
        lux = 10 - tax - sci
        cap = MAX_RATE.get(civ["government"], 6)
        action = "rates unchanged"
        delta = None if prev is None else gold - prev
        gov = civ["government"]
        if gov == "Democracy" and disorder and lux < cap and (sci > 0 or tax > 2):
            # two disorder turns in a row would bring anarchy again: more luxury now
            nt, ns = (tax, sci - 1) if sci > 0 else (tax - 1, sci)
            r = self.mw.set_rates(civ, nt, lux + 1, ns,
                                  f"Democracy: {len(disorder)} cities in disorder ({', '.join(disorder[:4])}); disorder again next "
                                  f"turn would topple the government → Lux {lux}0→{lux + 1}0%")
            self.gov_action = f"Lux {lux}0→{lux + 1}0% (disorder {len(disorder)})" if r.get("ok") else \
                f"could not raise Lux: {r.get('error')}"
            tax, sci, lux = nt, ns, lux + 1
        self.calm_turns = (getattr(self, "calm_turns", 0) + 1) if (gov == "Democracy" and not disorder) else 0
        # cities grew (rapture) -> 50% Lux brought disorder every time (turns 123, 126): keep 60%
        floor = int(getattr(self, "lux_floor", 6))
        if not getattr(self, "gov_action", None) and gov == "Democracy" and self.calm_turns >= 3 \
                and lux > floor and sci < cap:
            r = self.mw.set_rates(civ, tax, lux - 1, sci + 1,
                                  f"Democracy: {self.calm_turns} turns in a row without disorder → Lux {lux}0→{lux - 1}0%, "
                                  f"Sci {sci}0→{sci + 1}0% (approved by the player; Lux never below {floor}0%)")
            self.gov_action = f"Lux {lux}0→{lux - 1}0%, Sci +10%" if r.get("ok") else f"rates: {r.get('error')}"
            self.calm_turns = 0
            lux, sci = lux - 1, sci + 1
        if getattr(self, "gov_action", None):
            action, self.gov_action = self.gov_action, None
        elif gov == "Anarchy":
            action = "Anarchy: the game sets the rates itself"
        elif (gold < 60 or (delta is not None and delta < 0 and gold < 200)) and tax < cap and sci > 0:
            r = self.mw.set_rates(civ, tax + 1, lux, sci - 1,
                                  f"gold low/falling (gold {gold}, change {delta}) → 10% from science to tax")
            action = f"tax {tax}0→{tax + 1}0%" if r.get("ok") else f"could not raise tax: {r.get('error')}"
        elif gold > 400 and delta is not None and delta > 0 and (tax > 2 or (tax > 1 and gold > 1500)) and sci < cap:
            r = self.mw.set_rates(civ, tax - 1, lux, sci + 1,
                                  f"gold sufficient and growing (gold {gold}, +{delta}) → 10% from tax to science")
            action = f"tax {tax}0→{tax - 1}0%" if r.get("ok") else f"could not lower tax: {r.get('error')}"
        rep = {"turn": st["turn"], "gold": gold, "gold_change": delta, "rates": [tax, lux, sci],
               "cities": len(cs), "unhappy_gt_happy": len(riot), "disorder": len(disorder),
               "starving": len(starving), "entertainers": sum(c["entertainers"] for c in cs), "money_action": action,
               "government": civ["government"], "techs": civ["techs"], "beakers": civ["beakers"],
               "researching": civ["researching"], "population": sum(c["size"] for c in cs),
               "rivals": {str(x["id"]): {"name": x["name"], "cities": sum(1 for c in st["cities"] if c["owner"] == x["id"]),
                                         "techs": x["techs"]}
                          for x in st["civs"] if x["alive"] and x["id"] not in (0, me)}}
        if self.goals is not None:
            try:
                ev = self.goals.evaluate(self.s, self.mw, self.huts_left())
                rep["goals"] = [{"id": g["id"], "type": g["type"], "target": g["target"], "current": g["current"],
                                 "status": g["status"]} for g in ev]
            except Exception as e:
                rep["goals_error"] = str(e)
        self.reports.append(rep)
        self.reports[:] = self.reports[-50:]
        if self.report_path:
            try:
                with open(self.report_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rep, ensure_ascii=False) + "\n")
            except OSError:
                pass
        J(self.s, "city", "turn check",
          f"Turn {st['turn']}: gold {gold} ({'+' if (delta or 0) >= 0 else ''}{delta}), Tax/Lux/Sci {tax}/{lux}/{sci}; "
          f"unhappy>happy {len(riot)} {riot[:5]}, disorder {len(disorder)} {disorder[:5]}, starving {len(starving)} "
          f"{starving[:5]}; money: {action}", **{k: v for k, v in rep.items() if k != "turn"})

    def blocking(self, st) -> bool:
        """Cities that would riot at the end of this turn and are not handled yet."""
        me = st["human_civ"]
        demo = next(x for x in st["civs"] if x["id"] == me)["government"] in ("Democracy", "Republic")
        for c in st["cities"]:
            m = 1 if (demo and c["unhappy"] > 0) or c["disorder"] else 0
            if c["owner"] == me and c["unhappy"] > 0 and c["unhappy"] + m > c["happy"]:
                done = self.fixed.get(f"{c['index']}:{c['name']}")
                if not done or c["entertainers"] < done[1]:
                    return True
        return False

    def _run(self):
        try:
            while not self._cancel.is_set():
                try:
                    self.tick()
                    st = self.s.state()
                    if st and st.get("turn_civ_guess") == st.get("human_civ"):
                        self._after_tick(st)
                except Exception as e:
                    self._note(f"tick error: {type(e).__name__}: {e}")
                self._cancel.wait(3)
            self.status = "cancelled"
        except Exception as e:
            self.status = f"failed: {e}"
