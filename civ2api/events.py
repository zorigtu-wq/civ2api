"""
Turn two consecutive state snapshots into a list of events.

A snapshot is the dict produced by state.read_state():
  {"turn": int, "year": int, "active_civ": int, "human_civ": int,
   "units":  [{"id", "owner", "type", "type_name", "x", "y", "hp", "damage", "moves_left", ...}],
   "cities": [{"id", "owner", "name", "x", "y", "size", ...}],
   "civs":   [{"id", "alive", ...}]}
Only these keys are relied on, so the diff survives memory-map changes elsewhere.
"""
from __future__ import annotations

import itertools
import threading
import time
from collections import deque

_seq = itertools.count(1)


def _ev(kind: str, **data) -> dict:
    return {"seq": next(_seq), "t": round(time.time(), 3), "kind": kind, **data}


def _by_id(items):
    return {it["id"]: it for it in items}


def diff(prev: dict | None, cur: dict) -> list[dict]:
    if prev is None:
        return [_ev("snapshot", turn=cur.get("turn"), units=len(cur["units"]),
                    cities=len(cur["cities"]))]
    me = cur.get("human_civ")
    out: list[dict] = []

    if cur.get("turn") != prev.get("turn"):
        out.append(_ev("turn_changed", old=prev.get("turn"), new=cur.get("turn"),
                       year=cur.get("year")))
    if cur.get("active_civ") != prev.get("active_civ"):
        out.append(_ev("active_civ_changed", old=prev.get("active_civ"),
                       new=cur.get("active_civ")))
    if cur.get("active_unit") != prev.get("active_unit"):
        out.append(_ev("active_unit_changed", old=prev.get("active_unit"),
                       new=cur.get("active_unit")))

    pu, cu = _by_id(prev["units"]), _by_id(cur["units"])
    for uid in cu.keys() - pu.keys():
        u = cu[uid]
        kind = "my_unit_created" if u["owner"] == me else "foreign_unit_appeared"
        out.append(_ev(kind, unit=u))
    for uid in pu.keys() - cu.keys():
        u = pu[uid]
        kind = "my_unit_lost" if u["owner"] == me else "foreign_unit_gone"
        out.append(_ev(kind, unit=u))
    for uid in cu.keys() & pu.keys():
        a, b = pu[uid], cu[uid]
        if a["type"] != b["type"] or a["owner"] != b["owner"]:
            # slot was reused for a different unit
            out.append(_ev("unit_slot_reused", old=a, new=b))
            continue
        if (a["x"], a["y"]) != (b["x"], b["y"]):
            out.append(_ev("unit_moved", id=uid, owner=b["owner"], type=b.get("type_name"),
                           frm=[a["x"], a["y"]], to=[b["x"], b["y"]]))
        if b.get("hp") is not None and a.get("hp") is not None and b["hp"] < a["hp"]:
            out.append(_ev("unit_damaged", id=uid, owner=b["owner"], type=b.get("type_name"),
                           hp_old=a["hp"], hp_new=b["hp"], at=[b["x"], b["y"]]))
        if b.get("veteran") and not a.get("veteran"):
            out.append(_ev("unit_promoted", id=uid, owner=b["owner"]))

    pc, cc = _by_id(prev["cities"]), _by_id(cur["cities"])
    for cid in cc.keys() - pc.keys():
        out.append(_ev("city_founded", city=cc[cid]))
    for cid in pc.keys() - cc.keys():
        out.append(_ev("city_destroyed", city=pc[cid]))
    for cid in cc.keys() & pc.keys():
        a, b = pc[cid], cc[cid]
        if a["owner"] != b["owner"]:
            kind = ("city_captured" if b["owner"] == me else
                    "city_lost" if a["owner"] == me else "city_changed_owner")
            out.append(_ev(kind, id=cid, name=b["name"], old_owner=a["owner"],
                           new_owner=b["owner"]))
        if a.get("size") != b.get("size"):
            out.append(_ev("city_size_changed", id=cid, name=b["name"], owner=b["owner"],
                           old=a.get("size"), new=b.get("size")))
        if a.get("producing") != b.get("producing"):
            out.append(_ev("city_production_changed", id=cid, name=b["name"],
                           old=a.get("producing"), new=b.get("producing")))

    pv = {c["id"]: c for c in prev.get("civs", [])}
    for c in cur.get("civs", []):
        if pv.get(c["id"], {}).get("alive") and not c.get("alive"):
            out.append(_ev("civ_destroyed", id=c["id"], name=c.get("name")))
    return out


class EventLog:
    """Thread-safe ring buffer of events with monotonically increasing seq."""

    def __init__(self, maxlen: int = 5000):
        self._buf: deque[dict] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def extend(self, events: list[dict]) -> None:
        with self._lock:
            self._buf.extend(events)

    def since(self, seq: int = 0) -> list[dict]:
        with self._lock:
            return [e for e in self._buf if e["seq"] > seq]

    def last_seq(self) -> int:
        with self._lock:
            return self._buf[-1]["seq"] if self._buf else 0
