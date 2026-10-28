"""
File-based transport for the civ2api Session, for agents that can write files in
this folder but cannot reach 127.0.0.1. Runs windowless (pythonw) and logs to
logs/bridge.log.

Drop a JSON command into queue/in/<name>.json; the result appears in
queue/out/<name>.json and the command moves to queue/done/.

Commands: {"op":"health"} {"op":"state"} {"op":"units","owner":"me"|"all"|<id>}
          {"op":"events","since":0} {"op":"dialogs"} {"op":"diag"}
          {"op":"tiles","x":..,"y":..,"w":..,"h":..}
          {"op":"act", ...act body...}  {"op":"answer_dialog","option":1,"button":"ok"}
          {"op":"keys","method":"post"|"sendinput"}
          {"op":"console"} (start/restart web console server)
          {"op":"api","method":"GET"|"POST","path":"/tasks","body":{...}}  (proxy to the console server)
          {"op":"restart"} (reload code)  {"op":"stop"}
"""
import json, os, subprocess, sys, time, traceback
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

ROOT = os.path.join(HERE, "queue")
IN, OUT, DONE = (os.path.join(ROOT, d) for d in ("in", "out", "done"))
LOGS = os.path.join(HERE, "logs")
for d in (IN, OUT, DONE, LOGS):
    os.makedirs(d, exist_ok=True)
LOG = open(os.path.join(LOGS, "bridge.log"), "a", encoding="utf-8", buffering=1)
PIDFILE = os.path.join(LOGS, "bridge.pid")


def log(*a):
    LOG.write(time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a) + "\n")


def kill_other_bridges():
    """Stop older bridge processes (ours only: command line contains file_bridge.py)."""
    try:
        ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*file_bridge.py*' "
              f"-and $_.ProcessId -ne {os.getpid()} }} | ForEach-Object {{ $_.ProcessId }}")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                             text=True, timeout=20, creationflags=0x08000000).stdout
        for pid in [int(x) for x in out.split() if x.strip().isdigit()]:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                           creationflags=0x08000000)
            log("killed old bridge pid", pid)
    except Exception as e:
        log("kill_other_bridges failed:", e)


from civ2api.session import Session   # noqa: E402
from civ2api.state import Civ2        # noqa: E402

SESSION = None


def session():
    """Attach lazily; never block a command for long when the game is not running."""
    global SESSION
    if SESSION is not None:
        try:
            SESSION.game.p.u8(0x00655AE8)
            return SESSION
        except Exception as e:
            log("lost game:", e)
            SESSION.stop()
            SESSION = None
    g = Civ2.attach()
    SESSION = Session(g, poll_interval=0.25, key_method=os.environ.get("CIV2_KEYS", "post"))
    SESSION.start_polling()
    log("attached pid", g.p.pid, "warnings", g.warnings)
    return SESSION


def _api(cmd):
    """Proxy to the web console server, so the bridge exposes the full HTTP API (tasks, plan...)."""
    import urllib.request
    method = cmd.get("method", "GET").upper()
    data = json.dumps(cmd.get("body", {})).encode() if method == "POST" else None
    req = urllib.request.Request("http://127.0.0.1:8765" + cmd["path"], data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    import urllib.error
    try:
        with urllib.request.urlopen(req, timeout=float(cmd.get("timeout", 30))) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:          # keep the server's error message
        body = e.read().decode("utf-8", "replace")
        try:
            return {"http_status": e.code, **json.loads(body)}
        except ValueError:
            return {"http_status": e.code, "error": body[:2000]}


def handle(cmd):
    op = cmd.get("op")
    if op == "api":
        return _api(cmd)
    s = session()
    st = s.state()
    if op == "health":
        return {"pid": s.game.p.pid, "units_base": hex(s.game.units_base), "road_mult": s.game.road_mult,
                "move_scaled": s.game.move_scaled, "hp_scaled": s.game.hp_scaled,
                "warnings": s.game.warnings, "bridge_pid": os.getpid()}
    if op == "state":
        return st
    if op == "units":
        owner = cmd.get("owner", "me")
        us = st["units"]
        if owner == "me":
            us = [u for u in us if u["owner"] == st["human_civ"]]
        elif owner != "all":
            us = [u for u in us if u["owner"] == int(owner)]
        keep = ("turn", "human_civ", "human_civ_game", "turn_civ_guess", "active_unit", "unit_selected",
                "total_units", "total_cities", "popup", "cursor")
        return {**{k: st[k] for k in keep}, "units": us}
    if op == "events":
        return {"last_seq": s.log.last_seq(), "events": s.log.since(int(cmd.get("since", 0)))}
    if op == "dialogs":
        return {"popup": s.game.read_popup(), "windows": s.windows()}
    if op == "huts":
        return {"seed": s.game.map_seed(), "huts": s.game.huts()}
    if op == "diag":
        return s.diag()
    if op == "tiles":
        return s.game.read_tiles(cmd["x"], cmd["y"], cmd.get("w", 10), cmd.get("h", 10))
    if op == "act":
        return s.act({k: v for k, v in cmd.items() if k != "op"})
    if op == "answer_dialog":
        return s.answer_dialog(cmd.get("option"), cmd.get("button"))
    if op == "keys":
        s.key_method = cmd["method"]
        return {"key_method": s.key_method}
    return {"error": f"unknown op {op!r}"}


def respond(name, res):
    tmp = os.path.join(OUT, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, os.path.join(OUT, name))


def main():
    kill_other_bridges()
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    log("bridge started pid", os.getpid(), "watching", IN)
    while True:
        for name in sorted(os.listdir(IN)):
            if not name.endswith(".json"):
                continue
            src = os.path.join(IN, name)
            try:
                with open(src, encoding="utf-8") as f:
                    cmd = json.load(f)
            except Exception:
                time.sleep(0.2)
                continue
            op = cmd.get("op")
            if op == "console":
                # (re)start the web console server; it stops any older copy itself
                subprocess.Popen([sys.executable, "-m", "civ2api.server"], cwd=HERE,
                                 creationflags=0x00000008 | 0x00000200)
                respond(name, {"ok": True, "result": "console starting on http://127.0.0.1:8765/"})
                os.replace(src, os.path.join(DONE, name))
                log("console (re)started")
                continue
            if op in ("stop", "restart"):
                respond(name, {"ok": True, "result": op})
                os.replace(src, os.path.join(DONE, name))
                log(op)
                if op == "restart":
                    subprocess.Popen([sys.executable, os.path.abspath(__file__)], cwd=HERE,
                                     creationflags=0x00000008 | 0x00000200)  # DETACHED | NEW_GROUP
                return
            try:
                res = {"ok": True, "result": handle(cmd)}
            except Exception as e:
                res = {"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()}
            respond(name, res)
            os.replace(src, os.path.join(DONE, name))
            log("done", name, res.get("ok"), res.get("error", ""))
        time.sleep(0.2)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("FATAL", traceback.format_exc())
        raise
