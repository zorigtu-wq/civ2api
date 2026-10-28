"""
MVP step 1: show my units live from the running game.

    python mvp_units.py            # one snapshot
    python mvp_units.py --watch    # refresh every second, print events

Also prints the raw values that still need runtime confirmation
(move/HP scale, human civ fields, turn-civ guess) so we can fix memory-map.md.
"""
import argparse
import time

from civ2api import events as E
from civ2api.state import Civ2


def table(st):
    me = st["human_civ"]
    mine = [u for u in st["units"] if u["owner"] == me]
    print(f"\nturn {st['turn']}  human_civ={me} (game byte {st['human_civ_game']})  "
          f"turn_civ_guess={st['turn_civ_guess']}  active_unit_index={st['active_unit']}  "
          f"units total={len(st['units'])} mine={len(mine)}")
    print(f"{'idx':>4} {'id':>5} {'type':<14} {'x':>4} {'y':>4} {'hp':>7} {'moves':>6} "
          f"{'spent':>5} {'vet':>3} orders")
    for u in mine:
        act = "*" if u["index"] == st["active_unit"] else " "
        print(f"{u['index']:>4}{act}{u['id']:>5} {u['type_name']:<14} {u['x']:>4} {u['y']:>4} "
              f"{u['hp']!s:>3}/{u['hp_max']!s:<3} {u['moves_left']!s:>6} {u['moves_spent_raw']:>5} "
              f"{'V' if u['veteran'] else '':>3} {u['orders'] or ''}")
    if st["popup"]:
        p = st["popup"]
        print(f"\nPOPUP: {p['title']!r}  section={p['section']!r}")
        for line in p["lines"]:
            print("   ", line)
        for o in p["options"]:
            print(f"   [{o['index']}] {o['text']}")
        print("    buttons:", [b["text"] for b in p["buttons"]])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--rules")
    a = ap.parse_args()
    g = Civ2.attach(rules_path=a.rules)
    print(f"attached pid={g.p.pid}  exe={g.p.image_path()}")
    print(f"units_base=0x{g.units_base:08X}  road_mult={g.road_mult}  "
          f"move_scaled={g.move_scaled}  hp_scaled={g.hp_scaled}  rules={g.rules.source}")
    for w in g.warnings:
        print("WARNING:", w)
    print("unit types (first 8):",
          [(t["name"], t["move_raw"], t["hp_raw"]) for t in g.unit_types[:8]])
    prev = None
    while True:
        st = g.read_state()
        if prev is None or not a.watch:
            table(st)
        else:
            for ev in E.diff(prev, st):
                print(ev)
        prev = st
        if not a.watch:
            break
        time.sleep(1.0)


if __name__ == "__main__":
    main()
