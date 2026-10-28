"""
City operations through the game's own windows (no screenshots, no memory writes).

Open a city:   Advisors/City Status (F1) lists our cities in city-table order; a posted
               click on a row opens that City Window (seen live, turn 25).
Change build:  the City Window's "Change" button (top-right of the two buttons above the
               build box) opens the PRODUCTION popup, a list box whose items we cannot
               read. The list is sorted units (by id) then improvements/wonders (by id), so
               we step through it: select row i, OK, read what the city now builds from
               memory, repeat. This also tells whether an item is available at all.
Layout numbers below are fractions of the window size, measured on a 622x421
City Status window and a 976x681 City Window (UI Additions scaling).
"""
from __future__ import annotations

import time

from . import win32
from .journal import note as J

STATUS_ROW0, STATUS_ROW_H, STATUS_NAME_X = 107 / 421, 24.1 / 421, 180 / 622
STATUS_ROWS = 11


def _area(w):
    return (w.rect[2] - w.rect[0]) * (w.rect[3] - w.rect[1])


def _inside(w, r):
    return r[0] <= w.rect[0] and w.rect[2] <= r[2] and r[1] <= w.rect[1] and w.rect[3] <= r[3]


class CityOps:
    def __init__(self, session):
        self.s = session
        self.g = session.game
        self.status_hwnd = None
        self.log: list[str] = []

    def _note(self, m):
        self.log.append(time.strftime("%H:%M:%S ") + m)
        self.log[:] = self.log[-60:]

    # ------------------------------------------------------------------ windows
    def _kids(self):
        return [w for w in win32.child_windows(self.s.main_hwnd()) if w.visible]

    def _close_city_window(self, cw: int | None = None):
        """ESC on the main window closes the City Window. Focus is not a reliable sign that
        it is open (seen live: focus back on City Status while the window stayed up), so
        a known City Window hwnd is checked for visibility."""
        vis = {w.hwnd for w in self._kids() if w.cls == "MSWindowClass"}
        open_ = (cw in vis) if cw else (self.s.city_window() and self.s.city_window() != self.status_hwnd)
        if open_:
            self.s.press("ESC", target=self.s.main_hwnd())
            time.sleep(0.5)

    def status_window(self) -> int:
        kids = {w.hwnd for w in self._kids() if w.cls == "MSWindowClass"}
        if self.status_hwnd in kids:
            return self.status_hwnd
        self.s.menu("City Status")
        for _ in range(20):                   # can take a while with many cities
            time.sleep(0.25)
            new = [w for w in self._kids() if w.cls == "MSWindowClass" and w.hwnd not in kids]
            if new:
                self.status_hwnd = max(new, key=_area).hwnd
                return self.status_hwnd
        raise RuntimeError("City Status window did not open")

    CITY_WINDOW_INDEX = 0x006AA754     # TCityWindow(0x6A91B8)+0x159C: index of the open city

    def _open_index(self) -> int:
        return self.g.p.i32(self.CITY_WINDOW_INDEX)

    def _arrows(self, cw: int):
        """The two narrow buttons left of Info/Happy: up = previous city, down = next city."""
        cwin = next(x for x in self._kids() if x.hwnd == cw)
        # only buttons (the improvements list's scrollbar is just as narrow, seen live)
        arr = sorted([w for w in self._kids() if w.cls == "MSControlClass" and _inside(w, cwin.rect)
                      and w.rect[2] - w.rect[0] < 40 and w.rect[3] - w.rect[1] > 30
                      and w.rect[1] > cwin.rect[1] + 300], key=lambda w: w.rect[1])
        if len(arr) < 2:
            raise RuntimeError("city arrows not found")
        return arr[-2].hwnd, arr[-1].hwnd

    def open_city(self, city: dict) -> int:
        """City Status row (only the first STATUS_ROWS rows are visible), then the City
        Window's next/previous arrows until the open city index (memory) is the one we want."""
        st = self.s.state()
        own = [c for c in st["cities"] if c["owner"] == st["human_civ"]]
        row = next(i for i, c in enumerate(own) if c["id"] == city["id"])
        self.s.ensure_restored()
        self._close_city_window()
        hw = self.status_window()
        w = next(x for x in self._kids() if x.hwnd == hw)
        W, H = w.rect[2] - w.rect[0], w.rect[3] - w.rect[1]
        r = min(row, STATUS_ROWS - 1)
        win32.click_window(hw, int(STATUS_NAME_X * W), int((STATUS_ROW0 + r * STATUS_ROW_H) * H))
        cw = None
        for _ in range(15):
            time.sleep(0.2)
            cw = self.s.city_window()
            if cw and cw != hw:
                break
        else:
            raise RuntimeError(f"City Window for {city['name']} did not open")
        # The arrow order is the game's own (not always the table order, seen live with
        # Baltimore), so just keep stepping one way until the open index is the city.
        seen = set()
        for _ in range(2 * len(own) + 5):
            cur = self._open_index()
            if cur == city["index"]:
                return cw
            seen.add(cur)
            up, down = self._arrows(cw)
            win32.click_window(down)
            for _ in range(8):                   # wait until the window shows another city
                time.sleep(0.15)
                if self._open_index() != cur:
                    break
        raise RuntimeError(f"could not step the City Window to {city['name']}")

    def change_button(self, cw: int) -> int:
        cwin = next(x for x in self._kids() if x.hwnd == cw)
        btns = [w for w in self._kids() if w.cls == "MSControlClass" and _inside(w, cwin.rect)
                and w.rect[2] - w.rect[0] > 60]
        top = min(b.rect[1] for b in btns)
        row = [b for b in btns if abs(b.rect[1] - top) < 5]
        return max(row, key=lambda b: b.rect[0]).hwnd        # Buy | Change -> Change

    # ------------------------------------------------------------------ production
    def _key(self, prod: dict) -> tuple:
        return (0 if prod["kind"] == "unit" else 1, prod["id"])

    def item_key(self, name: str) -> tuple:
        r = self.g.rules
        for i in range(len(r.improvements)):
            if r.improvement_name(i).lower() == name.lower():
                return (1, i)
        for t in range(len(self.g.unit_types)):
            if r.unit_name(t).lower() == name.lower():
                return (0, t)
        raise ValueError(f"unknown item {name!r}")

    def _city(self, cid):
        self.s.refresh()
        return next(c for c in self.s.state()["cities"] if c["id"] == cid)

    def _open_production(self, cw):
        win32.click_window(self.change_button(cw))
        for _ in range(15):
            time.sleep(0.2)
            self.s.refresh()
            p = self.s.state().get("popup")
            if p and p.get("section") == "PRODUCTION":
                return p
        raise RuntimeError("PRODUCTION popup did not open")

    def set_production(self, city_id: int, names: list[str]) -> dict:
        """Make the city build the first of `names` that its PRODUCTION list offers."""
        with self.s._lock:
            city = self._city(city_id)
            if city["producing"]["name"].lower() == names[0].lower():
                return {"ok": True, "city": city["name"], "producing": city["producing"]["name"],
                        "changed": False}
            cw = self.open_city(city)
            try:
                p = self._open_production(cw)
                rows = p.get("list") or []
                avail = [r["name"] for r in rows]
                pick = next((r for n in names for r in rows if r["name"].lower() == n.lower()), None)
                J(self.s, "city", "set production",
                  f"{city['name']}: policy — city without a Temple builds a Temple, city with one builds a Caravan (else the next option); "
                  f"priority for this city: {names}. Available: "
                  f"{', '.join(avail)} → " + (f"picked {pick['name']}" if pick else "nothing suitable"),
                  city=city["name"], was=city["producing"]["name"])
                if pick is None or pick["selected"]:
                    self.s.answer_dialog(button="cancel")
                else:
                    r = self.s.answer_dialog(option=pick["index"], button="ok")
                    if not r.get("ok"):
                        raise RuntimeError(r.get("error"))
            finally:
                time.sleep(0.3)
                self.s.refresh()
                if self.s.state().get("popup"):
                    self.s.answer_dialog(button="cancel")
                self._close_city_window(cw)
                self.city_hwnd = cw
            city = self._city(city_id)
        ok = pick is not None and city["producing"]["name"].lower() == pick["name"].lower()
        res = {"ok": ok, "city": city["name"], "producing": city["producing"]["name"],
               "changed": pick is not None, "available": avail}
        self._note(f"{city['name']}: {res['producing']}" + ("" if pick else f" ({names} not in the list)"))
        return res

    # ------------------------------------------------------------------ citizens
    # Resource Map geometry, measured on a 976x681 City Window: centre tile at (159, 237),
    # one doubled-x step = 38 px, one y step = 19 px (clicks verified on all visible tiles).
    RES_CX, RES_CY, RES_UX, RES_UY, CW_W, CW_H = 159, 237, 38, 19, 976, 681

    def tile_pixel(self, cw: int, dx: int, dy: int) -> tuple[int, int]:
        w = next(x for x in self._kids() if x.hwnd == cw)
        sx, sy = (w.rect[2] - w.rect[0]) / self.CW_W, (w.rect[3] - w.rect[1]) / self.CW_H
        return (int((self.RES_CX + self.RES_UX * dx) * sx), int((self.RES_CY + self.RES_UY * dy) * sy))

    def fix_disorder(self, city_id: int) -> dict:
        """While unhappy > happy, turn a worker into an entertainer (click its tile in the
        Resource Map). Workers on the weakest tiles go first is not modelled: highest tile
        index first (outer ring before inner ring)."""
        from . import memmap as M
        with self.s._lock:
            city = self._city(city_id)
            if city["unhappy"] <= city["happy"]:
                return {"ok": True, "city": city["name"], "changed": 0}
            cw = self.open_city(city)
            changed = 0
            try:
                for _ in range(city["size"]):
                    c = self._city(city_id)
                    if c["unhappy"] <= c["happy"]:
                        break
                    workers = [i for i in c["worked_tiles"] if i != 20]
                    if not workers:
                        break
                    i = max(workers)
                    J(self.s, "city", "worker → entertainer",
                      f"{c['name']}: unhappy {c['unhappy']} > happy {c['happy']} → the city will riot at end of turn. "
                      f"Tile {i} {M.CITY_TILES[i]} worker made an entertainer (+2 luxury). Rule: while unhappy "
                      f"outnumber happy, do not put citizens back to work", city=c["name"])
                    win32.click_window(cw, *self.tile_pixel(cw, *M.CITY_TILES[i]))
                    time.sleep(0.6)
                    if i in self._city(city_id)["worked_tiles"]:
                        raise RuntimeError(f"tile {i} click did not free the worker")
                    changed += 1
            finally:
                self._close_city_window(cw)
                self.city_hwnd = cw
            c = self._city(city_id)
        self._note(f"{c['name']}: added {changed} entertainer(s) (unhappy {c['unhappy']}, happy {c['happy']})")
        return {"ok": c["unhappy"] <= c["happy"], "city": c["name"], "changed": changed,
                "unhappy": c["unhappy"], "happy": c["happy"], "entertainers": c["entertainers"]}

    def close_status(self):
        self._close_city_window(getattr(self, "city_hwnd", None))
        if self.status_hwnd and any(w.hwnd == self.status_hwnd for w in self._kids()):
            self.s.press("ESC", target=self.status_hwnd)
            time.sleep(0.4)
        self.status_hwnd = None


class ProductionJob:
    """Set production in many cities; with watch=True re-try every new turn until every
    city builds items[0] (e.g. ["Temple", "Barracks"]: Barracks now, Temple once known)."""

    def __init__(self, session, ops: CityOps, city_ids, items, watch=False, happy=False, after=None):
        import threading
        self.s, self.ops, self.items, self.watch, self.happy = session, ops, items, watch, happy
        self.happy_results = {}
        self.after = after            # {"built": "Temple", "items": ["Caravan", "Barracks"]}
        self.busy = False
        self.pause = lambda: False
        self.city_ids = city_ids
        self.status, self.results, self.last_turn = "running", {}, None
        self._cancel = threading.Event()
        self._thread = threading.Thread(target=self._run, name="production", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def cancel(self):
        self._cancel.set()

    def info(self):
        return {"status": self.status, "items": self.items, "after": self.after, "watch": self.watch,
                "happy": self.happy,
                "happy_results": self.happy_results,
                "results": self.results, "last_turn": self.last_turn, "log": self.ops.log[-20:]}

    def _ids(self, st):
        own = [c["id"] for c in st["cities"] if c["owner"] == st["human_civ"]]
        return own if self.city_ids in (None, "all") else [i for i in self.city_ids if i in own]

    def _pass(self):
        self.busy = True
        try:
            return self._pass_inner()
        finally:
            self.busy = False

    def _pass_inner(self):
        self.s.refresh()
        st = self.s.state()
        if st.get("popup"):
            return False
        avail_seen: dict[str, list] = {}       # rule name -> production list seen this pass
        for cid in self._ids(st):
            if self._cancel.is_set():
                break
            c = next(x for x in st["cities"] if x["id"] == cid)
            # per-city rule: after the city owns `after.built`, it builds `after.items`
            rule, items = "base", list(self.items)
            if self.after and self.after.get("built") in c.get("improvement_names", []):
                rule, items = "after", list(self.after["items"])
            cur = c["producing"]["name"].lower()
            if cur == items[0].lower():
                self.results[c["name"]] = c["producing"]["name"]
                continue
            seen = avail_seen.get(rule)
            if seen is not None and items[0].lower() not in seen and len(items) > 1 \
                    and cur == items[1].lower():
                self.results[c["name"]] = c["producing"]["name"]     # fallback already set
                continue
            try:
                r = self.ops.set_production(cid, items)
                self.results[c["name"]] = r["producing"]
                if r.get("available") is not None:
                    avail_seen[rule] = [a.lower() for a in r["available"]]
            except Exception as e:
                self.results[c["name"]] = f"error: {e}"
                self.ops._note(f"{c['name']}: {e}")
        if self.happy:
            self.s.refresh()
            st2 = self.s.state()
            for cid in self._ids(st2):
                if self._cancel.is_set():
                    break
                c = next(x for x in st2["cities"] if x["id"] == cid)
                if c["unhappy"] <= c["happy"]:
                    continue
                try:
                    r = self.ops.fix_disorder(cid)
                    self.happy_results[c["name"]] = f"+{r['changed']} entertainer"
                except Exception as e:
                    self.happy_results[c["name"]] = f"error: {e}"
                    self.ops._note(f"{c['name']}: {e}")
        self.ops.close_status()
        self.last_turn = st["turn"]
        return True

    def blocking(self, st) -> bool:
        me = st["human_civ"]
        return any(c["owner"] == me and c["unhappy"] > c["happy"] for c in st["cities"])

    def _run(self):
        try:
            while not self._cancel.is_set():
                if self.pause():                 # a village hunt (save/enter/load) is running
                    self._cancel.wait(2)
                    continue
                st = self.s.state() or {}
                if self.happy and self.last_turn == st.get("turn") and not st.get("popup") \
                        and st.get("turn_civ_guess") == st.get("human_civ"):
                    # mid-turn: a unit left a city (martial law) or a city grew -> fix it now,
                    # so no city is in disorder when the turn ends
                    bad = [c for c in st.get("cities", []) if c["owner"] == st["human_civ"]
                           and c["unhappy"] > c["happy"]]
                    self.busy = bool(bad)
                    for c in bad:
                        try:
                            r = self.ops.fix_disorder(c["id"])
                            self.happy_results[c["name"]] = f"+{r['changed']} entertainer"
                        except Exception as e:
                            self.ops._note(f"{c['name']}: {e}")
                    if bad:
                        self.ops.close_status()
                    self.busy = False
                if self.last_turn != st.get("turn"):
                    done = self._pass()
                    if done and not self.watch:
                        break
                    top = self.items[0]
                    if self.watch and not self.happy and all(v == top for v in self.results.values()) \
                            and self.results:
                        break
                self._cancel.wait(5)
            self.status = "cancelled" if self._cancel.is_set() else "done"
        except Exception as e:
            self.status = f"failed: {type(e).__name__}: {e}"
