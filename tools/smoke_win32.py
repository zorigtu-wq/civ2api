"""Windows smoke test of civ2api.win32 without the game: struct sizes, process list, windows, key parsing."""
import ctypes, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from civ2api import win32
print("python", sys.version.split()[0], ctypes.sizeof(ctypes.c_void_p) * 8, "bit")
print("sizeof INPUT", ctypes.sizeof(win32.INPUT), "(expect 40 on x64)")
print("sizeof PROCESSENTRY32W", ctypes.sizeof(win32.PROCESSENTRY32W), "(expect 568 on x64)")
pids = win32.find_pids("explorer.exe")
print("explorer pids", pids)
pr = win32.ProcessReader(pids[0])
print("explorer image", pr.image_path(), "32bit:", pr.is_32bit())
# read our own process memory through the same API as a correctness check
me = win32.ProcessReader(os.getpid())
buf = ctypes.create_string_buffer(b"CIV2API-SELFTEST\0")
print("self read:", me.cstr(ctypes.addressof(buf), 32))
wins = win32.top_windows(pids[0])
print("explorer windows:", [(w.cls, w.title[:40]) for w in wins][:5])
print("parse shift+d:", win32.parse_key("shift+d"), " num9:", win32.parse_key("num9"))
print("civ2 pids:", win32.find_pids("civ2.exe"))
print("SMOKE OK")
