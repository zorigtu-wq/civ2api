"""
Buying production with gold through the game's own City Window "Buy" button (no memory
write: the game itself charges the treasury and fills the shield box).

  buy(session, city_index, max_price, dry=False)
     open the City Window (View Pieces -> cursor -> Center View -> click), check the open
     city index (0x006AA754), click Buy (left of the two wide buttons on the top row),
     read the price from the popup, answer Yes only if price <= max_price (dry: always No),
     close the City Window.

  estimate(remaining, has_shields, wonder)
     Civ2 price rule (2*R + R^2/20, x2 for Wonders, x2 when the box is empty); only used
     to pre-filter, the price in the popup is what decides.
"""
from __future__ import annotations

import re
import time

from . import win32
from .journal import note as J

CITY_WINDOW_INDEX = 0x006AA754


def estimate(remaining: int, has_shields: bool = True, wonder: bool = False) -> int:
    """Seen live (MGE): Michelangelo's 262/400 -> 552 gold = 4 x 138 remaining shields.
    Improvements 2 x remaining, Wonders 4 x, x2 when the box is still empty."""
    p = max(0, remaining) * (4 if wonder else 2)
    return p * (1 if has_shields else 2)


def _buy_button(s, cw):
    kids = [w for w in win32.child_windows(s.main_hwnd()) if w.visible]
    cwin = next((x for x in kids if x.hwnd == cw), None)
    if cwin is None:
        return None
    inside = lambda w: (w.rect[0] >= cwin.rect[0] and w.rect[2] <= cwin.rect[2]
                        and w.rect[1] >= cwin.rect[1] and w.rect[3] <= cwin.rect[3])
    btns = [w for w in kids if w.cls == "MSControlClass" and inside(w) and w.rect[2] - w.rect[0] > 60]
    if not btns:
        return None
    top = min(b.rect[1] for b in btns)
    row = [b for b in btns if abs(b.rect[1] - top) < 5]
    return min(row, key=lambda b: b.rect[0]).hwnd            # Buy | Change -> Buy


def _price(popup) -> int | None:
    text = " ".join(popup.get("lines") or [])
    nums = [int(n) for n in re.findall(r"\d+", text.replace(",", ""))]
    return nums[0] if nums else None


def buy(s, city_index: int, max_price: int, dry: bool = False, why: str = "") -> dict:
    st = s.state()
    c = next((x for x in st["cities"] if x["index"] == city_index), None)
    if c is None:
        return {"ok": False, "error": "city not found"}
    if st.get("popup"):
        return {"ok": False, "error": "a popup is open"}
    s.ensure_restored()
    gold0 = next(x for x in st["civs"] if x["id"] == st["human_civ"])["gold"]
    shields0 = c["shields"]
    cw0 = s.city_window()
    if cw0 and s.game.p.i32(CITY_WINDOW_INDEX) == city_index:
        r = {"ok": True, "city_window": cw0}            # already open on this city
    else:
        r = s.find_city(c["id"])                     # Shift+C (Find City) by unique name
        if r.get("ok") and s.game.p.i32(CITY_WINDOW_INDEX) != city_index:
            s.press("ESC", target=s.main_hwnd())
            time.sleep(0.5)
            r = {"ok": False}
        if not r.get("ok"):
            r = s.open_city(c["id"])
        if r.get("ok") and s.game.p.i32(CITY_WINDOW_INDEX) != city_index:
            s.press("ESC", target=s.main_hwnd())     # the map click opened a neighbour city
            time.sleep(0.5)
            r = {"ok": False}
    if not r.get("ok"):
        # no City Status fallback: it opened F1 on top of a City Window and the game then looped
        # "You must close the City Window" (seen live, turn 120)
        s.refresh()
        if s.city_window():
            s.press("ESC", target=s.main_hwnd())
            time.sleep(0.4)
        return {"ok": False, "error": f"City Window did not open: {r.get('error', '')}", "step": "open_city"}
    out = {"ok": False, "city": c["name"], "producing": c["producing"]["name"], "bought": False}
    with s._lock:
        try:
            if s.game.p.i32(CITY_WINDOW_INDEX) != city_index:
                out["error"] = "City Window shows another city"
                return out
            b = _buy_button(s, r["city_window"])
            if not b:
                out["error"] = "Buy button not found"
                return out
            win32.click_window(b)
            p = None
            for _ in range(15):
                time.sleep(0.2)
                s.refresh()
                p = s.state().get("popup")
                if p:
                    break
            if not p:
                out["error"] = "no popup after Buy"
                return out
            out["popup"] = {"section": p.get("section"), "title": p.get("title"), "lines": p.get("lines"),
                            "options": [o.get("text") for o in p.get("options") or []],
                            "list": [x.get("text") for x in p.get("list") or []],
                            "buttons": [x.get("text") for x in p.get("buttons") or []]}
            price = _price(p)
            out["price"] = price
            # COMPLETE1 popup (seen live): list rows "Complete it." / "Never mind."
            opts = [(o.get("index"), (o.get("text") or "").lower()) for o in (p.get("list") or [])
                    if (o.get("text") or "").strip()] or \
                   [(o.get("index"), (o.get("text") or "").lower()) for o in (p.get("options") or [])]
            yes = next((i for i, t in opts if "complete" in t or "yes" in t or "buy" in t), None)
            no = next((i for i, t in opts if t.startswith("no") or "never" in t), None)
            if price is not None and price <= max_price and not dry and yes is not None:
                s.answer_dialog(option=yes, button="ok")
                out["bought"] = True
            elif no is not None:
                s.answer_dialog(option=no, button="ok")
            else:
                s.answer_dialog(button="cancel")
            time.sleep(0.5)
            s.refresh()
            if s.state().get("popup"):
                s.answer_dialog(button="ok")
                time.sleep(0.3)
        finally:
            s.refresh()
            if s.city_window():
                s.press("ESC", target=s.main_hwnd())
                time.sleep(0.4)
    s.refresh()
    st = s.state()
    c2 = next(x for x in st["cities"] if x["index"] == city_index)
    gold1 = next(x for x in st["civs"] if x["id"] == st["human_civ"])["gold"]
    out.update(ok=True, gold_before=gold0, gold_after=gold1, shields_before=shields0, shields_after=c2["shields"])
    if out["bought"]:
        J(s, "city", "buy", f"{c['name']}: bought {c['producing']['name']} for {price} gold "
          f"(gold {gold0}→{gold1}). {why}", city=c["name"], price=price)
    return out
