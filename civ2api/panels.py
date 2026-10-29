"""
Data for the console panels (all read-only, from memory + our own logs):

  alerts(s, hub)      what needs attention now: barbarian/foreign units near our cities,
                      disorder / riot next turn / starving cities, a rival building or having
                      built a wonder we want, low gold, Anarchy, stopped jobs
  rivals(s, mw)       every civ: cities, population, techs, gold, government, army, wonders
                      built and wonders in production
  government(s, hub)  government, plan, rates, max rate, rate policy, revolution turn
  research(s, mw)     current research, beakers (per turn from the turn reports), the
                      research plan with known / can research now / needs prerequisites
  saves_list(dir)     every .sav in civ2api/saves with turn and kind
"""
from __future__ import annotations

import os
import re
import time

WONDER_FIRST = 39


def _dist(a, b, w2, flat):
    dx, dy = abs(a["x"] - b["x"]), abs(a["y"] - b["y"])
    if w2 and not flat:
        dx = min(dx, w2 - dx)
    return max(dy, (dx + dy) // 2)


def _wonder_cost(r, wid):
    cost = getattr(r, "improve_cost", [])
    return cost[wid] if wid < len(cost) else 0


def _my(st):
    me = st["human_civ"]
    return me, next(c for c in st["civs"] if c["id"] == me)


def alerts(s, hub, mw) -> dict:
    st = s.state()
    me, civ = _my(st)
    r = s.game.rules
    ms = st.get("map") or {}
    w2, flat = ms.get("width2", 0), bool(ms.get("flat"))
    types = s.game.unit_types
    mine = [c for c in st["cities"] if c["owner"] == me]
    out = []

    def add(level, kind, text, x=None, y=None, **kw):
        out.append({"level": level, "kind": kind, "text": text, "x": x, "y": y, **kw})

    # --- units near our cities
    for u in st["units"]:
        if u["owner"] == me:
            continue
        ut = types[u["type"]] if u["type"] < len(types) else {}
        if not ut.get("attack"):
            continue                                   # caravans, diplomats, settlers ...
        near = None
        for c in mine:
            d = _dist(u, c, w2, flat)
            if d <= 3 and (near is None or d < near[1]):
                near = (c, d)
        if near:
            c, d = near
            barb = u["owner"] == 0
            if barb or d <= 2:
                who = "Barbarian" if barb else next((x["name"] for x in st["civs"] if x["id"] == u["owner"]), "?")
                add("bad" if barb or d <= 1 else "warn", "enemy_near",
                    f"{who} {u['type_name']} (A{ut.get('attack')}) → {d} tiles from {c['name']}", u["x"], u["y"],
                    city=c["name"], owner=u["owner"])

    # --- cities
    for c in mine:
        fixed = f" · {c['entertainers']} entertainer(s) set (counts refresh at end of turn)" if c["entertainers"] else ""
        if c["disorder"]:
            add("warn" if fixed else "bad", "disorder",
                f"{c['name']} disorder (happy {c['happy']} < unhappy {c['unhappy']}){fixed}", c["x"], c["y"])
        elif c["unhappy"] > c["happy"]:
            add("info" if fixed else "warn", "riot_next",
                f"{c['name']}: unhappy {c['unhappy']} > happy {c['happy']} — disorder next turn{fixed}",
                c["x"], c["y"])
        if c["food_surplus_raw"] < 2 * c["size"]:
            add("warn", "starving", f"{c['name']} starving (food {c['food_surplus_raw']} < needed {2 * c['size']})",
                c["x"], c["y"])

    # --- pollution on tiles our cities work (Engineers on auto clean it up)
    try:
        from .memmap import CITY_TILES
        tiles = {(t["x"], t["y"]): t for t in s.game.read_tiles(0, 0, w2, ms.get("height", 0))}
        for c in mine:
            n = sum(1 for dx, dy in CITY_TILES[:20] + [(0, 0)]
                    if (tiles.get(((c["x"] + dx) % w2 if w2 else c["x"] + dx, c["y"] + dy)) or {}).get("pollution"))
            if n:
                add("warn", "pollution", f"{c['name']}: {n} polluted tiles (less food/shields; Engineers clean it)",
                    c["x"], c["y"])
        for c in mine:
            if c["size"] >= 8 and "Aqueduct" not in c.get("improvement_names", []):
                add("info", "aqueduct", f"{c['name']} size {c['size']}: no Aqueduct, cannot grow past 8",
                    c["x"], c["y"])
    except Exception:
        pass

    # --- wonder race
    job = getattr(hub, "production", None)
    wanted = set(((getattr(job, "wonders", None) or {}).get("list")) or [])
    ours = {c["producing"]["name"] for c in mine if c["producing"]["kind"] == "improvement"
            and c["producing"]["id"] >= WONDER_FIRST}
    wanted |= ours
    built = mw.wonders_built() if mw else {}
    cities_by_index = {c["index"]: c for c in st["cities"]}
    for wid, ci in built.items():
        name = r.improvement_name(wid)
        c = cities_by_index.get(ci)
        if name in wanted and c and c["owner"] != me:
            add("bad" if name in ours else "info", "wonder_lost",
                f"{name} was built by {next((x['name'] for x in st['civs'] if x['id'] == c['owner']), '?')} ({c['name']})",
                c["x"], c["y"])
    for c in st["cities"]:
        p = c["producing"]
        if c["owner"] == me or p["kind"] != "improvement" or p["id"] < WONDER_FIRST or p["id"] in built:
            continue
        if p["name"] in wanted:
            cost = _wonder_cost(r, p["id"])
            add("warn" if p["name"] in ours else "info", "wonder_race",
                f"{next((x['name'] for x in st['civs'] if x['id'] == c['owner']), '?')} is building {p['name']} "
                f"({c['name']}: {c['shields']}/{cost or '?'})", c["x"], c["y"])

    # --- money, government, jobs
    if civ["gold"] < 60:
        add("warn", "gold_low", f"Gold low: {civ['gold']}")
    if civ["government"] == "Anarchy":
        add("info", "anarchy", "Anarchy: tax and science stopped; next government will be "
            f"{', '.join(getattr(s, 'government_plan', []) or ['?'])}")
    camp = getattr(hub, "campaign", None)
    if camp is not None and camp.status in ("failed", "stopped"):
        add("bad", "job", f"Campaign stopped ({camp.status}): {camp.message or ''}")
    if camp is not None and camp.status == "needs_decision":
        add("bad", "job", f"Campaign is waiting for your decision: {camp.message or ''}")
    if job is not None and str(getattr(job, "status", "")).startswith("failed"):
        add("bad", "job", f"City manager stopped: {job.status}")
    if job is None or getattr(job, "status", None) != "running":
        add("info", "job", "City manager (city_manager) is not running")
    if st.get("popup"):
        add("info", "popup", f"Popup open: {st['popup'].get('title') or st['popup'].get('section') or ''}")

    order = {"bad": 0, "warn": 1, "info": 2}
    out.sort(key=lambda a: order.get(a["level"], 3))
    counts = {k: sum(1 for a in out if a["level"] == k) for k in order}
    return {"turn": st["turn"], "counts": counts, "alerts": out[:300], "total": len(out)}


def rivals(s, mw) -> dict:
    st = s.state()
    me = st["human_civ"]
    r = s.game.rules
    types = s.game.unit_types
    built = mw.wonders_built() if mw else {}
    cities_by_index = {c["index"]: c for c in st["cities"]}
    rows = []
    for civ in st["civs"]:
        if not civ["alive"]:
            continue
        cs = [c for c in st["cities"] if c["owner"] == civ["id"]]
        us = [u for u in st["units"] if u["owner"] == civ["id"]]
        army = [u for u in us if u["type"] < len(types) and types[u["type"]].get("attack")]
        wb = sorted(r.improvement_name(w) for w, ci in built.items()
                    if (cities_by_index.get(ci) or {}).get("owner") == civ["id"])
        wp = [{"city": c["name"], "wonder": c["producing"]["name"], "shields": c["shields"],
               "cost": _wonder_cost(r, c["producing"]["id"])}
              for c in cs if c["producing"]["kind"] == "improvement" and c["producing"]["id"] >= WONDER_FIRST
              and c["producing"]["id"] not in built]
        rows.append({"id": civ["id"], "name": civ["name"], "me": civ["id"] == me, "government": civ["government"],
                     "gold": civ["gold"], "techs": civ["techs"], "researching": civ["researching"],
                     "cities": len(cs), "population": sum(c["size"] for c in cs),
                     "largest": max((c["size"] for c in cs), default=0),
                     "units": len(us), "army": len(army),
                     "wonders_built": wb, "wonders_building": wp})
    return {"turn": st["turn"], "civs": rows}


def government(s, hub) -> dict:
    from .memwrite import MAX_RATE
    st = s.state()
    _, civ = _my(st)
    tax, sci = civ["tax_rate"], civ["science_rate"]
    job = getattr(hub, "production", None)
    return {"turn": st["turn"], "government": civ["government"], "plan": getattr(s, "government_plan", []),
            "rates": {"tax": tax, "lux": 10 - tax - sci, "sci": sci},
            "max_rate": MAX_RATE.get(civ["government"], 6),
            "revolution_turn": getattr(s, "revolution_turn", None),
            "rate_policy": {k: list(v) for k, v in (getattr(job, "gov_rates", None) or {}).items()},
            "money_rule": "gold < 60, or falling and < 200 → tax +10% / sci −10%; gold > 400 and rising → the reverse",
            "options": list(MAX_RATE)}


def research(s, mw, reports) -> dict:
    st = s.state()
    me, civ = _my(st)
    r = s.game.rules
    codes = getattr(r, "advance_codes", [])
    names = r.advances
    known = mw.known(me) if mw else set()
    prereq = getattr(r, "advance_prereq", [])
    by_name = {n: i for i, n in enumerate(names)}

    def ok(code):
        return code in ("nil", "", None) or code in known

    plan = []
    for n in getattr(s, "research_plan", []):
        i = by_name.get(n)
        if i is None:
            plan.append({"name": n, "status": "name not found"})
            continue
        code = codes[i] if i < len(codes) else None
        p1, p2 = prereq[i] if i < len(prereq) else ("", "")
        if code in known:
            status = "known"
        elif civ["researching"] == n:
            status = "researching"
        elif ok(p1) and ok(p2):
            status = "available"
        else:
            need = [names[codes.index(p)] for p in (p1, p2) if not ok(p) and p in codes]
            status = "needs: " + ", ".join(need)
        plan.append({"name": n, "status": status})
    hist = [{"turn": x["turn"], "beakers": x.get("beakers"), "researching": x.get("researching"),
             "techs": x.get("techs")} for x in reports if x.get("beakers") is not None]
    rate = None
    if len(hist) >= 2 and hist[-1]["researching"] == hist[-2]["researching"]:
        rate = hist[-1]["beakers"] - hist[-2]["beakers"]
    known_names = sorted(names[i] for i, c in enumerate(codes) if c in known and i < len(names))
    available = sorted(names[i] for i, c in enumerate(codes)
                       if c not in known and i < len(prereq) and ok(prereq[i][0]) and ok(prereq[i][1])
                       and not names[i].startswith(("Future", "Extra")))
    return {"turn": st["turn"], "researching": civ["researching"], "beakers": civ["beakers"], "techs": civ["techs"],
            "beakers_per_turn": rate, "science_rate": civ["science_rate"], "plan": plan,
            "known": known_names, "available": available, "history": hist[-30:]}


_SAVE_RE = re.compile(r"turn(\d+)_(start|end)_", re.I)


def saves_list(saves_dir: str, limit: int = 150) -> dict:
    out = []
    try:
        names = [n for n in os.listdir(saves_dir) if n.lower().endswith(".sav")]
    except OSError:
        names = []
    for n in names:
        p = os.path.join(saves_dir, n)
        try:
            stt = os.stat(p)
        except OSError:
            continue
        m = _SAVE_RE.search(n)
        kind = m.group(2).lower() if m else ("hunt" if n.lower().startswith(("hut_", "hunt")) else "other")
        out.append({"name": n, "turn": int(m.group(1)) if m else None, "kind": kind, "size": stt.st_size,
                    "mtime": stt.st_mtime, "time": time.strftime("%m-%d %H:%M:%S", time.localtime(stt.st_mtime))})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return {"count": len(out), "total_mb": round(sum(x["size"] for x in out) / 1e6, 1), "saves": out[:limit]}
