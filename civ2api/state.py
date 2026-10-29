"""
Live, read-only view of a running CIV2.EXE (MGE 5.4.0f).

    g = Civ2.attach()           # finds civ2.exe, checks exe size, resolves units base
    s = g.read_state()          # dict: turn, civs, units, cities, popup ...
    g.read_tiles(x0, y0, w, h)  # map window

Nothing here writes to the game process.
"""
from __future__ import annotations

import struct
from pathlib import Path

from . import memmap as M
from . import win32
from .rules import Rules, load_game_txt


def _i16(b, o): return struct.unpack_from("<h", b, o)[0]
def _u16(b, o): return struct.unpack_from("<H", b, o)[0]
def _i32(b, o): return struct.unpack_from("<i", b, o)[0]
def _u32(b, o): return struct.unpack_from("<I", b, o)[0]
def _i8(b, o): return struct.unpack_from("<b", b, o)[0]


def _cstr(b: bytes) -> str:
    return b.split(b"\0", 1)[0].decode("cp1252", "replace")


def pe_read_va(exe_path: str, va: int, size: int) -> bytes:
    """Read bytes at a virtual address from the PE file on disk (32-bit PE)."""
    data = Path(exe_path).read_bytes()
    pe = _u32(data, 0x3C)
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError("not a PE file")
    nsec = _u16(data, pe + 6)
    opt_size = _u16(data, pe + 20)
    opt = pe + 24
    image_base = _u32(data, opt + 28)
    rva = va - image_base
    sec = opt + opt_size
    for i in range(nsec):
        s = sec + 40 * i
        vsize, vaddr, rsize, rptr = (_u32(data, s + 8), _u32(data, s + 12),
                                     _u32(data, s + 16), _u32(data, s + 20))
        if vaddr <= rva < vaddr + max(vsize, rsize):
            off = rptr + (rva - vaddr)
            return data[off:off + size]
    raise ValueError(f"VA 0x{va:08X} not in any section")


def hut_pattern(x: int, y: int, seed: int) -> bool:
    """Civ2 places goody huts by formula, not by a tile flag (Civ2-clone Tile.cs,
    after the Apolyton 'hut pattern' threads). x, y are Civ2 doubled coordinates."""
    n_sum = (x + y) // 2
    n_diff = ((x - y) // 2 + 4096) % 4096
    h = (n_sum // 4 * 11) + (n_diff // 4 * 13 + 8)
    return (h + seed) % 32 == (n_sum % 4) + (n_diff % 4) * 4


class Civ2:
    def __init__(self, proc: win32.ProcessReader, rules: Rules | None, units_base: int,
                 warnings: list[str]):
        self.p = proc
        self.rules = rules or Rules()
        self.units_base = units_base
        self.units_relocated = units_base != M.UNITS
        self.warnings = warnings
        self.gametxt: dict = {}
        self._scales()

    # ------------------------------------------------------------------ attach
    @classmethod
    def attach(cls, pid: int | None = None, rules_path: str | None = None,
               strict_size: bool = True) -> "Civ2":
        if pid is None:
            pids = win32.find_pids("civ2.exe")
            if not pids:
                raise RuntimeError("CIV2.EXE is not running")
            # several civ2.exe can exist (e.g. a stale one): take one we can open
            # that owns a visible window; newest pid last
            errors, proc = [], None
            for cand in sorted(pids, key=lambda x: -len(win32.top_windows(x))):
                try:
                    proc = win32.ProcessReader(cand)
                    break
                except OSError as e:
                    errors.append(f"{cand}: {e}")
            if proc is None:
                raise RuntimeError("cannot open any civ2.exe: " + "; ".join(errors))
        else:
            proc = win32.ProcessReader(pid)
        warnings: list[str] = []
        exe = proc.image_path()
        size = Path(exe).stat().st_size
        if size != M.EXPECTED_EXE_SIZE:
            msg = (f"{exe} is {size:,} bytes, expected {M.EXPECTED_EXE_SIZE:,} "
                   "(MGE 5.4.0f). Addresses will be wrong.")
            if strict_size:
                proc.close()
                raise RuntimeError(msg)
            warnings.append(msg)

        # units array may have been moved by Civ2UIA's units-limit patch
        units_base = M.UNITS
        try:
            mem = proc.u32(M.UNITS_REF_OPERAND)
            disk = _u32(pe_read_va(exe, M.UNITS_REF_OPERAND, 4), 0)
            delta = (mem - disk) & 0xFFFFFFFF
            if delta:
                units_base = (M.UNITS + delta) & 0xFFFFFFFF
                warnings.append(f"units array relocated by Civ2UIA: 0x{units_base:08X}")
        except Exception as e:  # keep going with the default base
            warnings.append(f"could not check units relocation: {e}")

        rules = None
        try:
            rules = Rules.load(Path(exe).parent)
        except Exception as e:
            warnings.append(f"RULES.TXT not loaded ({e}); unit names will be numeric")
        if rules_path:
            rules = Rules.load(rules_path)
        game = cls(proc, rules, units_base, warnings)
        try:
            game.gametxt = load_game_txt(Path(exe).parent)
        except Exception as e:
            warnings.append(f"GAME.TXT not loaded ({e})")
        return game

    def close(self):
        self.p.close()

    # ------------------------------------------------------------------ scales
    def _scales(self):
        """Work out whether rules-table Move/HP are pre-multiplied (verify at runtime)."""
        self.road_mult = self.p.u8(M.COSMIC) or 3
        raw = self.p.read(M.UNIT_TYPES, M.UNIT_TYPE_SIZE * M.UNIT_TYPES_MAX)
        self.unit_types = []
        for i in range(M.UNIT_TYPES_MAX):
            o = i * M.UNIT_TYPE_SIZE
            self.unit_types.append({
                "id": i, "name": self.rules.unit_name(i),
                "domain": M.DOMAINS[raw[o + M.UT_DOMAIN]] if raw[o + M.UT_DOMAIN] < 3 else raw[o + M.UT_DOMAIN],
                "move_raw": raw[o + M.UT_MOVE], "range": raw[o + M.UT_RANGE],
                "attack": raw[o + M.UT_ATT], "defense": raw[o + M.UT_DEF],
                "hp_raw": raw[o + M.UT_HP], "firepower": raw[o + M.UT_FP],
                "cost": raw[o + M.UT_COST], "hold": raw[o + M.UT_HOLD],
                "role": raw[o + M.UT_ROLE],
                "abilities": _u32(raw, o + M.UT_ABILITIES)})
        moves = [t["move_raw"] for t in self.unit_types if t["move_raw"]]
        hps = [t["hp_raw"] for t in self.unit_types if t["hp_raw"]]
        # pre-multiplied if every move value is a multiple of the road multiplier
        self.move_scaled = bool(moves) and all(m % self.road_mult == 0 for m in moves)
        # HP: rules file says 1..4, game shows 10..40
        self.hp_scaled = bool(hps) and max(hps) >= 10
        for t in self.unit_types:
            t["move_points"] = t["move_raw"] if self.move_scaled else t["move_raw"] * self.road_mult
            t["moves"] = t["move_points"] / self.road_mult
            t["hp"] = t["hp_raw"] if self.hp_scaled else t["hp_raw"] * 10

    # ------------------------------------------------------------------ reads
    def read_game(self) -> dict:
        if not any(t["move_raw"] for t in self.unit_types):
            # attached at the main menu: the rules tables were still empty (seen 2026-10-27)
            self._scales()
        g = self.p.read(M.GAME, M.GAME_SIZE)
        unit_selected = self.p.u32(M.UNIT_SELECTED) != 0
        active_unit = _i16(g, M.G_ACTIVE_UNIT)
        return {
            "turn": _i16(g, M.G_TURN),
            "year_counter": _u16(g, M.G_YEAR_COUNTER),
            "human_civ": self.p.i32(M.HUMAN_CIV_INDEX),
            "human_civ_game": g[M.G_HUMAN_CIV],
            "turn_civ_guess": _i8(g, M.G_SOME_CIV),
            "multiplayer_type": g[M.G_MULTI_TYPE],
            "difficulty": g[M.G_DIFFICULTY],
            "reveal_map": bool(g[M.G_REVEAL_MAP]),
            "active_players_mask": g[M.G_ACTIVE_PLAYERS],
            "human_players_mask": g[M.G_HUMAN_PLAYERS],
            "total_units": _i16(g, M.G_TOTAL_UNITS),
            "total_cities": _u16(g, M.G_TOTAL_CITIES),
            "unit_selected": unit_selected,
            "active_unit": active_unit if unit_selected and active_unit >= 0 else None,
            "cursor": [self.p.i16(M.CURSOR_X), self.p.i16(M.CURSOR_Y)],
        }

    def read_units(self, total_hint: int | None = None) -> list[dict]:
        # Civ2UIA iterates 0..Game.TotalUnits-1 and skips ID == 0
        # (Civ2UIA_PathLine.pas:154). Reading past it can hit UIA's options block.
        limit = 0x8000 if self.units_relocated else M.UNITS_MAX
        n = min(max(total_hint or 0, 0), limit)
        raw = self._read_shrinking(self.units_base, M.UNIT_SIZE, n) if n else b""
        out = []
        for i in range(len(raw) // M.UNIT_SIZE):
            o = i * M.UNIT_SIZE
            uid = _i32(raw, o + M.U_ID)
            x, y = _i16(raw, o + M.U_X), _i16(raw, o + M.U_Y)
            if uid == 0 or x < 0:
                continue
            t = raw[o + M.U_TYPE]
            ut = self.unit_types[t] if t < len(self.unit_types) else None
            spent = _i8(raw, o + M.U_MOVES_SPENT)
            dmg = raw[o + M.U_HP_LOST]
            attr = _u16(raw, o + M.U_ATTR)
            orders = _i8(raw, o + M.U_ORDERS)
            home = raw[o + M.U_HOME_CITY]
            mp_left = (ut["move_points"] - spent) if ut else None
            out.append({
                "index": i, "id": uid, "type": t,
                "type_name": ut["name"] if ut else f"UnitType#{t}",
                "owner": _i8(raw, o + M.U_OWNER), "x": x, "y": y,
                "hp": (ut["hp"] - dmg) if ut else None, "hp_max": ut["hp"] if ut else None,
                "damage": dmg,
                "moves_spent_raw": spent,
                "move_points_left": mp_left,
                "moves_left": round(mp_left / self.road_mult, 2) if mp_left is not None else None,
                "veteran": bool(attr & M.ATTR_VETERAN),
                "orders": M.ORDERS.get(orders, orders), "orders_raw": orders,
                "home_city": None if home == 0xFF else home,
                "goto": [_i16(raw, o + M.U_GOTO_X), _i16(raw, o + M.U_GOTO_Y)]
                if orders in (11, 27) else None,
                "visible_to_mask": raw[o + M.U_VISIBILITY],
                "attr_raw": attr,
            })
        return out

    def read_cities(self, total: int | None = None) -> list[dict]:
        n = M.CITIES_MAX if total is None else min(max(total, 0), M.CITIES_MAX)
        raw = self.p.read(M.CITIES, M.CITY_SIZE * n) if n else b""
        out = []
        for i in range(n):
            o = i * M.CITY_SIZE
            cid = _i32(raw, o + M.C_ID)
            if cid == 0:
                continue
            b = _i8(raw, o + M.C_BUILDING)
            if b >= 0:
                producing = {"kind": "unit", "id": b, "name": self.rules.unit_name(b)}
            else:
                producing = {"kind": "improvement", "id": -b,
                             "name": self.rules.improvement_name(-b)}
            attr = _u32(raw, o + M.C_ATTR)
            out.append({
                "index": i, "id": cid, "name": _cstr(raw[o + M.C_NAME:o + M.C_NAME + 16]),
                "owner": raw[o + M.C_OWNER], "x": _i16(raw, o + M.C_X), "y": _i16(raw, o + M.C_Y),
                "size": _i8(raw, o + M.C_SIZE), "food": _i16(raw, o + M.C_FOOD),
                "shields": _i16(raw, o + M.C_SHIELDS), "producing": producing,
                "disorder": bool(attr & 0x1), "wltkd": bool(attr & 0x2),
                "coastal": bool(attr & 0x80),
                "food_surplus_raw": raw[o + M.C_TOTAL_FOOD],
                "shield_total": raw[o + M.C_TOTAL_SHIELD],
                "happy": raw[o + M.C_HAPPY], "unhappy": raw[o + M.C_UNHAPPY],
                "improvements": [i for i in range(1, 40) if raw[o + M.C_IMPROVEMENTS + i // 8] >> (i % 8) & 1],
                "improvement_names": [self.rules.improvement_name(i) for i in range(1, 40)
                                      if raw[o + M.C_IMPROVEMENTS + i // 8] >> (i % 8) & 1],
                "worked_tiles": [i for i in range(21) if _u32(raw, o + M.C_WORKED) >> i & 1],
                "specialists": _u32(raw, o + M.C_WORKED) >> 26,
                "entertainers": sum(1 for k in range((_u32(raw, o + M.C_WORKED) >> 26) & 0x3F)
                                    if (_u32(raw, o + M.C_SPECIALISTS) >> (2 * k)) & 3 == 1),
            })
        return out

    def read_civs(self, active_mask: int) -> list[dict]:
        raw = self.p.read(M.CIVS, M.CIV_SIZE * M.CIVS_MAX)
        out = []
        for c in range(M.CIVS_MAX):
            o = c * M.CIV_SIZE
            leader = _u16(raw, o + M.CV_LEADER)
            tribe = self.rules.tribe(leader) if c else {"plural": "Barbarians"}
            gov = raw[o + M.CV_GOVERNMENT]
            res = _i16(raw, o + M.CV_RESEARCHING)
            out.append({
                "id": c, "alive": bool(active_mask & (1 << c)),
                "name": (tribe or {}).get("plural") or f"Civ#{c}",
                "name_source": "RULES.TXT default (custom names not read yet)",
                "gold": _i32(raw, o + M.CV_GOLD),
                "government": M.GOVERNMENTS[gov] if gov < len(M.GOVERNMENTS) else gov,
                "researching": self.rules.advance_name(res), "beakers": _u16(raw, o + M.CV_BEAKERS),
                "techs": raw[o + M.CV_TECHS],
                "tax_rate": raw[o + M.CV_TAX_RATE], "science_rate": raw[o + M.CV_SCIENCE_RATE],
            })
        return out

    def read_popup(self) -> dict | None:
        ptr = self.p.u32(M.CURR_POPUP)
        if not ptr:
            return None
        d = self.p.read(ptr, M.DIALOG_SIZE)
        flags = _u32(d, M.D_FLAGS)
        popup = {
            "ptr": f"0x{ptr:08X}",
            "title": self._str_at(_u32(d, M.D_TITLE)),
            "lines": self._walk(_u32(d, M.D_FIRST_TEXT_LINE), M.TEXT_LINE_TEXT, M.TEXT_LINE_NEXT),
            "options": [{"index": i, "text": t} for i, t in enumerate(
                self._walk(_u32(d, M.D_FIRST_LIST_ITEM), M.LIST_ITEM_TEXT, M.LIST_ITEM_NEXT))],
            "has_listbox": bool(flags & 0x1000),
            "list": self._listbox(d),
            "flags": [n for bit, n in M.DLG_FLAGS.items() if flags & bit],
            "num_buttons": _i32(d, M.D_NUM_BUTTONS),
            "num_buttons_std": _i32(d, M.D_NUM_BUTTONS_STD),
            "selected_item": _i32(d, M.D_SELECTED_ITEM),
            "pressed_button": _i32(d, M.D_PRESSED_BUTTON),
            "buttons": [],
            "section": None,
            "hwnd": None,
        }
        nbtn = min(max(popup["num_buttons"], 0) + max(popup["num_buttons_std"], 0), 6)
        ctrls = _u32(d, M.D_BUTTON_CONTROLS)
        for k in range(nbtn):
            std = _i32(d, M.D_BUTTONS + 8 * k)
            hwnd = None
            if ctrls:
                try:
                    hwnd = self.p.u32(ctrls + k * M.BUTTON_CTRL_SIZE + M.BUTTON_CTRL_HWND) or None
                except OSError:
                    pass
            popup["buttons"].append({
                "index": k, "text": self._str_at(_u32(d, M.D_BUTTON_TEXTS + 4 * k)),
                "std_type": {0: "ok", 1: "help", 2: "cancel"}.get(std, std), "hwnd": hwnd})
        try:
            popup["section"] = self.p.cstr(M.LOADED_TXT_SECTION, 32) or None
        except OSError:
            pass
        try:  # [[GraphicsInfo + 0x48 + 0x08] + 0x04]
            gi = _u32(d, M.D_GRAPHICS_INFO)
            ws = self.p.u32(gi + 0x48 + 0x08)
            popup["hwnd"] = self.p.u32(ws + 0x04) or None
        except OSError:
            pass
        n_std = max(popup["num_buttons_std"], 0)
        for b in popup["buttons"]:
            if b["index"] < n_std:   # standard buttons have no text pointer
                b["text"] = {"ok": "OK", "cancel": "Cancel", "help": "Help"}.get(b["std_type"], b["text"])
        popup["radios"] = self._read_radios(popup["hwnd"]) if popup["hwnd"] else None
        popup["option_controls"] = self._read_list_item_controls(popup["hwnd"]) if popup["hwnd"] else []
        if popup["radios"] and not popup["options"]:
            popup["options"] = [{"index": r["index"], "text": r["text"]}
                                for r in popup["radios"]["items"]]
        tmpl = self.gametxt.get(popup["section"] or "") if self.gametxt else None
        if tmpl:
            popup["template"] = tmpl
            if not popup["options"] and tmpl.get("options"):
                popup["options"] = [{"index": i, "text": t, "source": "GAME.TXT"}
                                    for i, t in enumerate(tmpl["options"])]
        if popup["hwnd"] and not popup["option_controls"] and popup["options"]:
            # fallback: option rows are the non-button child windows, top to bottom
            btn = {b["hwnd"] for b in popup["buttons"] if b.get("hwnd")}
            rows = sorted((w for w in win32.child_windows(popup["hwnd"])
                           if w.hwnd not in btn and w.visible), key=lambda w: (w.rect[1], w.rect[0]))
            if len(rows) == len(popup["options"]):
                popup["option_controls"] = [{"index": i, "hwnd": w.hwnd, "mapping": "by_position"}
                                            for i, w in enumerate(rows)]
        popup["selected_option"] = (popup["radios"] or {}).get("selected", popup["selected_item"])
        return popup

    def _read_list_item_controls(self, popup_hwnd: int) -> list[dict]:
        """Option rows of @options popups are TControlInfoListItem children
        (ControlType 1, Code = 0x3E8 + index or 0x12C + index)."""
        out = []
        for w in win32.child_windows(popup_hwnd):
            try:
                ci = win32.control_info_ptr(w.hwnd)
                if not ci:
                    continue
                hdr = self.p.read(ci, 0x20)
                if _u32(hdr, 0) != 1:
                    continue
                code = _i32(hdr, 4)
                idx = code - 0x3E8 if code >= 0x3E8 else code - 0x12C
                out.append({"index": idx, "code": code, "hwnd": w.hwnd})
            except OSError:
                continue
        return sorted(out, key=lambda x: x["index"])

    def _read_radios(self, popup_hwnd: int) -> dict | None:
        """TControlInfoRadioGroup (ControlType 3) behind a popup child window."""
        for w in win32.child_windows(popup_hwnd):
            try:
                ci = win32.control_info_ptr(w.hwnd)
                if not ci or self.p.u32(ci) != 3:
                    continue
                g = self.p.read(ci, 0x50)
                n, cols, sel, pr = _i32(g, 0x38), _i32(g, 0x3C), _i32(g, 0x40), _u32(g, 0x48)
                items = []
                if pr and 0 < n <= 64:
                    raw = self.p.read(pr, 0xA4 * n)
                    for k in range(n):
                        o = k * 0xA4
                        items.append({"index": _i32(raw, o + 0x14),
                                      "text": _cstr(raw[o + 0x1C:o + 0x1C + 128]).replace("&", ""),
                                      "enabled": bool(_i32(raw, o + 0x18)),
                                      "hwnd": _u32(raw, o) or None})
                return {"group_hwnd": w.hwnd, "selected": sel, "columns": cols, "items": items}
            except OSError:
                continue
        return None

    def read_state(self) -> dict:
        g = self.read_game()
        state = dict(g)
        state["units"] = self.read_units(g["total_units"])
        state["cities"] = self.read_cities(g["total_cities"])
        state["civs"] = self.read_civs(g["active_players_mask"])
        state["popup"] = self.read_popup()
        state["map"] = self.map_size()
        return state

    # ------------------------------------------------------------------ map
    def map_size(self) -> dict:
        h = self.p.read(M.MAP_HEADER, 6)
        return {"width2": _i16(h, M.MH_SIZE_X), "height": _i16(h, M.MH_SIZE_Y),
                "area": _i16(h, M.MH_AREA)}

    def map_seed(self) -> int:
        return self.p.u16(M.MAP_HEADER + M.MH_SEED)

    def huts(self) -> list[dict]:
        """All tiles that still hold a goody hut (village)."""
        ms = self.map_size()
        return [t for t in self.read_tiles(0, 0, ms["width2"], ms["height"]) if t["hut"]]

    def read_tiles(self, x0: int, y0: int, w: int, h: int) -> list[dict]:
        seed = self.map_seed()
        ms = self.map_size()
        W2, H, area = ms["width2"], ms["height"], ms["area"]
        base = self.p.u32(M.MAP_DATA_PTR)
        raw = self.p.read(base, area * M.TILE_SIZE)
        out = []
        for y in range(max(0, y0), min(H, y0 + h)):
            for x in range(x0, x0 + w):
                xx = x % W2
                if (xx + y) % 2:          # Civ2 uses only tiles where x+y is even
                    continue
                idx = y * (W2 // 2) + xx // 2
                if not 0 <= idx < area:
                    continue
                t = raw[idx * M.TILE_SIZE:(idx + 1) * M.TILE_SIZE]
                terr = t[M.T_TERRAIN]
                f = t[M.T_FEATURES]
                out.append({
                    "x": xx, "y": y,
                    "terrain": M.TERRAINS[terr & 0x0F] if (terr & 0x0F) < 11 else terr & 0x0F,
                    "river": bool(terr & 0x80), "resource_suppressed": bool(terr & 0x40),
                    "unit": bool(f & 0x01), "city": bool(f & 0x02),
                    "irrigation": (f & 0x0C) == 0x04,
                    "mine": (f & 0x0C) == 0x08,
                    "farmland": (f & 0x0C) == 0x0C,
                    "road": bool(f & 0x10), "railroad": (f & 0x30) == 0x30,
                    "fortress": bool(f & 0x40), "pollution": bool(f & 0x80),
                    "explored_mask": t[M.T_VISIBILITY], "owner_raw": t[M.T_OWNER],
                    "owner": None if (t[M.T_OWNER] >> 4) == 0xF else t[M.T_OWNER] >> 4,
                    # hut = seed pattern + tile never owned/entered (UIA Civ2Types.pas:1150)
                    "hut": (terr & 0x0F) != 10 and (t[M.T_OWNER] >> 4) == 0xF and not (f & 0x02)
                           and hut_pattern(xx, y, seed),
                    "city_radius_civ": t[M.T_CITY_RADII] >> 5, "mass": t[M.T_MASS],
                })
        return out

    # ------------------------------------------------------------------ helpers
    def _read_shrinking(self, addr: int, elem: int, n: int) -> bytes:
        while n > 0:
            try:
                return self.p.read(addr, elem * n)
            except OSError:
                n //= 2
        return b""

    def _str_at(self, ptr: int, maxlen: int = 256) -> str | None:
        if not ptr:
            return None
        try:
            return self.p.cstr(ptr, maxlen)
        except OSError:
            # string may sit near the end of a page: retry shorter
            try:
                return self.p.cstr(ptr, 32)
            except OSError:
                return None

    def _listbox(self, d: bytes, limit: int = 400) -> list[dict]:
        """Rows of a list-box popup: [{"index", "text", "name", "value", "selected"}]."""
        ptr, sel = _u32(d, M.D_LISTBOX_FIRST), _u32(d, M.D_LISTBOX_SELECTED)
        out, seen = [], set()
        while ptr and ptr not in seen and len(out) < limit:
            seen.add(ptr)
            try:
                rec = self.p.read(ptr, M.LIST_ITEM_NEXT + 4)
            except OSError:
                break
            text = self._str_at(_u32(rec, M.LIST_ITEM_TEXT)) or ""
            out.append({"index": len(out), "text": text, "name": text.split("|")[0].strip(),
                        "value": _i32(rec, M.LIST_ITEM_VALUE), "selected": ptr == sel})
            ptr = _u32(rec, M.LIST_ITEM_NEXT)
        return out

    def _walk(self, ptr: int, text_off: int, next_off: int, limit: int = 64) -> list[str]:
        out, seen = [], set()
        while ptr and ptr not in seen and len(out) < limit:
            seen.add(ptr)
            try:
                rec = self.p.read(ptr, next_off + 4)
            except OSError:
                break
            out.append(self._str_at(_u32(rec, text_off)) or "")
            ptr = _u32(rec, next_off)
        return out
