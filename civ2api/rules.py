"""
Names from RULES.TXT. The unit-type table in memory only holds an index into
the game's text pool (base address not yet known), so names come from the
rules file next to CIV2.EXE (or a scenario folder you point at).
Order of @UNITS rows == order of the UnitTypes array in memory.
"""
from __future__ import annotations

import re
from pathlib import Path


def _sections(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    cur: list[str] | None = None
    for raw in text.splitlines():
        line = raw.split(";", 1)[0].rstrip()
        if line.startswith("@"):
            cur = out.setdefault(line[1:].strip().upper(), [])
            continue
        if cur is None:
            continue
        if not line.strip():
            if cur:            # blank line after entries closes the section
                cur = None
            continue
        cur.append(line)
    return out


def _fields(line: str) -> list[str]:
    return [f.strip() for f in line.split(",")]


_NUM = re.compile(r"^-?\d+\.?\d*$")


class Rules:
    def __init__(self, units=None, improvements=None, advances=None, leaders=None,
                 source: str | None = None):
        self.units: list[str] = units or []
        self.improvements: list[str] = improvements or []
        self.advances: list[str] = advances or []
        self.leaders: list[dict] = leaders or []
        self.source = source
        # @TERRAIN move cost per base terrain (Desert..Ocean); Civ2 defaults
        self.terrain_move: list[int] = [1, 1, 1, 2, 2, 3, 1, 2, 2, 2, 1]

    @classmethod
    def load(cls, path: str | Path) -> "Rules":
        p = Path(path)
        if p.is_dir():
            p = next((c for c in p.iterdir() if c.name.lower() == "rules.txt"), p / "RULES.TXT")
        text = p.read_text(encoding="cp1252", errors="replace")
        s = _sections(text)
        units = [_fields(l)[0] for l in s.get("UNITS", [])]
        impr = [_fields(l)[0] for l in s.get("IMPROVE", [])]
        adv = [_fields(l)[0] for l in s.get("CIVILIZE", [])]
        leaders = []
        for l in s.get("LEADERS", []):
            words = [f for f in _fields(l) if f and not _NUM.match(f)]
            leaders.append({"male": words[0] if words else "",
                            "female": words[1] if len(words) > 1 else "",
                            "plural": words[2] if len(words) > 2 else "",
                            "adjective": words[3] if len(words) > 3 else ""})
        r = cls(units, impr, adv, leaders, str(p))
        # prerequisites, for "can this city build X" without opening the game's list
        codes = []
        in_civ = False
        for raw in text.splitlines():
            if raw.startswith("@"):
                in_civ = raw.strip().upper() == "@CIVILIZE"
                continue
            if in_civ and ";" in raw and raw.split(";", 1)[0].strip():
                codes.append(raw.split(";", 1)[1].strip().split()[0] if raw.split(";", 1)[1].strip() else "")
        r.advance_codes = codes[:len(adv)]
        r.improve_prereq = [(_fields(l) + ["", "", "", ""])[3] for l in s.get("IMPROVE", [])]
        r.unit_prereq = [(_fields(l) + [""] * 14)[12] for l in s.get("UNITS", [])]
        r.unit_until = [(_fields(l) + ["", ""])[1] for l in s.get("UNITS", [])]
        r.terrain_lines = s.get("TERRAIN", [])
        r.terrain_move = []
        for l in s.get("TERRAIN", [])[:11]:
            try:
                r.terrain_move.append(int(_fields(l)[1]))
            except (IndexError, ValueError):
                r.terrain_move.append(1)
        return r

    def unit_name(self, t: int) -> str:
        return self.units[t] if 0 <= t < len(self.units) else f"UnitType#{t}"

    def improvement_name(self, i: int) -> str:
        return self.improvements[i] if 0 <= i < len(self.improvements) else f"Improvement#{i}"

    def advance_name(self, i: int) -> str | None:
        if i < 0:
            return None
        return self.advances[i] if i < len(self.advances) else f"Advance#{i}"

    def tribe(self, leader_index: int) -> dict | None:
        return self.leaders[leader_index] if 0 <= leader_index < len(self.leaders) else None


def load_game_txt(path: str | Path) -> dict[str, dict]:
    """Parse GAME.TXT popup templates: {SECTION: {title, lines, options, flags}}.
    Used to name popup options when the game keeps them only as drawn controls."""
    p = Path(path)
    if p.is_dir():
        p = next((c for c in p.iterdir() if c.name.lower() == "game.txt"), p / "Game.txt")
    out: dict[str, dict] = {}
    cur = None
    in_opts = False
    for raw in p.read_text(encoding="cp1252", errors="replace").splitlines():
        line = raw.rstrip()
        if line.startswith(";"):
            continue
        if line.startswith("@") and "=" not in line and line[1:].strip().upper() == line[1:].strip() \
                and line[1:].strip().upper() not in ("OPTIONS", "LISTBOX", "COLUMNS", "DEFAULT"):
            cur = out.setdefault(line[1:].strip(), {"title": None, "lines": [], "options": [], "flags": []})
            in_opts = False
            continue
        if cur is None:
            continue
        if not line.strip():
            if cur["lines"] or cur["options"] or cur["title"]:
                cur = None
            continue
        low = line.lower()
        if low.startswith("@title="):
            cur["title"] = line.split("=", 1)[1]
        elif low.startswith("@options"):
            in_opts = True
        elif line.startswith("@"):
            cur["flags"].append(line[1:])
        elif in_opts:
            cur["options"].append(line.lstrip("^"))
        else:
            cur["lines"].append(line.lstrip("^"))
    return out
