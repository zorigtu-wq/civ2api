"""
MVP step 2: move the ACTIVE unit one tile by keystroke and confirm in memory.

    python mvp_move.py NE                 # PostMessage (game may stay in background)
    python mvp_move.py NE --keys sendinput  # if PostMessage is ignored by the game

Pass/fail rules:
  PASS  unit x/y changed by the expected delta, or moves_spent increased
        (e.g. move was blocked by terrain/enemy but the key was received)
  FAIL  nothing changed in memory -> try --keys sendinput
"""
import argparse
import time

from civ2api.session import DIR_DELTA, Session
from civ2api.state import Civ2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir", choices=list(DIR_DELTA))
    ap.add_argument("--keys", choices=["post", "sendinput"], default="post")
    a = ap.parse_args()

    g = Civ2.attach()
    s = Session(g, key_method=a.keys)
    st = s.state()
    au = s._active_unit(st)
    if st["popup"]:
        print("A popup is open, close it first:", st["popup"]["title"], st["popup"]["lines"])
        return
    if not au:
        print("No active unit (unit_selected =", st["unit_selected"], "). Select a unit in game.")
        return
    print(f"before: #{au['id']} {au['type_name']} at ({au['x']},{au['y']}) "
          f"spent={au['moves_spent_raw']} moves_left={au['moves_left']}")
    r = s.act({"action": "move", "dir": a.dir})
    time.sleep(0.3)
    s.refresh()
    after = next((u for u in s.state()["units"] if u["id"] == au["id"]), None)
    print("keys sent:", r.get("keys"), "via", a.keys)
    for ev in r["events"]:
        print("  event:", {k: v for k, v in ev.items() if k not in ("t",)})
    if after is None:
        print("RESULT: unit no longer exists (killed / disbanded?) -> key was received")
        return
    dx, dy = DIR_DELTA[a.dir]
    print(f"after:  #{after['id']} at ({after['x']},{after['y']}) "
          f"spent={after['moves_spent_raw']} moves_left={after['moves_left']}")
    moved = (after["x"] - au["x"], after["y"] - au["y"])
    if moved != (0, 0):
        ok = moved == (dx, dy) or abs(moved[0]) > 2  # map wrap gives large dx
        print("RESULT: PASS - unit moved", moved, "" if ok else f"(expected {(dx, dy)})")
    elif after["moves_spent_raw"] != au["moves_spent_raw"]:
        print("RESULT: PASS (partial) - key received, moves spent changed but tile did not")
    elif r.get("popup"):
        print("RESULT: popup appeared:", r["popup"]["title"], r["popup"]["lines"])
    else:
        print("RESULT: FAIL - no change in memory. Try --keys sendinput")


if __name__ == "__main__":
    main()
