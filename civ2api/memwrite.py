"""
Stage 2 (approved by the player on 2026-10-27): change a few game fields by writing the
game's memory directly, instead of driving the City Window / Tax Rate dialog.

Only these fields are ever written (whitelist), each followed by a read-back check:
  * City+0x39  Building        i8   what the city builds (unit type >= 0, improvement = -id)
  * City+0x30  Workers         u32  bits 0-19 worked tiles (bit 20 = centre), bits 26-31 = specialists
  * City+0x16  Specialists     u32  2 bits per specialist, 1 = entertainer
  * Civ+0x13 / +0x14  science / tax rate (tens of percent); luxury is the rest
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

import struct
import threading
import time

from . import memmap as M
from . import win32
from .journal import note as J

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
        self.wonder_plan: dict = {}
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
                "results": self.results, "fixed_this_turn": self.fixed, "log": self.log[-20:]}

    def _note(self, m):
        self.log.append(time.strftime("%H:%M:%S ") + m)
        self.log[:] = self.log[-80:]

    def tick(self):
        st = self.s.state()
        if not st or st.get("turn_civ_guess") != st.get("human_civ"):
            return
        me = st["human_civ"]
        known = self.mw.known(me)
        turn = st["turn"]
        self.fixed = {k: v for k, v in self.fixed.items() if v[0] == turn}
        wplan = self._plan_wonders(st, known) if self.wonders else {}
        for c in st["cities"]:
            if c["owner"] != me or self._cancel.is_set():
                continue
            rule = "base"
            items = self.items
            if self.after and self.after.get("built") in c.get("improvement_names", []):
                rule, items = "after", self.after["items"]
            if c["index"] in wplan:
                rule, items = "wonder", [wplan[c["index"]]] + list(items)
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
            done = self.fixed.get(key)
            if done and c["entertainers"] < done[1]:
                done = None          # a village hunt loaded an older save: our write is gone
            if self.happy and c["unhappy"] > c["happy"] and not done:
                n = c["unhappy"] - c["happy"]
                try:
                    r = self.mw.make_entertainers(
                        c, n, f"unhappy {c['unhappy']} > happy {c['happy']} → the city will riot at end of turn; "
                              f"rule: while unhappy outnumber happy, do not put citizens back to work")
                    self.fixed[key] = (turn, c["entertainers"] + r.get("entertainers_added", 0))
                    if r.get("ok"):
                        self._note(f"{c['name']}: +{r['entertainers_added']} entertainer")
                except Exception as e:
                    self._note(f"{c['name']}: {e}")

    def _plan_wonders(self, st, known) -> dict:
        """Give each wonder city a different wonder: city k prefers list[k], then the rest of
        the list in order; skip wonders that need a missing advance or are built already."""
        names = self.wonders["list"]
        cities = {c["index"]: c for c in st["cities"] if c["owner"] == st["human_civ"]}
        plan, taken = {}, set()
        for k, idx in enumerate(self.wonders["cities"]):
            c = cities.get(idx)
            if not c:
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
        if plan != self.wonder_plan:
            J(self.s, "city", "wonder plan",
              "Each wonder city builds a different wonder; priority (happiness/growth first): " + ", ".join(names) +
              ". Wonders lacking the tech or already built are skipped → " +
              ", ".join(f"{cities[i]['name']}: {n}" for i, n in plan.items()))
            self.wonder_plan = plan
        self._dispatch_caravans(st, cities)
        return plan

    def _dispatch_caravans(self, st, cities):
        """Every own Caravan without a task goes to the wonder city that still needs the
        most shields (round robin on ties); on arrival the CARAVANMENU popup is answered
        "Help build Wonder" (Session.answer_dialog)."""
        if not self.runner or not self.wonder_plan:
            return
        live = ("pending", "running", "waiting_next_turn", "paused_popup")
        targets = [cities[i] for i in self.wonder_plan if i in cities]
        if not targets:
            return
        for u in st["units"]:
            if u["owner"] != st["human_civ"] or u["type_name"] != "Caravan":
                continue
            t = self.runner.tasks.get(u["id"])
            if t and t["status"] in live:
                continue
            cands = [c for c in targets if (c["x"], c["y"]) != (u["x"], u["y"])]
            if not cands:
                continue
            need = lambda c: -(c["shields"])          # fewest shields so far first
            cnt = lambda c: sum(1 for v in self.caravans.values() if v == c["name"])
            dest = min(cands, key=lambda c: (cnt(c), need(c)))
            r = self.runner.add(u["id"], dest["x"], dest["y"])
            self.caravans[str(u["id"])] = dest["name"]
            J(self.s, "city", "send caravan",
              f"Caravan #{u['id']} → {dest['name']} ({self.wonder_plan.get(dest['index'])}): rotated between wonder cities "
              f"(to the city that has received the fewest caravans); on arrival choose 'Help build Wonder'",
              unit_id=u["id"], ok=r.get("ok"))

    def blocking(self, st) -> bool:
        """Cities that would riot at the end of this turn and are not handled yet."""
        me = st["human_civ"]
        for c in st["cities"]:
            if c["owner"] == me and c["unhappy"] > c["happy"]:
                done = self.fixed.get(f"{c['index']}:{c['name']}")
                if not done or c["entertainers"] < done[1]:
                    return True
        return False

    def _run(self):
        try:
            while not self._cancel.is_set():
                try:
                    self.tick()
                except Exception as e:
                    self._note(f"tick error: {type(e).__name__}: {e}")
                self._cancel.wait(3)
            self.status = "cancelled"
        except Exception as e:
            self.status = f"failed: {e}"
