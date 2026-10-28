"""
Decision protocol: one line per action the agent takes, with the reason it was chosen.

Each entry: {seq, time, turn, actor, action, why, ...data}
  actor  = runner | campaign | hunt | city | research | save
  action = what was done (move, enter_village, reload, set_production, entertainer, ...)
  why    = the rule / numbers that made the agent choose it
Kept in memory (last 3000) and appended to logs/decisions.jsonl, so the whole game can be
replayed and audited later. GET /protocol?since=<seq> returns new entries.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque


class Journal:
    def __init__(self, path: str | None = None, keep: int = 3000):
        self.path = path
        self.items: deque = deque(maxlen=keep)
        self.seq = 0
        self._lock = threading.Lock()
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)

    def log(self, actor: str, action: str, why: str, turn=None, **data) -> dict:
        with self._lock:
            self.seq += 1
            e = {"seq": self.seq, "time": time.strftime("%H:%M:%S"), "turn": turn,
                 "actor": actor, "action": action, "why": why, **data}
            self.items.append(e)
            if self.path:
                try:
                    with open(self.path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(e, ensure_ascii=False) + "\n")
                except OSError:
                    pass
            return e

    def since(self, seq: int = 0, limit: int = 300) -> list[dict]:
        with self._lock:
            out = [e for e in self.items if e["seq"] > seq]
        return out[-limit:]


def note(session, actor: str, action: str, why: str, **data):
    """Log through the session's journal if it has one (never raises)."""
    j = getattr(session, "journal", None)
    if j is None:
        return None
    try:
        turn = (session.state() or {}).get("turn")
    except Exception:
        turn = None
    try:
        return j.log(actor, action, why, turn=turn, **data)
    except Exception:
        return None
