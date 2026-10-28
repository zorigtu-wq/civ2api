import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from civ2api import win32
for name in ("civ2.exe", "civ2uialauncher.exe"):
    for pid in win32.find_pids(name):
        print(name, pid)
        for w in win32.top_windows(pid, visible_only=False):
            print("   ", w.as_dict())
