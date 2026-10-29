"""
Player goals and how far along they are, computed from live memory.

A goal: {"id", "type", "target", "by_turn" (optional), "note", "created_turn", "done_turn"}
Types:
  cities      own cities >= target (number)
  gold        treasury >= target
  wonder      wonder `target` (name) built in one of our cities
  tech        advance `target` (name) known
  government  government == target (name)
  no_disorder number of cities in disorder == 0 (target ignored)
  no_starving number of starving cities == 0
  villages    villages left on the map <= target
Saved in logs/goals.json so they survive restarts.
"""
from __future__ import annotations

import json
import os
import threading

TYPES = ["cities", "gold", "wonder", "tech", "government", "no_disorder", "no_starving", "villages"]


class Goals:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self.items: list[dict] = []
        try:
            with open(path, encoding="utf-8") as f:
                self.items = json.load(f)
        except (OSError, ValueError):
            self.items = []

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.items, f, ensure_ascii=False, indent=1)

    def add(self, g: dict, turn: int) -> dict:
        if g.get("type") not in TYPES:
            raise ValueError(f"type must be one of {TYPES}")
        with self._lock:
            gid = max([x["id"] for x in self.items], default=0) + 1
            item = {"id": gid, "type": g["type"], "target": g.get("target"),
                    "by_turn": int(g["by_turn"]) if g.get("by_turn") not in (None, "") else None,
                    "note": g.get("note", ""), "created_turn": turn, "done_turn": None}
            self.items.append(item)
            self._save()
            return item

    def remove(self, gid: int):
        with self._lock:
            self.items = [x for x in self.items if x["id"] != gid]
            self._save()

    def evaluate(self, s, mw, huts_left: int | None = None) -> list[dict]:
        st = s.state()
        me = st["human_civ"]
        civ = next(c for c in st["civs"] if c["id"] == me)
        cs = [c for c in st["cities"] if c["owner"] == me]
        known = mw.known(me) if mw else set()
        codes = getattr(s.game.rules, "advance_codes", [])
        names = s.game.rules.advances
        known_names = {names[i] for i, c in enumerate(codes) if c in known and i < len(names)}
        built = {}
        if mw:
            cities = {c["index"]: c for c in st["cities"]}
            for wid, ci in mw.wonders_built().items():
                c = cities.get(ci)
                built[s.game.rules.improvement_name(wid)] = bool(c and c["owner"] == me)
        out, changed = [], False
        for g in self.items:
            t, tgt = g["type"], g.get("target")
            cur, done, pct = None, False, 0.0
            if t == "cities":
                cur, done = len(cs), len(cs) >= int(tgt)
                pct = min(1.0, len(cs) / max(1, int(tgt)))
            elif t == "gold":
                cur, done = civ["gold"], civ["gold"] >= int(tgt)
                pct = min(1.0, max(0, civ["gold"]) / max(1, int(tgt)))
            elif t == "wonder":
                mine = built.get(tgt)
                prog = next((c for c in cs if c["producing"]["name"] == tgt), None)
                cost = 0
                r = s.game.rules
                wid = next((i for i, n in enumerate(r.improvements) if n == tgt), None)
                if wid is not None and wid < len(getattr(r, "improve_cost", [])):
                    cost = r.improve_cost[wid]
                if mine:
                    cur, done, pct = "built", True, 1.0
                elif mine is False:
                    cur, pct = "built by another civ", 0.0
                elif prog:
                    cur = f"{prog['name']}: {prog['shields']}/{cost or '?'}"
                    pct = min(1.0, prog["shields"] / cost) if cost else 0.0
                else:
                    cur = "not built"
            elif t == "tech":
                done = tgt in known_names
                cur, pct = ("known" if done else f"researching: {civ['researching']}"), (1.0 if done else 0.0)
            elif t == "government":
                cur, done = civ["government"], civ["government"] == tgt
                pct = 1.0 if done else 0.0
            elif t == "no_disorder":
                n = sum(1 for c in cs if c["disorder"])
                cur, done, pct = n, n == 0, 1.0 if n == 0 else 0.0
            elif t == "no_starving":
                n = sum(1 for c in cs if c["food_surplus_raw"] < 2 * c["size"])
                cur, done, pct = n, n == 0, 1.0 if n == 0 else 0.0
            elif t == "villages":
                cur = huts_left
                done = huts_left is not None and huts_left <= int(tgt)
                pct = 1.0 if done else 0.0
            status = "done" if done else "in progress"
            if g.get("by_turn") and not done and st["turn"] > g["by_turn"]:
                status = "overdue"
            if done and not g.get("done_turn"):
                g["done_turn"] = st["turn"]
                changed = True
            if g.get("done_turn"):
                status = f"done (turn {g['done_turn']})"
            out.append({**g, "current": cur, "done": done or bool(g.get("done_turn")), "progress": round(pct, 3),
                        "status": status, "turn": st["turn"]})
        if changed:
            with self._lock:
                self._save()
        return out
