"""
Localhost HTTP API + web console (stdlib only). Binds to 127.0.0.1 only.

    python -m civ2api.server            -> http://127.0.0.1:8765/

GET  /                        web console (console.html)
GET  /health                  attach status, scales, warnings
GET  /state                   full snapshot (units, cities, civs, popup, game)
GET  /state/units?owner=me    units filter (owner=me|all|<civ id>)
GET  /events?since=<seq>&wait=<sec>   events after seq (long-poll)
GET  /tiles?x=&y=&w=&h=       map window
GET  /unit_types
GET  /dialogs                 current popup (memory) + game windows (Win32)
GET  /diag                    key target / focus / popup child windows
GET  /huts                    goody huts (villages) still on the map
GET  /plan?unit_id=&x=&y=     fastest path preview (turn-aware; roads, rivers, railroad, ZOC)
GET  /tasks                   unit tasks + observed move costs
POST /tasks                   {"unit_id": 9, "x": 117, "y": 47}  -> goto task
POST /tasks/cancel            {"unit_id": 9}
POST /tasks/settings          {"enabled": true, "auto_cycle": false}
POST /hunt                    {"unit_id": 9, "x": 117, "y": 47, "min_moves": 2, "max_attempts": 20,
                               "then": [120, 40]}  village: save, enter, keep if a 2+ move unit
                               is gained, else load and retry
GET  /hunt                    hunt status / outcomes;  POST /hunt/cancel
POST /campaign                {"target": 10, "min_moves": 2}  keep taking villages until we own
                               `target` units with >= min_moves (skips other units, ends turns)
GET  /campaign;  POST /campaign/cancel
POST /game/save               {"name": "x.sav"}  -> civ2api/saves/x.sav (never overwrites)
POST /game/load               {"name": "x.sav"}  (answers the LOADOK popup)
GET  /menu                    Civ2 menu tree with command ids
POST /activate                {"unit_id": 5}  make that unit active (wakes sleeping units)
GET  /city_sites?unit_ids=1,2&max_turns=6   best non-overlapping city sites for settlers
POST /end_turn                {"save": true}  save saves/turnNNN_end_*.sav, then end the turn
GET  /protocol?since=0         decision protocol: every action with the reason it was taken
POST /tax_rates               {"tax":2,"lux":2,"sci":6}  (tens of percent; memory write, "method":"ui" = mouse)
POST /city_manager            {"items":["Temple","Barracks"],"after":{"built":"Temple","items":["Caravan","Barracks"]}}
                              Stage 2: production + entertainers by memory writes (no windows)
GET  /wonders                 wonder cities, progress, caravans on the way, wonders built
POST /wonders/plan            {"cities":[0,1,2], "list":["Pyramids", ...]}  change the wonder plan
GET  /reports                 one report per turn: gold, rates, unhappy, disorder, starving, goals
GET  /goals ; POST /goals {"type":"cities","target":250,"by_turn":90} ; POST /goals/delete {"id":1}
GET  /turn_saves              automatic saves/turnNNN_start_*.sav at the start of each turn
GET  /alerts                  enemy units near our cities, disorder/riot/starving, wonder race, gold, jobs
GET  /rivals                  every civ: cities, population, techs, army, wonders built / in production
GET  /government              government, plan, rates, max rate, rate policy;  POST /gov_rates {"policy":{"Democracy":[4,4,2]}}
GET  /research                research, beakers/turn, research plan status;  POST /research_plan {"techs":[...]}
GET  /saves                   every save in civ2api/saves;  POST /rollback {"name":"turn070_start_*.sav"}
POST /found_cities           {"unit_ids":[1,2], "max_turns":6} -> goto + Build New City tasks
POST /act                     {"action":"move","dir":"NE"} | {"action":"key","key":"shift+d"} | {"action":"fortify"}
POST /answer_dialog           {"option": 2, "button": "ok"}
POST /keys                    {"method": "post"|"sendinput"}

The game is attached lazily and re-attached automatically, so the server can be
started before Civ2 and survives a game restart.
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import win32
from .session import COMMANDS, DIRECTIONS, Session
from .state import Civ2
from .tasks import TaskRunner
from .hunt import HutHunt
from .campaign import Campaign

HERE = os.path.dirname(os.path.abspath(__file__))
CONSOLE = os.path.join(HERE, "console.html")
SAVES = os.path.join(os.path.dirname(HERE), "saves")   # civ2api/saves
RESEARCH_PLAN_PATH = os.path.join(os.path.dirname(HERE), "logs", "research_plan.json")
GOV_PLAN_PATH = os.path.join(os.path.dirname(HERE), "logs", "government_plan.json")


class Hub:
    """Owns the Session; attaches on demand and re-attaches after the game restarts."""

    def __init__(self, pid=None, rules=None, key_method="post", poll=0.25):
        self.pid, self.rules, self.key_method, self.poll = pid, rules, key_method, poll
        self.session: Session | None = None
        self.runner: TaskRunner | None = None
        self.hunts: list[HutHunt] = []
        self.campaign: Campaign | None = None
        self.last_error: str | None = None
        self._lock = threading.Lock()

    def get(self) -> Session:
        with self._lock:
            s = self.session
            if s is not None:
                try:
                    s.game.p.u8(0x00655AE8)
                    return s
                except Exception as e:
                    self.last_error = f"lost game: {e}"
                    s.stop()
                    if self.runner:
                        self.runner.stop()
                    self.session = self.runner = None
            game = Civ2.attach(self.pid, self.rules)
            s = Session(game, self.poll, self.key_method)
            s.start_polling()
            self.session = s
            self.production = None
            self.runner = TaskRunner(s)
            install_site_hooks(s, self.runner)
            from .journal import Journal
            s.journal = Journal(os.path.join(os.path.dirname(HERE), "logs", "decisions.jsonl"))
            try:                                   # the player's government choice survives restarts
                with open(GOV_PLAN_PATH, encoding="utf-8") as f:
                    s.government_plan = list(json.load(f)) or s.government_plan
            except (OSError, ValueError):
                pass
            try:
                with open(RESEARCH_PLAN_PATH, encoding="utf-8") as f:
                    s.research_plan = list(json.load(f)) or s.research_plan
            except (OSError, ValueError):
                pass
            from .turnsave import TurnSaver
            if getattr(self, "saver", None):
                self.saver.stop()
            self.saver = TurnSaver(s, SAVES, busy=lambda: (
                any(h.info()["live"] for h in self.hunts)
                or (self.campaign is not None and self.campaign.status == "hunting")
                or bool(self.production and getattr(self.production, "busy", False)))).start()
            self.runner.start()
            self.last_error = None
            return s


HUB: Hub | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "civ2api/0.3"

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, ctype):
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def log_message(self, fmt, *args):  # quiet
        pass

    def _host_ok(self) -> bool:
        # refuse requests whose Host header is not loopback (DNS-rebinding guard)
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost", "[::1]")

    def do_GET(self):
        if not self._host_ok():
            return self._send({"error": "forbidden host"}, 403)
        u = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        if u.path in ("/", "/console", "/index.html"):
            return self._send_file(CONSOLE, "text/html; charset=utf-8")
        try:
            s = HUB.get()
        except Exception as e:
            return self._send({"attached": False, "error": f"{type(e).__name__}: {e}"}, 503)
        try:
            if u.path == "/health":
                st = s.state()
                return self._send({"attached": True, "pid": s.game.p.pid, "turn": st["turn"],
                                   "units_base": hex(s.game.units_base), "key_method": s.key_method,
                                   "move_scaled": s.game.move_scaled, "hp_scaled": s.game.hp_scaled,
                                   "road_mult": s.game.road_mult, "warnings": s.game.warnings,
                                   "commands": COMMANDS, "directions": list(DIRECTIONS),
                                   "console_version": int(os.path.getmtime(CONSOLE))})
            if u.path == "/state":
                return self._send(s.state())
            if u.path == "/state/units":
                st = s.state()
                owner = q.get("owner", "me")
                units = st["units"]
                if owner == "me":
                    units = [x for x in units if x["owner"] == st["human_civ"]]
                elif owner != "all":
                    units = [x for x in units if x["owner"] == int(owner)]
                return self._send({"turn": st["turn"], "human_civ": st["human_civ"],
                                   "active_unit": st["active_unit"], "units": units})
            if u.path == "/events":
                since = int(q.get("since", 0))
                deadline = time.time() + min(float(q.get("wait", 0)), 25)
                evs = s.log.since(since)
                while not evs and time.time() < deadline:
                    time.sleep(0.1)
                    evs = s.log.since(since)
                return self._send({"last_seq": s.log.last_seq(), "events": evs})
            if u.path == "/tiles":
                return self._send(s.game.read_tiles(int(q["x"]), int(q["y"]),
                                                    int(q.get("w", 10)), int(q.get("h", 10))))
            if u.path == "/huts":
                st = s.state()
                me = st["human_civ"]
                au = next((x for x in st["units"] if x["index"] == st["active_unit"]), None)
                hs = s.game.huts()
                for h in hs:
                    h["explored_by_me"] = bool(h["explored_mask"] & (1 << me)) if me is not None and me >= 0 else None
                    if au:
                        dx, dy = abs(h["x"] - au["x"]), abs(h["y"] - au["y"])
                        h["distance"] = max(dy, (dx + dy) // 2)   # tiles in Civ2 diamond grid
                hs.sort(key=lambda h: h.get("distance", 0))
                return self._send({"seed": s.game.map_seed(), "count": len(hs), "huts": hs})
            if u.path == "/tasks":
                r = HUB.runner
                return self._send({"enabled": r.enabled, "auto_cycle": r.auto_cycle,
                                   "status": r.status, "tasks": r.list(), "cost_log": r.cost_log[-30:]})
            if u.path == "/hunt":
                return self._send({"hunts": [h.info() for h in HUB.hunts[-10:]][::-1]})
            if u.path == "/campaign":
                return self._send(HUB.campaign.info() if HUB.campaign else {"status": "none", "live": False})
            if u.path == "/plan":
                return self._send(HUB.runner.plan(int(q["unit_id"]), int(q["x"]), int(q["y"])))
            if u.path == "/dev/read":
                a, n = int(q["addr"], 0), min(int(q.get("n", "64")), 4096)
                return self._send({"addr": hex(a), "hex": s.game.p.read(a, n).hex()})
            if u.path == "/dev/find_spiral":
                return self._send(find_spiral(s, int(q.get("lo", "0x600000"), 0), int(q.get("hi", "0x700000"), 0)))
            if u.path == "/dev/popup_scan":
                return self._send(popup_scan(s))
            if u.path == "/research_plan":
                return self._send({"research_plan": s.research_plan})
            if u.path in ("/alerts", "/rivals", "/government", "/research"):
                from . import panels
                from .memwrite import MemWriter
                HUB.memw = getattr(HUB, "memw", None) or MemWriter(s)
                if u.path == "/alerts":
                    return self._send(panels.alerts(s, HUB, HUB.memw))
                if u.path == "/rivals":
                    return self._send(panels.rivals(s, HUB.memw))
                if u.path == "/government":
                    return self._send(panels.government(s, HUB))
                return self._send(panels.research(s, HUB.memw, load_reports(60)))
            if u.path == "/saves":
                from . import panels
                return self._send(panels.saves_list(SAVES, int(q.get("limit", 150))))
            if u.path == "/wonders":
                return self._send(wonders_view(s))
            if u.path == "/reports":
                return self._send(load_reports(int(q.get("limit", 60))))
            if u.path == "/goals":
                return self._send(goals_store().evaluate(s, getattr(HUB, "memw", None), len(s.game.huts())))
            if u.path == "/protocol":
                return self._send(s.journal.since(int(q.get("since", 0)), int(q.get("limit", 300))))
            if u.path == "/turn_saves":
                return self._send(HUB.saver.info())
            if u.path == "/production":
                return self._send(HUB.production.info() if HUB.production else {"status": "none"})
            if u.path == "/city_sites":
                ids = [int(x) for x in q.get("unit_ids", "").split(",") if x]
                return self._send(city_sites(s, HUB.runner, ids, int(q.get("max_turns", 6)),
                                             int(q.get("top", 8))))
            if u.path == "/unit_types":
                return self._send(s.game.unit_types)
            if u.path == "/dialogs":
                return self._send({"popup": s.game.read_popup(), "windows": s.windows()})
            if u.path == "/menu":
                from . import win32
                return self._send(win32.menu_tree(s.main_hwnd()))
            if u.path == "/diag":
                return self._send(s.diag())
            return self._send({"error": "not found", "doc": __doc__}, 404)
        except Exception as e:
            return self._send({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        if not self._host_ok():
            return self._send({"error": "forbidden host"}, 403)
        # browsers send Origin on cross-site POSTs; only accept our own page
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
            return self._send({"error": "forbidden origin"}, 403)
        u = urlparse(self.path)
        try:
            s = HUB.get()
            body = self._body()
            if u.path == "/act":
                return self._send(s.act(body))
            if u.path == "/answer_dialog":
                return self._send(s.answer_dialog(body.get("option"), body.get("button")))
            if u.path == "/tasks":
                return self._send(HUB.runner.add(int(body["unit_id"]), int(body["x"]), int(body["y"])))
            if u.path == "/hunt":
                if any(h.info()["live"] for h in HUB.hunts):
                    return self._send({"ok": False, "error": "a village hunt is already running"})
                then = body.get("then")
                h = HutHunt(s, HUB.runner, SAVES, int(body["unit_id"]), (int(body["x"]), int(body["y"])),
                            min_moves=int(body.get("min_moves", 2)), max_attempts=int(body.get("max_attempts", 20)),
                            then=tuple(then) if then else None, auto_cycle=bool(body.get("auto_cycle", True)),
                            goal=body.get("goal", "unit"))
                HUB.hunts.append(h.start())
                return self._send({"ok": True, "hunt": h.info()})
            if u.path == "/campaign":
                return self._send(start_campaign(s, body))
            if u.path == "/campaign/cancel":
                if HUB.campaign:
                    HUB.campaign.cancel()
                return self._send({"ok": True})
            if u.path == "/dev/click":
                win32.click_window(int(body["hwnd"]), body.get("x"), body.get("y"))
                return self._send({"ok": True})
            if u.path == "/dev/post":
                r = win32.user32.PostMessageW(int(body["hwnd"]), int(body["msg"]), int(body.get("wparam", 0)),
                                              int(body.get("lparam", 0)))
                return self._send({"ok": bool(r)})
            if u.path == "/dev/send":
                r = win32.user32.SendMessageW(int(body["hwnd"]), int(body["msg"]), int(body.get("wparam", 0)),
                                              int(body.get("lparam", 0)))
                return self._send({"ok": True, "result": r})
            if u.path == "/dev/realclick":
                win32.real_click(int(body["hwnd"]), body.get("x"), body.get("y"))
                return self._send({"ok": True})
            if u.path == "/dev/key":
                win32.post_key(int(body["hwnd"]), body["key"])
                return self._send({"ok": True})
            if u.path == "/dev/children":
                return self._send([w.as_dict() for w in win32.child_windows(int(body["hwnd"]))])
            if u.path == "/dev/menu":
                s.menu(body["label"])
                return self._send({"ok": True})
            if u.path == "/open_city":
                return self._send(s.open_city(int(body["city_id"])))
            if u.path == "/production":
                from .cityops import CityOps, ProductionJob
                if HUB.production and HUB.production.status == "running":
                    return self._send({"ok": False, "error": "a production job is running"})
                HUB.ops = getattr(HUB, "ops", None) or CityOps(s)
                ids = body.get("city_ids", "all")
                HUB.production = ProductionJob(s, HUB.ops, ids, body.get("items") or ["Temple", "Barracks"],
                                               bool(body.get("watch")), bool(body.get("happy")),
                                               after=body.get("after"))
                HUB.production.pause = lambda: (any(h.info()["live"] for h in HUB.hunts) or bool(
                    HUB.campaign and HUB.campaign.status == "hunting"))
                HUB.production.start()
                return self._send({"ok": True, "job": HUB.production.info()})
            if u.path == "/end_turn":
                from .turnsave import end_turn
                return self._send(end_turn(s, SAVES, save=bool(body.get("save", True))))
            if u.path == "/turn_saves":
                HUB.saver.enabled = bool(body.get("enabled", True))
                return self._send(HUB.saver.info())
            if u.path == "/tax_rates":
                from .journal import note as J
                if body.get("method", "memory") == "memory":
                    from .memwrite import MemWriter
                    HUB.memw = getattr(HUB, "memw", None) or MemWriter(s)
                    st = s.state()
                    civ = next(c for c in st["civs"] if c["id"] == st["human_civ"])
                    r = HUB.memw.set_rates(civ, int(body["tax"]), int(body["lux"]), int(body["sci"]),
                                           "set by the player")
                    return self._send(r)
                r = s.set_rates(int(body["tax"]), int(body["lux"]), int(body["sci"]))
                J(s, "kingdom", "set tax rates", f"set by the player: Tax {body['tax']}0% · Lux {body['lux']}0% · "
                  f"Sci {body['sci']}0% → result {r.get('rates')}", ok=r.get("ok"))
                return self._send(r)
            if u.path == "/fix_disorder":
                from .cityops import CityOps
                HUB.ops = getattr(HUB, "ops", None) or CityOps(s)
                return self._send(HUB.ops.fix_disorder(int(body["city_id"])))
            if u.path == "/government_plan":
                s.government_plan = list(body.get("governments", []))
                try:
                    with open(GOV_PLAN_PATH, "w", encoding="utf-8") as f:
                        json.dump(s.government_plan, f)
                except OSError:
                    pass
                from .journal import note as J
                J(s, "research", "government plan", f"player's decision: next government {s.government_plan}")
                return self._send({"ok": True, "government_plan": s.government_plan})
            if u.path == "/revolution":
                from .journal import note as J
                with s._lock:
                    s.menu("REVOLUTION")
                time.sleep(1.2)
                s.refresh()
                p = s.state().get("popup")
                if not p or p.get("section") != "REVOLUTION":
                    return self._send({"ok": False, "error": "REVOLUTION popup did not open", "popup": p})
                r1 = s.answer_dialog(option=0, button="ok")         # "Yes"
                time.sleep(1.0)
                s.refresh()
                p2 = s.state().get("popup")
                if p2 and p2.get("section") == "STARTREV":
                    s.answer_dialog(button="ok")
                civ = next(c for c in s.state()["civs"] if c["id"] == s.state()["human_civ"])
                s.revolution_turn = s.state()["turn"]
                J(s, "research", "revolution", f"approved by the player: Monarchy → {s.government_plan} (Democracy: "
                  "no corruption, rapture growth, rates up to 100%)", government=civ["government"])
                return self._send({"ok": True, "government": civ["government"], "plan": s.government_plan})
            if u.path == "/research_plan":
                s.research_plan = [str(t).strip() for t in body.get("techs", []) if str(t).strip()]
                try:
                    with open(RESEARCH_PLAN_PATH, "w", encoding="utf-8") as f:
                        json.dump(s.research_plan, f)
                except OSError:
                    pass
                from .journal import note as J
                J(s, "research", "research plan", f"changed by the player in the console: {s.research_plan[:8]}"
                  f"{' …' if len(s.research_plan) > 8 else ''}")
                return self._send({"ok": True, "research_plan": s.research_plan})
            if u.path == "/gov_rates":
                job = HUB.production
                if not job or not hasattr(job, "gov_rates"):
                    return self._send({"ok": False, "error": "city_manager is not running"})
                job.gov_rates = {str(k): tuple(int(x) for x in v) for k, v in body.get("policy", {}).items()
                                 if len(v) == 3 and sum(int(x) for x in v) == 10}
                from .journal import note as J
                J(s, "city", "rate policy", f"set by the player in the console: rates on government change {job.gov_rates}")
                return self._send({"ok": True, "rate_policy": {k: list(v) for k, v in job.gov_rates.items()}})
            if u.path == "/buy":
                from .buy import buy
                job = HUB.production
                was_busy = getattr(job, "busy", False) if job else None
                if job:
                    job.busy = True                    # the campaign waits while we use the City Window
                if HUB.runner:
                    HUB.runner.enabled, was = False, HUB.runner.enabled
                try:
                    time.sleep(1.0)
                    return self._send(buy(s, int(body["city_index"]), int(body.get("max_price", 0)),
                                          dry=bool(body.get("dry", False)), why=body.get("why", "set by the player")))
                finally:
                    if job:
                        job.busy = was_busy
                    if HUB.runner:
                        HUB.runner.enabled = was
            if u.path == "/dedupe_city_names":
                from .memwrite import MemWriter
                HUB.memw = getattr(HUB, "memw", None) or MemWriter(s)
                st = s.state()
                names = {c["name"] for c in st["cities"]}
                seen, out = {}, []
                ROMAN = ["II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII", "XIII", "XIV", "XV"]
                for c in sorted(st["cities"], key=lambda c: c["index"]):
                    n = c["name"]
                    if n not in seen:
                        seen[n] = 1
                        continue
                    if c["owner"] != st["human_civ"]:
                        continue
                    k = seen[n]
                    while True:
                        suf = " " + (ROMAN[k - 1] if k - 1 < len(ROMAN) else str(k + 1))
                        new = n[:15 - len(suf)] + suf
                        k += 1
                        if new not in names:
                            break
                    seen[n] = k
                    names.add(new)
                    out.append(HUB.memw.rename_city(c, new, "tell apart cities with the same name (player's request; for Find City)"))
                s.refresh()
                return self._send({"ok": True, "renamed": len(out), "items": out[:400]})
            if u.path == "/rollback":
                return self._send(rollback(s, body.get("name", ""), bool(body.get("restart_campaign", True))))
            if u.path == "/goals":
                g = goals_store().add(body, s.state()["turn"])
                from .journal import note as J
                J(s, "goal", "add goal", f"player added a goal: {g['type']} {g.get('target')} "
                  f"(by turn {g.get('by_turn') or '-'})", goal=g)
                return self._send({"ok": True, "goal": g})
            if u.path == "/goals/delete":
                goals_store().remove(int(body["id"]))
                return self._send({"ok": True})
            if u.path == "/wonders/plan":
                job = HUB.production
                if not job or not hasattr(job, "wonder_plan"):
                    return self._send({"ok": False, "error": "city_manager is not running"})
                w = dict(job.wonders or {})
                if "cities" in body:
                    w["cities"] = [int(i) for i in body["cities"]]
                if "list" in body:
                    w["list"] = [str(n) for n in body["list"]]
                job.wonders = w
                from .journal import note as J
                J(s, "city", "wonder settings", f"changed by the player in the console: cities {w.get('cities')}, "
                  f"priority {w.get('list')}")
                return self._send({"ok": True, "wonders": w})
            if u.path == "/game_options":
                from .memwrite import MemWriter
                HUB.memw = getattr(HUB, "memw", None) or MemWriter(s)
                out = {k: HUB.memw.set_option(k, bool(v), "the full-screen wonder movie froze the game for a long time")
                       for k, v in body.items() if k in HUB.memw.OPTIONS}
                return self._send({"ok": True, **out})
            if u.path == "/city_manager":
                from .memwrite import MemWriter, CityManager
                if HUB.production and HUB.production.status == "running":
                    HUB.production.cancel()
                HUB.memw = getattr(HUB, "memw", None) or MemWriter(s)
                HUB.production = CityManager(s, HUB.memw, body.get("items") or ["Temple", "Barracks"],
                                             body.get("after"), bool(body.get("happy", True)),
                                             wonders=body.get("wonders"), runner=HUB.runner)
                HUB.production.report_path = REPORTS_PATH
                HUB.production.goals = goals_store()
                HUB.production.huts_left = lambda: len(s.game.huts())
                HUB.production.hunt_live = lambda: any(h.info()["live"] for h in HUB.hunts) or bool(
                    HUB.campaign and HUB.campaign.status == "hunting")
                if "gold_reserve" in body:
                    HUB.production.gold_reserve = int(body["gold_reserve"])
                if "buy_wonders" in body:
                    HUB.production.buy_wonders = bool(body["buy_wonders"])
                HUB.production.start()
                return self._send({"ok": True, "job": HUB.production.info()})
            if u.path == "/production/cancel":
                if HUB.production:
                    HUB.production.cancel()
                return self._send({"ok": True})
            if u.path == "/open_city_status":
                from .cityops import CityOps
                HUB.ops = getattr(HUB, "ops", None) or CityOps(s)
                st = s.state()
                c = next(x for x in st["cities"] if x["id"] == int(body["city_id"]))
                cw = HUB.ops.open_city(c)
                return self._send({"ok": True, "city_window": cw, "change": HUB.ops.change_button(cw)})
            if u.path == "/found_cities":
                ids = [int(x) for x in body.get("unit_ids", [])]
                plan = city_sites(s, HUB.runner, ids, int(body.get("max_turns", 6)), 8)
                out = []
                for c in plan["chosen"]:
                    task = HUB.runner.add(c["unit_id"], c["x"], c["y"], then="found_city")
                    why = (f"score {c['score']} (over 21 tiles: food×2 + shields×1.5 + trade, centre {c['centre']}, "
                           f"specials {c['specials']}), reachable in {c['turns']} turns, no overlap with other cities — "
                           f"highest (score − 1.5×turns) within {int(body.get('max_turns', 6))} turns")
                    if task.get("ok") and task["task"]["unit_id"] in HUB.runner.tasks:
                        HUB.runner.tasks[task["task"]["unit_id"]]["why"] = why
                    from .journal import note as J
                    J(s, "runner", "choose city site", why, unit_id=c["unit_id"], site=[c["x"], c["y"]])
                    out.append({**c, "task": task})
                return self._send({"ok": True, "assignments": out, "skipped": plan["no_site"]})
            if u.path == "/activate":
                return self._send(s.activate_unit(int(body["unit_id"])))
            if u.path == "/hunt/cancel":
                for h in HUB.hunts:
                    if h.info()["live"]:
                        h.cancel()
                return self._send({"ok": True})
            if u.path == "/tasks/cancel":
                return self._send(HUB.runner.cancel(int(body["unit_id"])))
            if u.path == "/tasks/settings":
                r = HUB.runner
                if "enabled" in body:
                    r.enabled = bool(body["enabled"])
                if "auto_cycle" in body:
                    r.auto_cycle = bool(body["auto_cycle"])
                return self._send({"enabled": r.enabled, "auto_cycle": r.auto_cycle})
            if u.path in ("/game/save", "/game/load"):
                from . import gameio
                name = os.path.basename(body.get("name") or time.strftime("api_%Y%m%d_%H%M%S.sav"))
                path = os.path.join(SAVES, name if name.lower().endswith(".sav") else name + ".sav")
                fn = gameio.save_game if u.path == "/game/save" else gameio.load_game
                if HUB.runner:
                    HUB.runner.enabled, was = False, HUB.runner.enabled
                try:
                    return self._send(fn(s, path))
                finally:
                    if HUB.runner:
                        HUB.runner.enabled = was
            if u.path == "/keys":
                if body.get("method") not in ("post", "sendinput"):
                    return self._send({"error": "method must be post|sendinput"}, 400)
                s.key_method = HUB.key_method = body["method"]
                return self._send({"key_method": s.key_method})
            return self._send({"error": "not found"}, 404)
        except Exception as e:
            return self._send({"error": f"{type(e).__name__}: {e}"}, 500)


def popup_scan(s):
    """Dev: find where a list-box popup keeps its item texts."""
    from . import memmap as M
    g = s.game
    ptr = g.p.u32(M.CURR_POPUP)
    if not ptr:
        return {"error": "no popup"}
    d = g.p.read(ptr, M.DIALOG_SIZE)

    def text_at(a):
        try:
            b = g.p.read(a, 48)
        except Exception:
            return None
        t = b.split(b"\0")[0]
        return t.decode("latin-1") if len(t) >= 4 and all(32 <= c < 127 for c in t) else None
    out = []
    for off in range(0, M.DIALOG_SIZE - 3, 4):
        v = int.from_bytes(d[off:off + 4], "little")
        if not (0x10000 < v < 0x7FFF0000):
            continue
        t = text_at(v)
        if t:
            out.append({"off": hex(off), "ptr": hex(v), "text": t})
            continue
        try:
            sub = g.p.read(v, 0x40)
        except Exception:
            continue
        for o2 in range(0, 0x40, 4):
            v2 = int.from_bytes(sub[o2:o2 + 4], "little")
            if 0x10000 < v2 < 0x7FFF0000:
                t2 = text_at(v2)
                if t2:
                    out.append({"off": hex(off), "ptr": hex(v), "sub": hex(o2), "text": t2})
    return {"popup": hex(ptr), "hits": out,
            "dwords": {hex(o): hex(int.from_bytes(d[o:o + 4], "little")) for o in range(0, 0x60, 4)}}


def find_spiral(s, lo, hi):
    """Look in the game's data for the city-radius offset tables (21 dx + 21 dy)."""
    from .citysite import RADIUS
    from itertools import product
    wants = [set(RADIUS), {(a, b) for a, b in product(range(-2, 3), repeat=2) if not (abs(a) == 2 and abs(b) == 2)}]
    data = b""
    for a in range(lo, hi, 0x10000):
        try:
            data += s.game.p.read(a, 0x10000)
        except Exception:
            data += bytes(0x10000)
    hits = []
    for e in (1, 2, 4):
        vals = [int.from_bytes(data[i:i + e], "little", signed=True) for i in range(0, len(data) - e, e)] \
            if e > 1 else [b - 256 if b > 127 else b for b in data]
        ok = [-3 <= v <= 3 for v in vals]
        run = 0
        starts = []
        for i in range(len(ok) - 20):
            if ok[i] and all(ok[i:i + 20]) and len(set(vals[i:i + 20])) >= 5:
                starts.append(i)
        for st in starts:
            for n in (21, 20):
                seq = vals[st:st + 2 * n]
                if len(seq) == 2 * n:
                    pairs = list(zip(seq[0::2], seq[1::2]))
                    if len(set(pairs)) == n and any(set(pairs) <= w for w in wants):
                        hits.append({"elem": e, "addr": hex(lo + st * e), "n": n, "interleaved": True,
                                     "pairs": pairs})
                for gap in range(n, n + 16):
                    A, B = vals[st:st + n], vals[st + gap:st + gap + n]
                    if len(B) < n:
                        continue
                    pairs = set(zip(A, B))
                    if len(pairs) == n and any(pairs <= w for w in wants):
                        hits.append({"elem": e, "addr": hex(lo + st * e), "n": n, "gap": gap,
                                     "dx": A, "dy": B})
            if len(hits) > 40:
                break
    return {"hits": hits[:20]}


def start_campaign(s, body):
    if HUB.campaign and HUB.campaign.info()["live"]:
        return {"ok": False, "error": "campaign already running"}
    HUB.campaign_body = dict(body)
    HUB.campaign = Campaign(s, HUB.runner, SAVES, HUB.hunts,
                            target=int(body.get("target", 10)), min_moves=int(body.get("min_moves", 2)),
                            max_attempts=int(body.get("max_attempts", 20)),
                            max_turns=int(body.get("max_turns", 40)),
                            max_turns_to_hut=int(body.get("max_turns_to_hut", 6)),
                            skip_others=bool(body.get("skip_others", True)),
                            end_turns=bool(body.get("end_turns", True)),
                            goal=body.get("goal", "unit"),
                            hunter_moves=body.get("hunter_moves"),
                            keep_last=bool(body.get("keep_last")), near_city=int(body.get("near_city", 2)),
                            explore=bool(body.get("explore", True)))
    HUB.campaign.pause = lambda: bool(HUB.production and HUB.production.busy)

    def _rail():
        mw = getattr(HUB, "memw", None)
        return bool(mw and "RR" in mw.known(s.state()["human_civ"]))
    HUB.campaign.rail_known = _rail
    HUB.campaign.wonder_cities = lambda: list((getattr(HUB.production, "wonders", None) or {}).get("cities", []))
    HUB.campaign.happy_guard = lambda st: bool(HUB.production and HUB.production.happy
                                               and HUB.production.status == "running"
                                               and HUB.production.blocking(st))
    HUB.campaign.start()
    return {"ok": True, "campaign": HUB.campaign.info()}


def rollback(s, name, restart_campaign=True):
    """Load an earlier save (turn rollback). Village hunts and the campaign are stopped first so
    nothing reloads its own save on top; the campaign is started again with the same settings."""
    from . import gameio
    from .journal import note as J
    name = os.path.basename(name or "")
    path = os.path.join(SAVES, name)
    if not name.lower().endswith(".sav") or not os.path.exists(path):
        return {"ok": False, "error": f"{name} not found"}
    was_camp = bool(HUB.campaign and HUB.campaign.info()["live"])
    if HUB.campaign:
        HUB.campaign.cancel()
    for h in HUB.hunts:
        if h.info()["live"]:
            h.cancel()
    t0 = time.time()
    while time.time() - t0 < 60 and (any(h.info()["live"] for h in HUB.hunts)
                                      or (HUB.campaign and HUB.campaign.info()["live"])):
        time.sleep(0.5)
    if any(h.info()["live"] for h in HUB.hunts):
        return {"ok": False, "error": "the village hunt will not stop, please try again"}
    turn0 = s.state()["turn"]
    if HUB.runner:
        HUB.runner.enabled, was = False, HUB.runner.enabled
    try:
        r = gameio.load_game(s, path)
    finally:
        if HUB.runner:
            HUB.runner.enabled = was
    s.refresh()
    J(s, "save", "rollback", f"rolled back by the player in the console: turn {turn0} → {name}", ok=r.get("ok"), file=name)
    out = {"ok": bool(r.get("ok")), "load": r, "turn": s.state()["turn"], "campaign_restarted": False}
    if r.get("ok") and restart_campaign and was_camp and getattr(HUB, "campaign_body", None):
        time.sleep(2)
        out["campaign_restarted"] = start_campaign(s, HUB.campaign_body).get("ok", False)
    return out


REPORTS_PATH = os.path.join(os.path.dirname(HERE), "logs", "turn_reports.jsonl")
_GOALS = None


def goals_store():
    global _GOALS
    if _GOALS is None:
        from .goals import Goals
        _GOALS = Goals(os.path.join(os.path.dirname(HERE), "logs", "goals.json"))
    return _GOALS


def load_reports(limit=60):
    out = []
    try:
        with open(REPORTS_PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    # one row per turn (the last one wins after a reload)
    by_turn = {}
    for r in out:
        by_turn[r["turn"]] = r
    return [by_turn[t] for t in sorted(by_turn)][-limit:]


def wonders_view(s):
    """Wonder cities, their progress, caravans on the way, and every wonder built so far."""
    from .memwrite import MemWriter
    st = s.state()
    r = s.game.rules
    cost = getattr(r, "improve_cost", [])
    me = st["human_civ"]
    cities = {c["index"]: c for c in st["cities"]}
    job = HUB.production
    plan = getattr(job, "wonder_plan", {}) or {}
    settings = getattr(job, "wonders", None)
    live = ("pending", "running", "waiting_next_turn", "paused_popup")
    units = {u["id"]: u for u in st["units"]}
    rows = []
    for idx in (settings or {}).get("cities", []):
        c = cities.get(idx)
        if not c:
            continue
        name = c["producing"]["name"]
        wid = next((i for i, n in enumerate(r.improvements) if n == name), None)
        need = cost[wid] if wid is not None and wid < len(cost) else 0
        coming = [t for t in HUB.runner.tasks.values() if t["status"] in live
                  and t["goal"] == [c["x"], c["y"]] and (units.get(t["unit_id"]) or {}).get("type_name") in ("Caravan", "Freight")]
        eta = [t.get("eta_turns") for t in coming if t.get("eta_turns") is not None]
        rows.append({"index": idx, "city": c["name"], "x": c["x"], "y": c["y"], "size": c["size"],
                     "building": name, "is_wonder": wid is not None and wid >= 39,
                     "shields": c["shields"], "cost": need, "per_turn": c.get("shield_total"),
                     "caravans_on_way": len(coming), "nearest_caravan_turns": min(eta) if eta else None,
                     "caravan_value": 50 * len(coming)})
    built = []
    mw = getattr(HUB, "memw", None) or MemWriter(s)
    HUB.memw = mw
    for wid, ci in sorted(mw.wonders_built().items()):
        c = cities.get(ci)
        built.append({"wonder": r.improvement_name(wid), "city": c["name"] if c else f"#{ci}",
                      "owner": c["owner"] if c else None, "mine": bool(c and c["owner"] == me)})
    caravans = [u for u in st["units"] if u["owner"] == me and u["type_name"] in ("Caravan", "Freight")]
    return {"settings": settings, "plan": {str(k): v for k, v in plan.items()}, "cities": rows,
            "built": built, "caravans_total": len(caravans),
            "caravans_building": sum(1 for c in st["cities"] if c["owner"] == me and c["producing"]["name"] in ("Caravan", "Freight")),
            "job": getattr(job, "status", None)}


def site_finder(s, st):
    from .citysite import SiteFinder, parse_terrain_rows
    from . import memmap as M
    ms = st["map"]
    tiles = s.game.read_tiles(0, 0, ms["width2"], ms["height"])
    me = st["human_civ"]
    civ = next(c for c in st["civs"] if c["id"] == me)
    rows = parse_terrain_rows(getattr(s.game.rules, "terrain_lines", []))
    return SiteFinder(tiles, ms["width2"], ms["height"], st["cities"], rows, s.game.map_seed(),
                      flat=bool(s.game.p.u16(M.GAME) & 0x8000),
                      despotism=civ["government"] in ("Despotism", "Anarchy"))


def install_site_hooks(s, runner):
    """Let found_city tasks re-check their site (villages keep turning into cities) and
    re-plan to the next best free site if it got taken."""
    runner.site_ok = lambda xy: site_finder(s, s.state()).score(tuple(xy)) is not None

    def replan(uid):
        r = city_sites(s, runner, [uid], 6, 1)
        return [r["chosen"][0]["x"], r["chosen"][0]["y"]] if r["chosen"] else None
    runner.site_replan = replan


def city_sites(s, runner, unit_ids, max_turns=6, top=8) -> dict:
    """Best non-overlapping city sites for the given settlers (see citysite.py)."""
    from .citysite import choose
    st = s.state()
    finder = site_finder(s, st)
    planner = runner.planner(st)
    options, no_site = {}, []
    for uid in unit_ids:
        u = next((x for x in st["units"] if x["id"] == uid), None)
        if u is None:
            no_site.append({"unit_id": uid, "error": "not found"})
            continue
        info = runner._unit_info(u)
        reach = planner.reach(info, info["full_mp"], u.get("move_points_left"), max_turns)
        cands = []
        for xy, turns in reach.items():
            sc = finder.score(xy)
            if sc:
                cands.append({**sc, "turns": turns})
        cands.sort(key=lambda c: c["score"] - 1.5 * c["turns"], reverse=True)
        if cands:
            options[uid] = cands
        else:
            no_site.append({"unit_id": uid, "error": "no free site within reach"})
    chosen = choose(finder, options) if options else []
    strip = lambda c: {k: v for k, v in c.items() if k != "area"}
    return {"chosen": [strip(c) for c in chosen], "no_site": no_site,
            "top": {uid: [strip(c) for c in cs[:top]] for uid, cs in options.items()}}


def _log(*a):
    try:
        d = os.path.join(os.path.dirname(HERE), "logs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "server.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a) + "\n")
    except Exception:
        pass


def _stop_other_servers(port: int):
    """Free the port from an older copy of this server (ours only: cmdline has civ2api.server)."""
    if os.name != "nt":
        return
    import subprocess
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*civ2api.server*' "
          f"-and $_.ProcessId -ne {os.getpid()} }} | ForEach-Object {{ $_.ProcessId }}")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                             text=True, timeout=20, creationflags=0x08000000).stdout
        pids = [int(x) for x in out.split() if x.strip().isdigit()]
        _log("other servers:", pids, "me:", os.getpid())
        for pid in pids:
            r = subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                               text=True, creationflags=0x08000000)
            _log("taskkill", pid, r.returncode, (r.stdout or r.stderr).strip()[:120])
        time.sleep(0.5)
    except Exception as e:
        _log("stop_other_servers failed:", e)


def main(argv=None):
    global HUB
    ap = argparse.ArgumentParser(description="Civ2 MGE agent API + web console")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--pid", type=int)
    ap.add_argument("--rules", help="RULES.TXT path (default: next to CIV2.EXE)")
    ap.add_argument("--keys", choices=["post", "sendinput"], default="post",
                    help="post = PostMessage (no focus needed); sendinput = real keyboard")
    ap.add_argument("--poll", type=float, default=0.25)
    a = ap.parse_args(argv)
    _stop_other_servers(a.port)
    HUB = Hub(a.pid, a.rules, a.keys, a.poll)
    _log("starting pid", os.getpid(), "port", a.port)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except Exception as e:
        _log("bind failed:", e)
        raise
    _log("listening")
    srv.daemon_threads = True
    print(f"civ2api console on http://127.0.0.1:{a.port}/  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if HUB.runner:
            HUB.runner.stop()
        if HUB.session:
            HUB.session.stop()
            HUB.session.game.close()


if __name__ == "__main__":
    main()
