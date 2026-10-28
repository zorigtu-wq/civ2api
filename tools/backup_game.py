"""
Back up the Civ2 game folder before anything else touches it, and record
CIV2.EXE's size + SHA-256 so we can prove later that it was never modified.

usage:  python tools\\backup_game.py "C:\\Games\\Civ2"  [dest_parent]
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import sys
from pathlib import Path

EXPECTED_SIZE = 2_489_344  # MGE 5.4.0f (Patch 3), required by Civ2UIA


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src = Path(sys.argv[1]).resolve()
    exe = next((p for p in src.iterdir() if p.name.lower() == "civ2.exe"), None)
    if exe is None:
        print(f"CIV2.EXE not found in {src}")
        return 1
    size = exe.stat().st_size
    digest = sha256(exe)
    print(f"CIV2.EXE  size={size:,}  sha256={digest}")
    if size != EXPECTED_SIZE:
        print(f"WARNING: expected {EXPECTED_SIZE:,} bytes (MGE 5.4.0f). Addresses may not match.")

    parent = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else src.parent
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = parent / f"{src.name}_backup_{stamp}"
    shutil.copytree(src, dest)
    manifest = {"source": str(src), "backup": str(dest), "time": stamp,
                "civ2_exe": {"size": size, "sha256": digest}}
    (dest / "_backup_manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"Backup written to {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
