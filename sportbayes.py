# -*- coding: utf-8 -*-
"""
SportBayes - NBA a NHL: zive vysledky, kurzy a Bayesovsky pravdepodobnostni model.

    python sportbayes.py            spusti dashboard v prohlizeci
    python sportbayes.py today      vypise dnesni zapasy do konzole
    python sportbayes.py backtest   overi model na historii a doladi parametry
    python sportbayes.py test       zkontroluje, ze funguje zdroj dat
    python sportbayes.py build      vygeneruje hotovou stranku docs/index.html (GitHub)

V prohlizeci je krome lig take "Denik sazek" (denik.html) a "Nastaveni" (nastaveni.html).

Za prikaz lze pridat ligu: nba nebo nhl (napr. python sportbayes.py backtest nhl).
"""
import csv
import datetime as dt
import html
import json
import math
import os
import shutil
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import data
from model import Params, calibration, default_params, metrics, run, tune, tune_draw

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
PRED_FIELDS = ["event_id", "date_utc", "home", "away", "p_home", "p_draw",
               "odds_home", "odds_draw", "odds_away", "source", "logged_at"]
DEFAULTS = {
    "port": 8765,
    "refresh_sec": 60,
    "seasons_back": 4,
    "min_edge": 0.05,
    "suspicious_edge": 0.15,
    "bankroll": 1000,
    "kelly_fraction": 0.25,
    "max_stake_pct": 0.03,
    "odds_api_key": "",
    "odds_api_region": "eu",
    "odds_api_refresh_min": 120,
    "leagues": ["nba", "nhl", "epl", "laliga", "seriea", "ligue1", "bundesliga", "extraliga", "czliga"],
    "apisports_key": "",
    "params": {},
}
DNY = ["po", "út", "st", "čt", "pá", "so", "ne"]


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except ValueError as ex:
            print("config.json je poškozený, používám výchozí hodnoty:", ex)
    else:
        save_config(cfg)
    cfg["leagues"] = list(cfg.get("leagues") or DEFAULTS["leagues"])
    known = cfg.get("leagues_known") or cfg["leagues"]
    for lg in DEFAULTS["leagues"]:               # nove pridane ligy se zapnou samy
        if lg not in known and lg not in cfg["leagues"]:
            cfg["leagues"].append(lg)
    cfg["leagues_known"] = list(DEFAULTS["leagues"])
    data.APISPORTS_KEY = (os.environ.get("APISPORTS_KEY") or cfg.get("apisports_key") or "").strip()
    if os.environ.get("ODDS_API_KEY"):          # na GitHubu jde klic z tajnych promennych
        cfg["odds_api_key"] = os.environ["ODDS_API_KEY"].strip()
    return cfg


def save_config(cfg):
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def params_key(lg):
    return "params" if lg == "nba" else "params_" + lg


def get_params(cfg, lg="nba"):
    p = default_params(data.LEAGUES[lg]["params"])
    for k, v in (cfg.get(params_key(lg)) or {}).items():
        if hasattr(p, k):
            setattr(p, k, float(v))
    return p


# ---------------------------------------------------------------- nastaveni a denik sazek
SETTINGS_FILE = os.path.join(HERE, "nastaveni.json")
DIARY_FILE = os.path.join(HERE, "denik.json")
DIARY_LOCK = threading.Lock()
STATUSES = ("open", "won", "lost", "void")


def save_json(path, obj, backup=False):
    if backup and os.path.exists(path):          # pred kazdym zapisem zustane kopie predchoziho stavu
        try:
            shutil.copy2(path, os.path.splitext(path)[0] + "_zaloha.json")
        except OSError:
            pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def load_settings(cfg, use_file=True):
    """Poradi a viditelnost lig v dashboardu (soubor nastaveni.json).
    Hotove stranky pro GitHub (use_file=False) ho nepouzivaji, tam to resi prohlizec."""
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            s = json.load(f) if use_file else {}
    except (OSError, ValueError):
        s = {}
    allowed = [l for l in cfg.get("leagues") or [] if l in data.LEAGUES]
    order = [l for l in (s.get("order") or []) if l in allowed]
    order += [l for l in allowed if l not in order]
    hidden = [l for l in (s.get("hidden") or []) if l in allowed]
    return {"order": order, "hidden": hidden}


def visible_leagues(cfg):
    st = load_settings(cfg)
    return [l for l in st["order"] if l not in st["hidden"]]


def diary_load():
    if not os.path.exists(DIARY_FILE):
        return []
    try:
        with open(DIARY_FILE, encoding="utf-8") as f:
            lst = json.load(f)
        if isinstance(lst, list):
            return [x for x in lst if isinstance(x, dict)]
    except (OSError, ValueError):
        pass
    try:                                          # poskozeny soubor se nikdy neprepise, jen odlozi stranou
        os.replace(DIARY_FILE, os.path.splitext(DIARY_FILE)[0] + "_poskozeny.json")
    except OSError:
        pass
    return []


def _num(v):
    try:
        x = float(str(v).replace(",", "."))
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def clean_entry(d):
    st = d.get("status")
    return {"id": str(d.get("id") or "")[:40], "created": str(d.get("created") or "")[:30],
            "date": str(d.get("date") or "")[:10], "lg": str(d.get("lg") or "")[:20],
            "event_id": str(d.get("event_id") or "")[:40], "match": str(d.get("match") or "")[:120],
            "pick": str(d.get("pick") or "")[:6], "tip": str(d.get("tip") or "")[:80],
            "odds": _num(d.get("odds")), "stake": _num(d.get("stake")), "p": _num(d.get("p")),
            "status": st if st in STATUSES else "open", "note": str(d.get("note") or "")[:300],
            "auto": bool(d.get("auto"))}


def diary_apply(op):
    """Jedna operace nad denikem (add / set / del); vrati cely aktualni seznam."""
    with DIARY_LOCK:
        lst = diary_load()
        kind = op.get("op")
        if kind == "add":
            e = clean_entry(op.get("entry") or {})
            if not e["id"] or not e["odds"] or e["odds"] <= 1 or not e["stake"] or e["stake"] <= 0:
                raise ValueError("neplatná sázka")
            if not any(x.get("id") == e["id"] for x in lst):
                lst.append(e)
        elif kind == "set":
            for x in lst:
                if x.get("id") != op.get("id"):
                    continue
                for k, v in (op.get("fields") or {}).items():
                    if k in ("odds", "stake"):
                        v = _num(v)
                        if not v or v <= (1 if k == "odds" else 0):
                            continue
                    elif k == "status":
                        if v not in STATUSES:
                            continue
                        x["auto"] = False
                    elif k in ("note", "tip", "match", "date"):
                        v = str(v)[:300]
                    else:
                        continue
                    x[k] = v
        elif kind == "del":
            lst = [x for x in lst if x.get("id") != op.get("id")]
        else:
            raise ValueError("neznámá operace")
        save_json(DIARY_FILE, lst, backup=True)
        return lst


def diary_settle(apps):
    """Otevrene sazky na zapasy z dashboardu se samy vyhodnoti, jakmile je zapas odehrany."""
    open_lgs = {x.get("lg") for x in diary_load() if x.get("status") == "open" and x.get("event_id")}
    for lg in open_lgs:
        if lg in apps:
            apps[lg].ensure_fresh()
    with DIARY_LOCK:
        lst = diary_load()
        res = {}
        changed = False
        for x in lst:
            if x.get("status") != "open" or not x.get("event_id") or x.get("lg") not in apps:
                continue
            if x["lg"] not in res:
                res[x["lg"]] = {r["event_id"]: r for r in apps[x["lg"]].rows}
            g = res[x["lg"]].get(x["event_id"])
            if not g or g.get("home_pts") is None or g.get("away_pts") is None:
                continue
            won = result_side(g["home_pts"], g["away_pts"])
            x["status"] = "won" if x.get("pick") == won else "lost"
            x["auto"] = True
            changed = True
        if changed:
            save_json(DIARY_FILE, lst, backup=True)
        return lst


# ---------------------------------------------------------------- log predikci
def load_preds(lg="nba"):
    out = {}
    if os.path.exists(data.preds_csv(lg)):
        with open(data.preds_csv(lg), newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                out[r["event_id"]] = r
    return out


def save_preds(preds, lg="nba"):
    os.makedirs(data.DATA_DIR, exist_ok=True)
    tmp = data.preds_csv(lg) + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=PRED_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in sorted(preds.values(), key=lambda r: r["date_utc"]):
            w.writerow(r)
    os.replace(tmp, data.preds_csv(lg))


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


LBL = {"home": "D", "draw": "R", "away": "H"}


def result_side(home_pts, away_pts):
    return "home" if home_pts > away_pts else ("away" if away_pts > home_pts else "draw")


def log_probs(p, sides):
    """Pravdepodobnosti ze zalogovaneho radku (stare soubory nemaji p_draw)."""
    ph = fnum(p.get("p_home"))
    if ph is None:
        return None
    pd = (fnum(p.get("p_draw")) or 0.0) if "draw" in sides else 0.0
    return {"home": ph, "draw": pd, "away": max(0.0, 1.0 - ph - pd)}


def log_odds(p, sides):
    o = {k: fnum(p.get("odds_" + k)) for k in sides}
    if all(o.values()):
        o["source"] = p.get("source") or ""
        return o
    return None


def track_record(preds, rows, min_edge, sides=("home", "away")):
    """Jak si model vede na zapasech, ktere predpovedel PRED zacatkem."""
    res = {r["event_id"]: r for r in rows}
    n = ok = 0
    ll = 0.0
    bets = wins = 0
    pnl = 0.0
    for e, p in preds.items():
        g = res.get(e)
        ps = log_probs(p, sides)
        if not g or ps is None:
            continue
        won = result_side(g["home_pts"], g["away_pts"])
        if won not in sides:
            continue
        n += 1
        ok += max(sides, key=lambda k: ps[k]) == won
        ll -= math.log(min(max(ps[won], 1e-9), 1.0))
        o = log_odds(p, sides)
        if o:
            edges = {k: ps[k] * o[k] - 1 for k in sides}
            side = max(sides, key=lambda k: edges[k])
            if edges[side] >= min_edge:
                bets += 1
                if side == won:
                    wins += 1
                    pnl += o[side] - 1
                else:
                    pnl -= 1
    return {"n": n, "acc": ok / n if n else None, "logloss": ll / n if n else None,
            "bets": bets, "wins": wins, "pnl": pnl}


def confidence_table(hist, rows, sides):
    """Uspesnost podle jistoty modelu: zapasy z walk-forward behu (predpoved vznikla PRED zapasem),
    bez prvni sezony (model se v ni teprve uci)."""
    seasons = sorted({r["season"] for r in rows})
    skip = seasons[0] if len(seasons) > 1 else None
    res = {r["event_id"]: r for r in rows}
    pts = []
    for e, p in hist.items():
        g = res.get(e)
        if not g or g["season"] == skip:
            continue
        won = result_side(g["home_pts"], g["away_pts"])
        if won not in sides:
            continue
        top = max(sides, key=lambda k: p[k])
        pts.append((p[top], top == won))
    out = []
    for name, lo, hi in (("pod 50 %", 0, .5), ("50–60 %", .5, .6), ("60–70 %", .6, .7), ("70 % a víc", .7, 1.01), ("60 % a víc", .6, 1.01)):
        sel = [x for x in pts if lo <= x[0] < hi]
        if sel:
            out.append({"name": name, "n": len(sel), "acc": sum(x[1] for x in sel) / len(sel),
                        "avg": sum(x[0] for x in sel) / len(sel)})
    return out


# ---------------------------------------------------------------- jadro aplikace
class App:
    def __init__(self, cfg, lg="nba"):
        self.cfg = cfg
        self.lg = lg
        self.info = data.LEAGUES[lg]
        self.sides = ("home", "draw", "away") if self.info["three_way"] else ("home", "away")
        self.record = {}
        self.lock = threading.Lock()
        self.rows = []
        self.net = None
        self.hist = {}          # event_id -> predzapasove pravdepodobnosti z walk-forward behu
        self.board = []
        self.updated = None
        self.error = ""
        self.last_refresh = 0.0

    def rebuild(self):
        games = data.to_model_games(self.rows, self.lg)
        self.net, preds, _ = run(games, get_params(self.cfg, self.lg))
        self.hist = {p["event_id"]: {"home": p["p_home"], "draw": p["p_draw"], "away": p["p_away"]}
                     for p in preds if p.get("event_id")}

    def init(self, progress=None):
        self.rows, new = data.update_history(int(self.cfg["seasons_back"]), progress, self.lg)
        self.rebuild()
        return new

    def _odds(self, ev, api_odds):
        o = data.match_odds_api(api_odds, ev) or ev.get("odds")
        if o and all(o.get(k) for k in self.sides):
            return o
        return None

    def _window(self, today, lo, hi):
        """Zapasy v zobrazenem okne. Kdyz zdroj neumi rozsah dni, vzdalenejsi dny
        se nacitaji jen jednou za pul hodiny, aby obnova netrvala dlouho."""
        if data.is_apisports(self.lg):
            return data.as_window(self.lg)
        if data.is_ch(self.lg):
            return data.ch_window(self.lg)
        d1, d2 = today + dt.timedelta(days=lo), today + dt.timedelta(days=hi)
        if data._CHUNK["days"] > 1:
            return data.fetch_range(d1, d2, None, None, self.lg)
        cache = self.__dict__.setdefault("_days", {})
        out, now, day = {}, time.time(), d1
        while day <= d2:
            near = abs((day - today).days) <= 1
            ts, evs = cache.get(day, (0.0, []))
            if now - ts > (0 if near else 1800):
                evs = data.fetch_scoreboard(day, None, self.lg)
                cache[day] = (now, evs)
            for e in evs:
                out[e["event_id"]] = e
            day += dt.timedelta(days=1)
        for k in [k for k in cache if k < d1]:
            del cache[k]
        return list(out.values())

    def refresh(self):
        cfg = self.cfg
        today = data.us_today()
        lo, hi = self.info["window"]
        got = self._window(today, lo, hi)
        evs = [e for e in got if data.keep_event(e, self.lg)]
        if data.merge_finals(self.rows, evs, self.lg):
            data.save_games(self.rows, self.lg)
            self.rebuild()
        api_odds = {}
        if cfg.get("odds_api_key") and any(e["state"] == "pre" for e in evs):
            api_odds = data.fetch_odds_api(cfg["odds_api_key"], cfg.get("odds_api_region", "eu"),
                                           float(cfg.get("odds_api_refresh_min", 120)), self.lg)
        logged = load_preds(self.lg)
        changed = False
        known = set(self.net.teams)
        open_league = not self.info["min_team_games"]      # fotbal: novacek je normalni tym
        board = []
        for e in sorted(evs, key=lambda e: e["date_utc"]):
            d = data.parse_utc(e["date_utc"])
            day = data.us_day(d).toordinal()
            odds = self._odds(e, api_odds)
            lg = logged.get(e["event_id"])
            ps = None
            b2b = (False, False)
            if e["state"] == "post":
                ps = self.hist.get(e["event_id"]) or (log_probs(lg, self.sides) if lg else None)
            elif open_league or (e["home"] in known and e["away"] in known):
                pr = self.net.predict(e["home"], e["away"], day, bool(e["neutral"]))
                ps = {"home": pr["p_home"], "draw": pr["p_draw"], "away": pr["p_away"]}
                b2b = (pr["b2b_home"], pr["b2b_away"])
            if e["state"] != "pre" and lg:          # po zacatku plati zalogovane kurzy
                odds = log_odds(lg, self.sides) or odds
            if e["state"] == "pre" and ps is not None:
                rec = {"event_id": e["event_id"], "date_utc": e["date_utc"],
                       "home": e["home"], "away": e["away"], "p_home": f"{ps['home']:.4f}",
                       "p_draw": f"{ps['draw']:.4f}" if "draw" in self.sides else "",
                       "odds_draw": "",
                       "logged_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}
                for k in self.sides:
                    rec["odds_" + k] = odds[k] if odds else (lg or {}).get("odds_" + k, "")
                rec["source"] = odds.get("source", "") if odds else (lg or {}).get("source", "")
                if not lg or any(str(lg.get(k) or "") != str(rec[k]) for k in PRED_FIELDS[4:10]):
                    logged[e["event_id"]] = rec
                    changed = True
            board.append(self._row(e, d, ps, b2b, odds))
        if changed:
            save_preds(logged, self.lg)
        self.board = board
        self.record = track_record(logged, self.rows, float(cfg["min_edge"]), self.sides)
        self.updated = dt.datetime.now()
        self.error = ""

    def _row(self, e, d, ps, b2b, odds):
        cfg = self.cfg
        loc = d.astimezone()
        row = dict(e)
        row.update({"when": f"{DNY[loc.weekday()]} {loc.day}.{loc.month}. {loc:%H:%M}",
                    "p": ps, "b2b": b2b, "odds": odds, "fair": None, "edge": None,
                    "pick": None, "stake": None, "flag": ""})
        if odds:
            o = {k: float(odds[k]) for k in self.sides}
            if odds.get("fair_home"):
                row["fair"] = {k: odds.get("fair_" + k) for k in self.sides}
            else:
                tot = sum(1 / o[k] for k in self.sides)
                row["fair"] = {k: (1 / o[k]) / tot for k in self.sides}
            if ps is not None:
                edges = {k: ps[k] * o[k] - 1 for k in self.sides}
                row["edge"] = edges
                side = max(self.sides, key=lambda k: edges[k])
                if edges[side] >= float(cfg["min_edge"]):
                    row["pick"] = side
                    kelly = edges[side] / (o[side] - 1) * float(cfg["kelly_fraction"])
                    kelly = min(kelly, float(cfg["max_stake_pct"]))
                    row["stake"] = round(kelly * float(cfg["bankroll"]))
                    if edges[side] >= float(cfg["suspicious_edge"]):
                        row["flag"] = "velký rozdíl proti trhu – ověř zranění"
        return row

    def ensure_fresh(self, force=False):
        with self.lock:
            if force or time.time() - self.last_refresh > float(self.cfg["refresh_sec"]):
                try:
                    self.refresh()
                except Exception as ex:           # sit spadla -> ukaz posledni data
                    self.error = f"{type(ex).__name__}: {ex}"
                self.last_refresh = time.time()


# ---------------------------------------------------------------- HTML
CSS = """
:root{--bg:#10141a;--card:#181e27;--line:#2a323f;--tx:#e6e9ee;--mut:#8b96a5;
--good:#3ecf8e;--warn:#f0b429;--bad:#ef6a6a;--acc:#6aa9ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);
font:14px/1.45 "Segoe UI",system-ui,sans-serif;padding:22px 26px 60px}
h1{font-size:21px;margin:0 0 2px}h2{font-size:15px;margin:30px 0 10px;color:var(--mut);
font-weight:600;text-transform:uppercase;letter-spacing:.06em}
.sub{color:var(--mut);margin-bottom:18px}.err{background:#3a1d1d;border:1px solid var(--bad);
padding:9px 13px;border-radius:8px;margin:10px 0}
table{border-collapse:collapse;width:100%;background:var(--card);border-radius:10px;overflow:hidden}
th,td{padding:8px 11px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--mut);font-weight:600;font-size:12px}td.l,th.l{text-align:left}
tr:last-child td{border-bottom:0}.num{font-variant-numeric:tabular-nums}
.fb{background:var(--card);border:1px solid var(--line);color:var(--tx);border-radius:6px;padding:4px 12px;cursor:pointer;margin:0 6px 6px 0;font:inherit}.fb.on{outline:2px solid var(--acc)}
.fav1{color:var(--acc);font-weight:700}.fav2{color:var(--good);font-weight:700}.fav0{color:var(--warn);font-weight:700}
.mut{color:var(--mut)}.good{color:var(--good);font-weight:600}.bad{color:var(--bad)}
.warn{color:var(--warn)}.live{color:var(--bad);font-weight:600}
.bar{display:inline-block;width:90px;height:8px;background:#2a323f;border-radius:4px;
vertical-align:middle;margin:0 7px;overflow:hidden}.bar i{display:block;height:100%;background:var(--acc)}
.tag{display:inline-block;background:#1f3a2e;color:var(--good);border-radius:5px;padding:1px 7px;font-weight:600}
.b2b{font-size:11px;color:var(--warn);margin-left:4px}
input.o{width:58px;background:#0f1319;border:1px solid var(--line);color:var(--tx);
border-radius:5px;padding:3px 5px;text-align:right;font:inherit}
.cards{display:flex;gap:12px;flex-wrap:wrap}.card{background:var(--card);border-radius:10px;
padding:12px 16px;min-width:150px}.card b{display:block;font-size:20px}.card span{color:var(--mut);font-size:12px}
.note{color:var(--mut);max-width:900px;margin-top:14px}.wrap{overflow-x:auto}
tr.pick td{background:#15241d}
.nav{display:flex;gap:8px;margin:10px 0 12px;flex-wrap:wrap}.nav a{color:var(--tx);text-decoration:none;
background:var(--card);border:1px solid var(--line);border-radius:8px;padding:7px 14px}
.nav a.cur{background:var(--acc);border-color:var(--acc);color:#0b1220;font-weight:600}
.nav a.tool{border-style:dashed}.nav a.tool.cur{border-style:solid}
button,select,input.t{font:inherit;background:#0f1319;border:1px solid var(--line);color:var(--tx);
border-radius:6px;padding:4px 9px}button{cursor:pointer}button:hover{border-color:var(--acc)}
button.bet{padding:1px 7px;margin-right:3px;font-weight:600;color:var(--acc)}
button.pri{background:var(--acc);border-color:var(--acc);color:#0b1220;font-weight:600;padding:6px 16px}
button.x{color:var(--bad);padding:1px 8px}
input.t,.form select{width:100%;min-width:70px}input.n{width:76px;text-align:right}
.toast{position:fixed;right:18px;bottom:18px;background:#1f3a2e;color:var(--good);border:1px solid #24553f;
border-radius:8px;padding:10px 16px;z-index:9}.toast.bad{background:#3a1d1d;color:var(--bad);border-color:var(--bad)}
.form{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;background:var(--card);
border-radius:10px;padding:12px;align-items:end}.form label{display:block;color:var(--mut);font-size:12px;margin-bottom:2px}
.lgs td{vertical-align:middle}.lgs button{padding:1px 9px}
@media(max-width:820px){
body{padding:14px 10px 50px}
table.games,table.games tbody,table.games tr,table.games td{display:block;width:100%}
table.games{background:none}
table.games tr:first-child{display:none}
table.games tr{background:var(--card);border-radius:10px;margin-bottom:10px;padding:8px 4px}
table.games tr.pick{background:#15241d;border:1px solid #24553f}
table.games td{display:block;overflow:hidden;border:0;padding:5px 10px;white-space:normal;text-align:right}
table.games td::before{content:attr(data-l);color:var(--mut);font-size:12px;float:left;margin-right:10px}
table.games td.tm{font-size:16px;text-align:left;padding-bottom:6px}
table.games td.tm::before{content:none}
table.games td:empty{display:none}
.bar{width:60px}input.o{width:70px;padding:6px}
.card{flex:1 1 40%;min-width:0}
}
"""

JS_COMMON = """
function p2(n){return (n<10?'0':'')+n}
function toast(t,bad){var d=document.createElement('div');d.className='toast'+(bad?' bad':'');
  d.textContent=t;document.body.appendChild(d);setTimeout(function(){d.remove()},3200)}
var DB={
 ls:function(){try{return JSON.parse(localStorage.getItem('sb_denik')||'[]')}catch(e){return []}},
 lsSave:function(l){try{localStorage.setItem('sb_denik',JSON.stringify(l))}catch(e){}},
 apply:function(l,op){
  if(op.op==='add'){l.push(op.entry)}
  else if(op.op==='set'){l.forEach(function(e){if(e.id===op.id)for(var k in op.fields)e[k]=op.fields[k]})}
  else if(op.op==='del'){l=l.filter(function(e){return e.id!==op.id})}
  return l},
 load:function(cb){
  if(STATIC){cb(DB.ls());return}
  fetch('api/denik').then(function(r){return r.json()}).then(cb)
   .catch(function(){toast('Deník se nepodařilo načíst - běží aplikace?',1)})},
 send:function(op,cb){
  if(STATIC){var l=DB.apply(DB.ls(),op);DB.lsSave(l);if(cb)cb(l);return}
  fetch('api/denik',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(op)})
   .then(function(r){if(!r.ok)throw 0;return r.json()}).then(function(l){if(cb)cb(l)})
   .catch(function(){toast('Uložení se nepodařilo - běží aplikace?',1)})}
};
if(STATIC){try{var S0=JSON.parse(localStorage.getItem('sb_set')||'null');
 if(S0){var nv=document.querySelector('.nav'),tl=nv.querySelector('a.tool'),as={};
  nv.querySelectorAll('a[data-lg]').forEach(function(a){as[a.dataset.lg]=a});
  (S0.order||[]).forEach(function(l){if(as[l])nv.insertBefore(as[l],tl)});
  (S0.hidden||[]).forEach(function(l){if(as[l]&&!as[l].classList.contains('cur'))as[l].style.display='none'})}}catch(e){}}
"""

JS_BET = """
function addBet(btn){
  var tr=btn.closest('tr'),i=+btn.dataset.i,N=JSON.parse(tr.dataset.nm),
      P=tr.dataset.p?tr.dataset.p.split(',').map(parseFloat):[],M=tr.dataset.mo?tr.dataset.mo.split(','):[],
      inp=tr.querySelectorAll('input.o')[i],mine=((inp&&inp.value)||'').replace(',','.');
  var odds=prompt('Sázka na: '+N[i]+'\\nKurz sázkovky:',mine||M[i]||'');
  if(odds===null)return;
  odds=parseFloat(String(odds).replace(',','.'));
  if(!(odds>1)){toast('Neplatný kurz',1);return}
  var def=(tr.dataset.pk===btn.dataset.k&&tr.dataset.st)||'';
  var stake=prompt('Vklad v Kč:',def);
  if(stake===null)return;
  stake=parseFloat(String(stake).replace(',','.'));
  if(!(stake>0)){toast('Neplatný vklad',1);return}
  var d=new Date(tr.dataset.d);
  DB.send({op:'add',entry:{id:Date.now().toString(36)+Math.random().toString(36).slice(2,6),
    created:new Date().toISOString(),date:d.getFullYear()+'-'+p2(d.getMonth()+1)+'-'+p2(d.getDate()),
    lg:tr.dataset.lg,event_id:tr.dataset.id,match:tr.dataset.m,pick:btn.dataset.k,tip:N[i],odds:odds,
    stake:stake,p:isNaN(P[i])?null:P[i],status:'open',note:''}},
    function(){toast('Zapsáno do deníku: '+N[i]+' @ '+odds)});
}
"""

JS = """
function upd(inp){
  var tr=inp.closest('tr'),P=tr.dataset.p.split(',').map(parseFloat),S=tr.dataset.s.split(','),id=tr.dataset.id;
  var a=tr.querySelectorAll('input.o'),out=tr.querySelector('.my'),t=[],v=[];
  for(var i=0;i<a.length;i++){v.push(a[i].value);
    var o=parseFloat(a[i].value.replace(',','.'));
    if(o>1){var e=P[i]*o-1;t.push('<span class="'+(e>=MIN?'good':(e<0?'bad':''))+'">'+S[i]+' '+(e*100).toFixed(1)+' %</span>')}}
  try{localStorage.setItem('sb_'+id,JSON.stringify(v))}catch(e){}
  out.innerHTML=t.join(' &nbsp; ');
}
document.querySelectorAll('tr[data-id]').forEach(function(tr){
  var a=tr.querySelectorAll('input.o');if(!a.length)return;
  try{var s=JSON.parse(localStorage.getItem('sb_'+tr.dataset.id)||'null');
      if(s){for(var i=0;i<a.length&&i<s.length;i++)a[i].value=s[i];upd(a[0])}}catch(e){}
});
var DNY=['ne','po','\u00fat','st','\u010dt','p\u00e1','so'];
document.querySelectorAll('[data-t]').forEach(function(el){var d=new Date(el.dataset.t);
  if(!isNaN(d))el.textContent=DNY[d.getDay()]+' '+d.getDate()+'.'+(d.getMonth()+1)+'. '+p2(d.getHours())+':'+p2(d.getMinutes())});
document.querySelectorAll('[data-now]').forEach(function(el){var d=new Date(el.dataset.now);
  if(!isNaN(d))el.textContent=d.getDate()+'.'+(d.getMonth()+1)+'. '+p2(d.getHours())+':'+p2(d.getMinutes())});
setInterval(function(){var el=document.activeElement;
  if(!(el&&el.tagName==='INPUT'))location.reload()},REFRESH*1000);
"""


def pct(v, d=0):
    return "–" if v is None else f"{v * 100:.{d}f} %"


def team_stats(app, min_edge, prior_min=10):
    """Za kazdy tym: jak uspesne ho model v historii predpovidal (walk-forward, tedy bez
    znalosti vysledku) a jak dopadly tipy modelu i moje sazky na nej."""
    sides = app.sides
    st = {}

    def T(t):
        return st.setdefault(t, {"n": 0, "hit": 0, "ll": 0.0, "hn": 0, "hhit": 0, "an": 0, "ahit": 0,
                                 "bets": 0, "wins": 0, "pnl": 0.0, "mb": 0, "mw": 0, "mp": 0.0})
    cnt = {}
    for r in sorted(app.rows, key=lambda r: (r["date_utc"], r["event_id"])):
        h, a = r["home"], r["away"]
        ps = app.hist.get(r["event_id"])
        enough = cnt.get(h, 0) >= prior_min and cnt.get(a, 0) >= prior_min
        cnt[h] = cnt.get(h, 0) + 1
        cnt[a] = cnt.get(a, 0) + 1
        if not ps or not enough:
            continue
        won = result_side(r["home_pts"], r["away_pts"])
        if won not in sides:
            continue
        hit = max(sides, key=lambda k: ps[k]) == won
        ll = -math.log(min(max(ps[won], 1e-9), 1.0))
        for t, home in ((h, True), (a, False)):
            x = T(t)
            x["n"] += 1
            x["hit"] += hit
            x["ll"] += ll
            x["hn" if home else "an"] += 1
            x["hhit" if home else "ahit"] += hit
    res = {r["event_id"]: r for r in app.rows}
    for e, p in load_preds(app.lg).items():                    # tipy modelu s kurzy, ktere aplikace zalogovala
        g = res.get(e)
        ps = log_probs(p, sides)
        o = log_odds(p, sides)
        if not g or ps is None or not o:
            continue
        won = result_side(g["home_pts"], g["away_pts"])
        edges = {k: ps[k] * o[k] - 1 for k in sides}
        side = max(sides, key=lambda k: edges[k])
        if edges[side] < min_edge or side == "draw":
            continue
        x = T(g["home"] if side == "home" else g["away"])
        x["bets"] += 1
        if side == won:
            x["wins"] += 1
            x["pnl"] += o[side] - 1
        else:
            x["pnl"] -= 1
    names = {}
    for r in app.board:
        names[r["home_name"] or r["home"]] = r["home"]
        names[r["away_name"] or r["away"]] = r["away"]
    for d in diary_load():                                      # moje skutecne sazky z deniku
        if d.get("lg") != app.lg or d.get("status") not in ("won", "lost") or not d.get("odds") or not d.get("stake"):
            continue
        t = names.get(d.get("tip"), d.get("tip"))
        if t in st or t in cnt:
            x = T(t)
            x["mb"] += 1
            x["mw"] += d["status"] == "won"
            x["mp"] += d["stake"] * (d["odds"] - 1) if d["status"] == "won" else -d["stake"]
    return {t: x for t, x in st.items() if x["n"] or x["bets"] or x["mb"]}


JS_TEAMS = """
(function(){
var tb=document.getElementById('ts');if(!tb)return;
var cur={k:'hit',d:-1};
function apply(){
  var min=parseInt(document.getElementById('ts_min').value)||0,rows=[].slice.call(tb.querySelectorAll('tr[data-n]'));
  rows.sort(function(a,b){var x=parseFloat(a.dataset[cur.k]),y=parseFloat(b.dataset[cur.k]);
    if(isNaN(x))x=-1e9;if(isNaN(y))y=-1e9;return (x-y)*cur.d||a.dataset.t.localeCompare(b.dataset.t)});
  rows.forEach(function(r,i){tb.appendChild(r);r.style.display=(+r.dataset.n<min)?'none':'';r.cells[0].textContent=i+1});
}
tb.querySelectorAll('th[data-k]').forEach(function(th){th.style.cursor='pointer';th.onclick=function(){
  var k=th.dataset.k;cur.d=cur.k===k?-cur.d:-1;cur.k=k;apply()}});
document.getElementById('ts_min').oninput=apply;apply();
})();
"""


JS_FILTER = """
(function(){
var bar=document.querySelector('.fbar');if(!bar)return;
var tb=document.querySelector('table.games'),msg=document.getElementById('fb_msg'),on={};
function apply(){
  var act=Object.keys(on).filter(function(k){return on[k]}),n=0;
  [].forEach.call(tb.querySelectorAll('tr[data-id]'),function(tr){
    var ok=act.length===0||act.some(function(k){return k==='ob'?tr.dataset.ob==='1':tr.dataset.fc===k});
    tr.style.display=ok?'':'none';if(ok)n++});
  [].forEach.call(bar.querySelectorAll('.fb'),function(b){
    b.classList.toggle('on',b.dataset.f==='all'?act.length===0:!!on[b.dataset.f])});
  var t=act.length?' zobrazeno '+n+' zápasů':'';
  if(on.ob)t+=bar.dataset.ob?' · outsider: '+bar.dataset.ob:' · žádný zápas nemá kurz trhu';
  msg.textContent=t;
  try{sessionStorage.setItem('sb_gf',JSON.stringify(on))}catch(e){}}
bar.addEventListener('click',function(ev){var b=ev.target.closest('.fb');if(!b)return;
  if(b.dataset.f==='all')on={};else on[b.dataset.f]=!on[b.dataset.f];apply()});
try{on=JSON.parse(sessionStorage.getItem('sb_gf')||'{}')||{}}catch(e){on={}}
if(typeof on!=='object')on={};apply();
})();
"""


def nav_html(cfg, cur, static=False):
    st = load_settings(cfg, not static)
    items = []
    for lg in st["order"]:
        if lg in st["hidden"] and lg != cur and not static:
            continue
        href = "index.html" if lg == "nba" else lg + ".html"
        c = " cur" if lg == cur else ""
        items.append(f"<a data-lg='{lg}' class='lg{c}' href='{href}'>{data.LEAGUES[lg]['name']} · {data.LEAGUES[lg]['sport']}</a>")
    items.append(f"<a class='tool{' cur' if cur == 'favoriti' else ''}' href='favoriti.html'>★ Favoriti</a>")
    items.append(f"<a class='tool{' cur' if cur == 'denik' else ''}' href='denik.html'>Deník sázek</a>")
    items.append(f"<a class='tool{' cur' if cur == 'nastaveni' else ''}' href='nastaveni.html'>⚙ Nastavení</a>")
    return "<div class='nav'>" + "".join(items) + "</div>"


def page_open(title, h1, cfg, cur, static=False, refresh=None):
    meta = f"<meta http-equiv='refresh' content='{refresh}'>" if refresh else ""
    return ["<!doctype html><html lang='cs'><head><meta charset='utf-8'>",
            "<meta name='viewport' content='width=device-width,initial-scale=1'>", meta,
            f"<title>{html.escape(title)}</title><style>", CSS, "</style></head><body>",
            f"<h1>SportBayes · {html.escape(h1)}</h1>", nav_html(cfg, cur, static)]


def page_close(out, js, static=False, pre=""):
    out.append(f"<script>var STATIC={'true' if static else 'false'};{pre}{JS_COMMON}{js}</script></body></html>")
    return "".join(out)


def render(app, static=False):
    cfg = app.cfg
    refresh = 300 if static else int(cfg['refresh_sec'])
    e = html.escape
    out = page_open(f"SportBayes {app.info['name']}", app.info["name"], cfg, app.lg, static)
    upd = app.updated.strftime("%H:%M:%S") if app.updated else "–"
    now_utc = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    upd = f"<span data-now='{now_utc}'>{upd}</span>"
    how = "stránka se přepočítává zhruba každých 15 minut" if static else f"obnova každých {int(cfg['refresh_sec'])} s"
    g = app.net.globals()
    u, dec = app.info["unit"], app.info["dec"]
    out.append(f"<div class='sub'>Aktualizováno {upd} · {how} · "
               f"model: {len(app.rows)} zápasů v historii · domácí výhoda {g['home_adv']:+.{dec}f} {u}"
               + (f" · únava (zápas 2. den po sobě) {g['b2b']:+.{dec}f} {u}" if app.info["b2b"] else "") + "</div>")
    if app.error:
        out.append(f"<div class='err'>Data se nepodařilo obnovit ({e(app.error)}). Zobrazuji poslední známý stav.</div>")

    sides = app.sides
    hdr = " / ".join(LBL[k] for k in sides)
    three = len(sides) == 3
    lo, hi = app.info["window"]
    span = "včera, dnes, zítra" if (lo, hi) == (-1, 1) else f"poslední {-lo} dny a příštích {hi} dní"
    ob_id, ob_val, ob_txt = None, 0.0, ""
    for r in app.board:                                   # outsider s nejvyssim kurzem trhu (bez remizy)
        if r["state"] != "pre" or r["p"] is None or not r["odds"]:
            continue
        fav_side = "home" if r["p"]["home"] >= r["p"]["away"] else "away"
        out_side = "away" if fav_side == "home" else "home"
        try:
            ov = float(r["odds"][out_side])
        except (KeyError, TypeError, ValueError):
            continue
        if ov > ob_val:
            ob_id, ob_val = r["event_id"], ov
            ob_txt = f"{r['away_name'] or r['away'] if out_side == 'away' else r['home_name'] or r['home']} (kurz {ov:.2f})"
    out.append(f"<h2>Zápasy – {span}</h2><div class='fbar' data-ob='{html.escape(ob_txt)}'>"
               "<button class='fb on' data-f='all' title='Zruší výběr, zobrazí všechny zápasy'>Všechny</button>"
               "<button class='fb' data-f='fav2'><span class='fav2'>●</span> Zelení (60 % a víc)</button>"
               "<button class='fb' data-f='fav1'><span class='fav1'>●</span> Modří (50–60 %)</button>"
               "<button class='fb' data-f='fav0'><span class='fav0'>●</span> Žlutí (pod 50 %)</button>"
               "<button class='fb' data-f='ob'>Outsider s nejvyšším kurzem</button>"
               "<span id='fb_msg' class='mut'></span></div>"
               "<div class='wrap'><table class='games'><tr>"
               "<th class='l'>Začátek</th><th class='l'>Domácí – Hosté</th><th class='l'>Stav</th>"
               f"<th>Model: {hdr if three else 'domácí'}</th><th>Férový kurz {hdr}</th><th>Kurz trhu {hdr}</th>"
               f"<th>Trh: {hdr if three else 'domácí'}</th><th title='25 % model + 75 % kurz trhu'>Mix 25/75: {hdr if three else 'domácí'}</th><th>Výhodnost {hdr}</th><th class='l'>Tip</th>"
               f"<th>Můj kurz {hdr}</th><th class='l'>Výhodnost mého kurzu</th><th class='l'>Do deníku</th></tr>")
    if not app.board:
        out.append("<tr><td class='l mut' colspan='13'>V těchto dnech nejsou na programu žádné zápasy.</td></tr>")

    def edge(v):
        c = "good" if v >= float(cfg["min_edge"]) else ("bad" if v < 0 else "")
        return f"<span class='{c}'>{v * 100:+.1f} %</span>"

    for r in app.board:
        ps = r["p"]
        st = r["state"]
        if st == "pre":
            stav = "<span class='mut'>před zápasem</span>"
        elif st == "in":
            stav = f"<span class='live'>ŽIVĚ {e(r.get('score_txt') or str(r['home_pts']) + ':' + str(r['away_pts']))}</span> <span class='mut'>{e(r['detail'])}</span>"
        elif r["home_pts"] is None or r["away_pts"] is None or not r["completed"]:
            stav = f"<span class='mut'>{e(r['detail'] or 'neodehráno')}</span>"
        else:
            mark = ""
            if ps is not None:
                won = result_side(r["home_pts"], r["away_pts"])
                hit = max(sides, key=lambda k: ps[k]) == won
                mark = " <span class='good'>✓</span>" if hit else " <span class='bad'>✗</span>"
            stav = f"{e(r.get('score_txt') or str(r['home_pts']) + ':' + str(r['away_pts']))} <span class='mut'>{e(r['detail'])}</span>{mark}"
        b_a = "<span class='b2b'>B2B</span>" if r["b2b"][1] else ""
        b_h = "<span class='b2b'>B2B</span>" if r["b2b"][0] else ""
        fc = ""
        fside = None
        if ps is not None:
            fside = max(sides, key=lambda k: ps[k])
            fc = "fav2" if ps[fside] >= 0.6 else ("fav1" if ps[fside] >= 0.5 else "fav0")
        hn, an = e(r['home_name'] or r['home']), e(r['away_name'] or r['away'])
        if fside == "home":
            hn = f"<span class='{fc}'>{hn}</span>"
        elif fside == "away":
            an = f"<span class='{fc}'>{an}</span>"
        teams = f"<b>{hn}</b>{b_h} <span class='mut'>–</span> {an}{b_a}"
        if ps is None:
            model = fair = "<span class='mut'>–</span>"
        else:
            if three:
                model = " / ".join((f"<span class='{fc}'>{ps[k] * 100:.0f}</span>" if k == fside else f"{ps[k] * 100:.0f}") for k in sides) + " %"
            else:
                model = (f"<span class='bar'><i style='width:{ps['home'] * 100:.0f}%'></i></span>"
                         f"<span class='{fc}'>{pct(ps[fside], 1)}</span>" + ("" if fside == "home" else " <span class='mut'>hosté</span>"))
            fair = " / ".join(f"{1 / max(ps[k], 1e-6):.2f}" for k in sides)
        o = r["odds"]
        if o:
            kurz = f"<span title='{e(str(o.get('source', '')))}'>" + " / ".join(f"{float(o[k]):.2f}" for k in sides) + "</span>"
        else:
            kurz = "<span class='mut'>–</span>"
        if r["fair"]:
            trh = (" / ".join(f"{r['fair'][k] * 100:.0f}" for k in sides) + " %") if three else pct(r["fair"]["home"], 1)
        else:
            trh = "–"
        if ps is not None and r["fair"]:                      # 25 % model + 75 % trh
            mix = {k: 0.25 * ps[k] + 0.75 * r["fair"][k] for k in sides}
            mixs = (" / ".join(f"{mix[k] * 100:.0f}" for k in sides) + " %") if three else pct(mix["home"], 1)
        else:
            mixs = "–"
        edges = " / ".join(edge(r["edge"][k]) for k in sides) if r["edge"] else "–"
        tip = ""
        if r["pick"]:
            who = {"home": r["home"], "away": r["away"], "draw": "remíza"}[r["pick"]]
            if st == "pre":
                tip = f"<span class='tag'>{e(who)}</span> <span class='mut'>vklad ~{r['stake']} Kč</span>"
                if r["flag"]:
                    tip += f"<br><span class='warn'>⚠ {e(r['flag'])}</span>"
            else:
                tip = f"<span class='mut'>tip byl {e(who)}</span>"
        if ps is not None and st == "pre":
            my = " ".join(f"<input class='o' inputmode='decimal' placeholder='{LBL[k]}' oninput='upd(this)'>" for k in sides)
        else:
            my = "<span class='mut'>–</span>"
        if ps is not None and st == "pre":
            names = [r["home_name"] or r["home"]] + (["Remíza"] if three else []) + [r["away_name"] or r["away"]]
            bets = "".join(f"<button class='bet' data-i='{i}' data-k='{k}' title='Zapsat sázku: {e(names[i])}' onclick='addBet(this)'>{LBL[k]}</button>"
                           for i, k in enumerate(sides))
        else:
            names, bets = [], "<span class='mut'>–</span>"
        cls = " class='pick'" if (r["pick"] and st == "pre") else ""
        fcls = "" if ps is None else ("fav2" if ps[max(sides, key=lambda k: ps[k])] >= 0.6 else ("fav1" if ps[max(sides, key=lambda k: ps[k])] >= 0.5 else "fav0"))
        pa = f" data-fc='{fcls}'" + (" data-ob='1'" if r["event_id"] == ob_id else "")
        pa += "" if ps is None else (" data-p='" + ",".join(f"{ps[k]:.5f}" for k in sides) + "'"
                                    + " data-s='" + ",".join(LBL[k] for k in sides) + "'")
        if names:
            mo = ",".join(f"{float(r['odds'][k]):.2f}" for k in sides) if r["odds"] else ""
            pa += (f" data-lg='{app.lg}' data-d='{e(r['date_utc'])}' data-nm='{e(json.dumps(names, ensure_ascii=False))}'"
                   f" data-m='{e(names[0] + ' – ' + names[-1])}' data-mo='{mo}'"
                   f" data-pk='{r['pick'] or ''}' data-st='{r['stake'] or ''}'")
        out.append(f"<tr{cls} data-id='{e(r['event_id'])}'{pa}>"
                   f"<td class='l' data-l='Začátek'><span data-t='{e(r['date_utc'])}'>{e(r['when'])}</span></td>"
                   f"<td class='l tm'>{teams}</td><td class='l' data-l='Stav'>{stav}</td>"
                   f"<td class='num' data-l='Model: {hdr if three else 'domácí'}'>{model}</td>"
                   f"<td class='num' data-l='Férový kurz {hdr}'>{fair}</td>"
                   f"<td class='num' data-l='Kurz trhu {hdr}'>{kurz}</td>"
                   f"<td class='num' data-l='Trh: {hdr if three else 'domácí'}'>{trh}</td>"
                   f"<td class='num' data-l='Mix 25/75: {hdr if three else 'domácí'}'>{mixs}</td>"
                   f"<td class='num' data-l='Výhodnost {hdr}'>{edges}</td>"
                   f"<td class='l' data-l='Tip'>{tip}</td>"
                   f"<td data-l='Můj kurz {hdr}'>{my}</td><td class='l my' data-l='Výhodnost mého kurzu'></td>"
                   f"<td class='l' data-l='Do deníku'>{bets}</td></tr>")
    out.append("</table></div>")
    legend = "D = domácí, R = remíza, H = hosté." if three else "D = domácí, H = hosté."
    extra = ", v hokeji ani to, který brankář chytá" if app.lg in ("nhl", "extraliga") else ""
    if app.lg == "extraliga":
        legend += (" Počítá se výsledek po základní hrací době (60 minut): zápas, který šel do prodloužení"
                   " nebo nájezdů, je remíza. „pp“ = po prodloužení, „sn“ = po nájezdech.")
    if data.is_ch(app.lg):
        legend += (" Zdroj: web Českého hokeje (ceskyhokej.cz); kurzy je třeba zadat ručně.")
    if data.is_apisports(app.lg):
        legend += (f" Zdroj dat má denní limit (dnes využito {data.as_used(app.info['host'])} ze 100 dotazů),"
                   " proto se zápasy obnovují po 15–30 minutách a kurzy je třeba zadat ručně.")
    legend += (" <b>Mix 25/75</b> = 25 % model + 75 % kurz trhu. V NBA na datech od roku 2012 trefuje stejně jako samotný trh,"
               " takže slouží jako kontrola, ne jako doporučení k sázení.")
    legend += (" <b>Barva favorita:</b> <span class='fav2'>zelená</span> = model dává vítězi 60 % a víc, "
               "<span class='fav1'>modrá</span> = 50–60 %, <span class='fav0'>oranžová</span> = pod 50 % (vyrovnaný zápas).")
    out.append(f"<div class='note'>{legend} <b>Výhodnost</b> = očekávaný zisk na 1 Kč vkladu podle modelu "
               f"(pravděpodobnost × kurz − 1). Tip se zobrazí od {pct(float(cfg['min_edge']))}. "
               "Do sloupce „Můj kurz“ napiš kurz své sázkovky a výhodnost se přepočítá. "
               f"Model nezná zranění ani sestavy{extra} – když se s trhem výrazně rozchází, má většinou pravdu trh.</div>")

    rec = getattr(app, "record", None) or {}
    out.append("<h2>Bilance modelu (zápasy předpovězené před začátkem)</h2><div class='cards'>")
    out.append(f"<div class='card'><b>{rec.get('n', 0)}</b><span>vyhodnocených zápasů</span></div>")
    out.append(f"<div class='card'><b>{pct(rec.get('acc'), 1)}</b><span>trefený vítěz</span></div>")
    ll = rec.get("logloss")
    out.append(f"<div class='card'><b>{'–' if ll is None else f'{ll:.3f}'}</b><span>log-loss (náhodný tip = {'1,099' if len(app.sides) == 3 else '0,693'})</span></div>")
    out.append(f"<div class='card'><b>{rec.get('wins', 0)} / {rec.get('bets', 0)}</b><span>vyhrané tipy / tipy</span></div>")
    pnl = rec.get("pnl", 0.0)
    out.append(f"<div class='card'><b class='{'good' if pnl > 0 else ('bad' if pnl < 0 else '')}'>{pnl:+.2f}</b>"
               "<span>zisk tipů v jednotkách vkladu</span></div></div>")

    ct = confidence_table(app.hist, app.rows, app.sides)
    if ct:
        out.append("<h2>Úspěšnost podle jistoty modelu</h2><div class='wrap'><table><tr>"
                   "<th class='l'>Nejvyšší pravděpodobnost modelu</th><th>Zápasů</th><th>Skutečná úspěšnost</th>"
                   "<th>Model sliboval</th></tr>")
        for r in ct:
            out.append(f"<tr><td class='l'>{r['name']}</td><td>{r['n']}</td><td><b>{pct(r['acc'], 1)}</b></td>"
                       f"<td>{pct(r['avg'], 1)}</td></tr>")
        out.append("</table></div><div class='note'>Počítá se ze všech zápasů, které model předpověděl jen z toho, co věděl před nimi "
                   "(bez první sezony v historii). Když sedí „skutečná“ s „model sliboval“, je model poctivě kalibrovaný. "
                   "Vysoká trefnost ještě neznamená zisk – závisí na kurzu. Řádek „60 % a víc“ je souhrn obou řádků nad ním.</div>")

    out.append(f"<h2>Síla týmů ({'góly' if u == 'g.' else 'body'} proti průměrnému týmu na neutrální půdě)</h2><div class='wrap'><table><tr>"
               "<th>#</th><th class='l'>Tým</th><th>Síla</th><th>± nejistota</th><th>Zápasů</th></tr>")
    for i, (t, s, sd, n) in enumerate(app.net.ratings(), 1):
        c = "good" if s > 0 else "bad"
        out.append(f"<tr><td class='mut'>{i}</td><td class='l'>{e(t)}</td><td class='num {c}'>{s:+.{dec}f}</td>"
                   f"<td class='num mut'>± {sd:.{dec}f}</td><td class='num mut'>{n}</td></tr>")
    out.append("</table></div>")
    ts = team_stats(app, float(cfg["min_edge"]))
    if ts:
        def ci(k, n):
            return 1.96 * math.sqrt(max(k / n * (1 - k / n), 0) / n) * 100 if n else 0
        out.append("<h2>Statistika týmů – úspěšnost modelu a sázek</h2>"
                   "<div class='sub'>Kolik zápasů tohoto týmu model předem trefil (favorit vyhrál), bez znalosti výsledku. "
                   "Řadit lze kliknutím na záhlaví. Minimum zápasů: "
                   "<input class='t n' id='ts_min' value='20' inputmode='numeric'></div>"
                   "<div class='wrap'><table id='ts'><tr><th>#</th><th class='l'>Tým</th><th data-k='n'>Zápasů</th>"
                   "<th data-k='hit'>Trefa modelu</th><th data-k='home'>doma</th><th data-k='away'>venku</th>"
                   "<th data-k='ll'>Log-loss</th><th data-k='bets'>Tipy modelu</th><th data-k='pnl'>Zisk tipů (jedn.)</th>"
                   "<th data-k='mb'>Moje sázky</th><th data-k='mp'>Můj zisk Kč</th></tr>")
        pc = lambda v: "–" if v is None else f"{v * 100:.0f} %"
        col = lambda v: "" if not v else ("good" if v > 0 else "bad")
        num = lambda v: "" if v is None else str(v)
        for t, x in ts.items():
            n = x["n"]
            hit = x["hit"] / n if n else None
            hh = x["hhit"] / x["hn"] if x["hn"] else None
            ha = x["ahit"] / x["an"] if x["an"] else None
            ll = x["ll"] / n if n else None
            pnl = x["pnl"] if x["bets"] else None
            mp = x["mp"] if x["mb"] else None
            attrs = (" data-t='" + e(t) + "' data-n='" + str(n) + "' data-hit='" + num(hit) + "' data-home='" + num(hh)
                     + "' data-away='" + num(ha) + "' data-ll='" + num(None if ll is None else -ll)
                     + "' data-bets='" + str(x["bets"]) + "' data-pnl='" + num(pnl) + "' data-mb='" + str(x["mb"])
                     + "' data-mp='" + num(mp) + "'")
            ci_txt = (" <span class='mut'>±" + format(ci(x["hit"], n), ".0f") + "</span>") if n else ""
            ll_txt = "–" if ll is None else format(ll, ".3f")
            tips = (str(x["wins"]) + " / " + str(x["bets"])) if x["bets"] else "–"
            mine = (str(x["mw"]) + " / " + str(x["mb"])) if x["mb"] else "–"
            pnl_txt = "–" if pnl is None else format(pnl, "+.2f")
            mp_txt = "–" if mp is None else format(mp, "+.0f")
            out.append("<tr" + attrs + "><td class='mut'></td><td class='l'>" + e(t) + "</td><td class='num mut'>" + str(n)
                       + "</td><td class='num'><b>" + pc(hit) + "</b>" + ci_txt + "</td><td class='num mut'>" + pc(hh)
                       + "</td><td class='num mut'>" + pc(ha) + "</td><td class='num mut'>" + ll_txt + "</td><td class='num'>"
                       + tips + "</td><td class='num " + col(pnl) + "'>" + pnl_txt + "</td><td class='num'>" + mine
                       + "</td><td class='num " + col(mp) + "'>" + mp_txt + "</td></tr>")
        out.append("</table></div><div class='note'>Rozdíly mezi týmy jsou z velké části náhoda: při 40 zápasech je "
                   "nejistota trefy kolem ±15 procentních bodů (číslo za ±). Vysoká trefa v minulosti neznamená, že "
                   "model bude tým trefovat i příště. <b>Tipy modelu</b> se počítají jen ze zápasů, u kterých aplikace "
                   "zalogovala kurzy před začátkem (proto jich je zatím málo); <b>Moje sázky</b> jsou vyhodnocené sázky "
                   "z deníku na daný tým.</div>")
        js_teams = JS_TEAMS
    else:
        js_teams = ""
    return page_close(out, JS + JS_BET + js_teams + JS_FILTER, static, pre=f"var MIN={float(cfg['min_edge'])},REFRESH={refresh};")


JS_DIARY = r"""
var ST={open:'Otevřená',won:'Výhra',lost:'Prohra',void:'Vrácená'},LIST=[];
var TB=document.getElementById('tb'),SM=document.getElementById('sum');
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function money(n){return n.toLocaleString('cs-CZ',{minimumFractionDigits:0,maximumFractionDigits:2})}
function profit(e){return e.status==='won'?e.stake*(e.odds-1):(e.status==='lost'?-e.stake:0)}
function sorted(){return LIST.slice().sort(function(a,b){
  return (b.date||'').localeCompare(a.date||'')||(b.created||'').localeCompare(a.created||'')})}
function render(){
  var L=sorted(),won=0,lost=0,stk=0,pf=0,op=0,ops=0;
  L.forEach(function(e){
    if(e.status==='open'){op++;ops+=e.stake}
    else if(e.status==='won'||e.status==='lost'){if(e.status==='won')won++;else lost++;stk+=e.stake;pf+=profit(e)}});
  var c=function(b,t,cls){return '<div class="card"><b class="'+(cls||'')+'">'+b+'</b><span>'+t+'</span></div>'};
  SM.innerHTML=c(L.length,'sázek celkem')+c(op,'otevřených ('+money(ops)+' Kč v sázce)')
    +c(money(stk)+' Kč','vsazeno ve vyhodnocených')
    +c((pf>0?'+':'')+money(pf)+' Kč','zisk / ztráta',pf>0?'good':(pf<0?'bad':''))
    +c(stk?((pf/stk*100>0?'+':'')+(pf/stk*100).toFixed(1)+' %'):'–','návratnost (zisk / vsazeno)',pf>0?'good':(pf<0?'bad':''))
    +c(won+lost?Math.round(won/(won+lost)*100)+' % ('+won+' z '+(won+lost)+')':'–','úspěšnost sázek');
  var h='<tr><th class="l">Datum</th><th class="l">Liga</th><th class="l">Zápas</th><th class="l">Tip</th><th>Kurz</th>'
    +'<th>Vklad</th><th class="l">Stav</th><th>Zisk</th><th>Model</th><th class="l">Poznámka</th><th></th></tr>';
  if(!L.length)h+='<tr><td class="l mut" colspan="11">Zatím žádná sázka. Zapiš ji tlačítkem D / R / H u zápasu, nebo formulářem výše.</td></tr>';
  L.forEach(function(e){
    var opts=Object.keys(ST).map(function(k){return '<option value="'+k+'"'+(e.status===k?' selected':'')+'>'+ST[k]+'</option>'}).join('');
    var pr=profit(e),pc=e.status==='open'?'':(pr>0?'good':(pr<0?'bad':''));
    h+='<tr data-id="'+esc(e.id)+'"><td class="l">'+esc(e.date)+'</td><td class="l">'+esc(LG[e.lg]||'jiná')+'</td>'
      +'<td class="l">'+esc(e.match)+'</td><td class="l"><b>'+esc(e.tip)+'</b></td>'
      +'<td><input class="t n" data-f="odds" value="'+esc(String(e.odds).replace('.',','))+'"></td>'
      +'<td><input class="t n" data-f="stake" value="'+esc(String(e.stake).replace('.',','))+'"></td>'
      +'<td class="l"><select data-f="status">'+opts+'</select>'+(e.auto?' <span class="mut" title="vyhodnoceno automaticky">auto</span>':'')+'</td>'
      +'<td class="num '+pc+'">'+(e.status==='open'?'–':(pr>0?'+':'')+money(pr))+'</td>'
      +'<td class="num mut">'+(e.p?Math.round(e.p*100)+' %':'–')+'</td>'
      +'<td class="l"><input class="t" data-f="note" value="'+esc(e.note)+'"></td>'
      +'<td><button class="x" data-del="1" title="Smazat sázku">✕</button></td></tr>';
  });
  TB.innerHTML=h;
}
TB.addEventListener('change',function(ev){
  var t=ev.target,tr=t.closest('tr'),f=t.dataset.f,v=t.value;
  if(!tr||!f)return;
  if(f==='odds'||f==='stake'){v=parseFloat(v.replace(',','.'));
    if(!(v>(f==='odds'?1:0))){toast('Neplatná hodnota',1);render();return}}
  var fields={};fields[f]=v;
  if(f==='status')fields.auto=false;
  DB.send({op:'set',id:tr.dataset.id,fields:fields},function(l){LIST=l;render()});
});
TB.addEventListener('click',function(ev){
  var t=ev.target;
  if(t.dataset.del&&confirm('Smazat tuto sázku z deníku?'))
    DB.send({op:'del',id:t.closest('tr').dataset.id},function(l){LIST=l;render()});
});
function addManual(){
  var g=function(i){return document.getElementById(i).value.trim()};
  var odds=parseFloat(g('f_odds').replace(',','.')),stake=parseFloat(g('f_stake').replace(',','.'));
  if(!g('f_match')&&!g('f_tip')){toast('Doplň zápas nebo tip',1);return}
  if(!(odds>1)){toast('Neplatný kurz',1);return}
  if(!(stake>0)){toast('Neplatný vklad',1);return}
  DB.send({op:'add',entry:{id:Date.now().toString(36)+Math.random().toString(36).slice(2,6),
    created:new Date().toISOString(),date:g('f_date')||TODAY,lg:g('f_lg'),event_id:'',match:g('f_match'),
    pick:'',tip:g('f_tip'),odds:odds,stake:stake,p:null,status:'open',note:g('f_note')}},
    function(l){LIST=l;render();['f_match','f_tip','f_odds','f_stake','f_note'].forEach(function(i){document.getElementById(i).value=''});
      toast('Sázka přidána')});
}
function exportCsv(){
  var n=function(x){return String(x==null?'':x).replace('.',',')};
  var rows=[['Datum','Liga','Zápas','Tip','Kurz','Vklad','Stav','Zisk','Model %','Poznámka']];
  sorted().forEach(function(e){rows.push([e.date,LG[e.lg]||'jiná',e.match,e.tip,n(e.odds),n(e.stake),ST[e.status],
    n(Math.round(profit(e)*100)/100),e.p?Math.round(e.p*100):'',e.note])});
  var txt='﻿'+rows.map(function(r){return r.map(function(c){return '"'+String(c==null?'':c).replace(/"/g,'""')+'"'}).join(';')}).join('\r\n');
  var a=document.createElement('a');a.href=URL.createObjectURL(new Blob([txt],{type:'text/csv;charset=utf-8'}));
  a.download='denik_sazek.csv';document.body.appendChild(a);a.click();a.remove();
}

var CUR=null,$=function(i){return document.getElementById(i)};
function num(v){return parseFloat(String(v).replace(',','.'))}
function rows(){return [].slice.call(document.querySelectorAll('#sp tr[data-i]'))}
function initGames(){
  var seen={},h='<option value="">– vyber ligu –</option>';
  GAMES.forEach(function(g){if(!seen[g.lg]){seen[g.lg]=1;h+='<option value="'+esc(g.lg)+'">'+esc(LG[g.lg]||g.lg)+'</option>'}});
  $('f_liga').innerHTML=h+'<option value="manual">jiná sázka (zadat ručně)…</option>';
  $('f_liga').onchange=pickLeague;$('f_game').onchange=pickGame;
}
function pickLeague(){
  var v=$('f_liga').value,gs=$('f_game'),h='<option value="">– vyber zápas –</option>';
  CUR=null;$('blk_split').style.display='none';
  $('blk_manual').style.display=v==='manual'?'':'none';
  if(v===''||v==='manual'){gs.innerHTML='<option value="">–</option>';gs.disabled=true;return}
  GAMES.forEach(function(g,i){if(g.lg===v)h+='<option value="'+i+'">'+esc(g.w+' · '+g.nm[0]+' – '+g.nm[g.nm.length-1])+'</option>'});
  gs.innerHTML=h;gs.disabled=false;
}
function pickGame(){
  var v=$('f_game').value;
  $('blk_split').style.display=v!==''?'':'none';
  if(v===''){CUR=null;return}
  CUR=GAMES[+v];buildSplit();
}
function buildSplit(){
  var g=CUR,h='<tr><th class="l"></th><th class="l">Tip</th><th>Model</th><th>Kurz</th><th>Podíl z vkladu %</th>'
    +'<th>Vklad Kč</th><th>Když vyjde, zisk celkem</th></tr>';
  g.nm.forEach(function(n,i){
    h+='<tr data-i="'+i+'"><td class="l"><input type="checkbox" data-c="1" checked></td><td class="l"><b>'+esc(n)+'</b></td>'
      +'<td class="num mut">'+Math.round(g.p[i]*100)+' %</td>'
      +'<td><input class="t n" data-f="o" value="'+(g.o?String(g.o[i]).replace('.',','):'')+'"></td>'
      +'<td><input class="t n" data-f="pc"></td><td class="num" data-r="st">–</td><td class="num" data-r="pf">–</td></tr>'});
  $('sp').innerHTML=h;splitModel();
}
function splitModel(){
  var R=rows(),s=0;
  R.forEach(function(tr){if(tr.querySelector('[data-c]').checked)s+=CUR.p[+tr.dataset.i]});
  R.forEach(function(tr){var on=tr.querySelector('[data-c]').checked;
    tr.querySelector('[data-f=pc]').value=on&&s?(CUR.p[+tr.dataset.i]/s*100).toFixed(1).replace('.',','):''});
  calc();
}
function splitEqual(){
  var R=rows(),n=R.filter(function(tr){return tr.querySelector('[data-c]').checked}).length;
  R.forEach(function(tr){tr.querySelector('[data-f=pc]').value=tr.querySelector('[data-c]').checked&&n?(100/n).toFixed(1).replace('.',','):''});
  calc();
}
function calc(){
  var T=num($('f_total').value)||0,R=rows(),sp=0,tot=0;
  R.forEach(function(tr){var on=tr.querySelector('[data-c]').checked,pc=num(tr.querySelector('[data-f=pc]').value)||0,
    st=on?Math.round(T*pc)/100:0;tr.dataset.st=st;if(on)sp+=pc;tot+=st;
    tr.querySelector('[data-r=st]').textContent=on?money(st):'–'});
  R.forEach(function(tr){var o=num(tr.querySelector('[data-f=o]').value),st=+tr.dataset.st,c=tr.querySelector('[data-r=pf]');
    if(st>0&&o>1){var pf=st*o-tot;c.textContent=(pf>0?'+':'')+money(pf)+' Kč';c.className='num '+(pf>0?'good':'bad')}
    else{c.textContent='–';c.className='num'}});
  var ok=Math.abs(sp-100)<0.06;
  $('sp_sum').innerHTML='Součet podílů: <b class="'+(ok?'good':'warn')+'">'+sp.toFixed(1).replace('.',',')+' %</b> · rozděleno <b>'
    +money(tot)+' Kč</b> z '+money(T)+' Kč'+(ok?'':' – podíly nedávají 100 %');
}
$('sp').addEventListener('input',function(ev){if(!ev.target.dataset.c)calc()});
$('sp').addEventListener('change',function(ev){if(ev.target.dataset.c)splitModel()});
$('f_total').addEventListener('input',calc);
function saveSplit(){
  if(!CUR)return;
  var q=[],bad='',ts=Date.now();
  rows().forEach(function(tr){
    var i=+tr.dataset.i,st=+tr.dataset.st,o=num(tr.querySelector('[data-f=o]').value);
    if(!tr.querySelector('[data-c]').checked||!(st>0))return;
    if(!(o>1)){bad=bad||CUR.nm[i];return}
    var d=new Date(CUR.d);
    q.push({id:(ts++).toString(36)+Math.random().toString(36).slice(2,6),created:new Date().toISOString(),
      date:d.getFullYear()+'-'+p2(d.getMonth()+1)+'-'+p2(d.getDate()),lg:CUR.lg,event_id:CUR.id,
      match:CUR.nm[0]+' – '+CUR.nm[CUR.nm.length-1],pick:CUR.k[i],tip:CUR.nm[i],odds:o,stake:st,p:CUR.p[i],status:'open',note:''})});
  if(bad){toast('Doplň kurz u: '+bad,1);return}
  if(!q.length){toast('Zadej celkový vklad a označ aspoň jednu sázku',1);return}
  var n=q.length;
  (function next(){var e=q.shift();
    if(!e){toast('Zapsáno sázek: '+n);return}
    DB.send({op:'add',entry:e},function(l){LIST=l;render();next()})})();
}
initGames();
var d0=new Date(),TODAY=d0.getFullYear()+'-'+p2(d0.getMonth()+1)+'-'+p2(d0.getDate());
document.getElementById('f_date').value=TODAY;
document.getElementById('f_lg').innerHTML=Object.keys(LG).map(function(k){return '<option value="'+k+'">'+esc(LG[k])+'</option>'}).join('')+'<option value="">jiná</option>';
function load(){DB.load(function(l){LIST=l;render()})}
load();
setInterval(function(){var a=document.activeElement;if(!(a&&/INPUT|SELECT/.test(a.tagName)))load()},120000);
"""

JS_SET = r"""
var order=SET.order.slice(),hidden=SET.hidden.slice(),TB=document.getElementById('tb');
if(STATIC){try{var s=JSON.parse(localStorage.getItem('sb_set')||'null');
  if(s){var o=(s.order||[]).filter(function(l){return SET.order.indexOf(l)>=0});
    SET.order.forEach(function(l){if(o.indexOf(l)<0)o.push(l)});order=o;hidden=(s.hidden||[]).filter(function(l){return SET.order.indexOf(l)>=0})}}catch(e){}}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function draw(){
  var h='<tr><th class="l">Zobrazit</th><th class="l">Liga</th><th class="l">Stav</th><th>Pořadí</th></tr>';
  order.forEach(function(l,i){
    h+='<tr data-l="'+l+'"><td class="l"><input type="checkbox" data-c="1"'+(hidden.indexOf(l)<0?' checked':'')+'></td>'
      +'<td class="l"><b>'+esc(SET.names[l])+'</b></td><td class="l mut">'+esc(SET.status[l]||'')+'</td>'
      +'<td><button data-m="-1"'+(i?'':' disabled')+'>↑</button> <button data-m="1"'+(i<order.length-1?'':' disabled')+'>↓</button></td></tr>';
  });
  TB.innerHTML=h;
}
TB.addEventListener('click',function(ev){
  var t=ev.target,tr=t.closest('tr');
  if(!t.dataset.m||!tr)return;
  var i=order.indexOf(tr.dataset.l),j=i+(+t.dataset.m);
  if(j<0||j>=order.length)return;
  var x=order[i];order[i]=order[j];order[j]=x;draw();
});
TB.addEventListener('change',function(ev){
  var t=ev.target,tr=t.closest('tr');
  if(!t.dataset.c||!tr)return;
  var l=tr.dataset.l,k=hidden.indexOf(l);
  if(t.checked&&k>=0)hidden.splice(k,1);
  if(!t.checked&&k<0)hidden.push(l);
});
function save(){
  if(order.every(function(l){return hidden.indexOf(l)>=0})){toast('Aspoň jedna liga musí zůstat zapnutá',1);return}
  if(STATIC){try{localStorage.setItem('sb_set',JSON.stringify({order:order,hidden:hidden}))}catch(e){}
    toast('Uloženo');setTimeout(function(){location.reload()},700);return}
  fetch('api/nastaveni',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({order:order,hidden:hidden})})
   .then(function(r){if(!r.ok)throw 0;return r.json()})
   .then(function(r){toast(r.loading&&r.loading.length?'Uloženo, nově zapnuté ligy se načítají na pozadí':'Uloženo');
     setTimeout(function(){location.reload()},1200)})
   .catch(function(){toast('Uložení se nepodařilo - běží aplikace?',1)});
}
draw();
"""


def _league_names(cfg):
    return {lg: f"{data.LEAGUES[lg]['name']} · {data.LEAGUES[lg]['sport']}"
            for lg in (cfg.get("leagues") or []) if lg in data.LEAGUES}


def upcoming_games(apps, cfg, static=False):
    """Zapasy pred zacatkem se zadanymi pravdepodobnostmi - nabidka v Denicku."""
    st = load_settings(cfg, not static)
    out = []
    for lg in st["order"]:
        app = apps.get(lg)
        if app is None or (lg in st["hidden"] and not static):
            continue
        for r in sorted(app.board, key=lambda r: r["date_utc"]):
            if r["state"] != "pre" or r["p"] is None:
                continue
            sides = app.sides
            out.append({"lg": lg, "id": r["event_id"], "d": r["date_utc"], "w": r["when"],
                        "nm": [r["home_name"] or r["home"]] + (["Remíza"] if len(sides) == 3 else []) + [r["away_name"] or r["away"]],
                        "k": list(sides), "p": [round(r["p"][k], 5) for k in sides],
                        "o": [float(r["odds"][k]) for k in sides] if r["odds"] else None})
    return out


def render_favorites(cfg, static=False, games=None):
    """Favorite vsech lig v jednom rozpisu podle casu + jednoduchy kombinator (soucin pravdepodobnosti)."""
    e = html.escape
    out = page_open("SportBayes – favoriti", "Favoriti", cfg, "favoriti", static)
    out.append("<div class='sub'>Všechny nadcházející zápasy ze všech lig, kde je podle modelu jasný favorit. "
               "Seřazeno podle začátku. Zaškrtni zápasy a dole se spočítá kombinace.</div>")
    out.append("<div class='form' style='margin:10px 0'><label>Zobrazit favority od jistoty: "
               "<select id='fv_min'><option value='0.5'>50 %</option><option value='0.55'>55 %</option>"
               "<option value='0.6'>60 %</option><option value='0.65'>65 %</option><option value='0.7'>70 %</option>"
               "<option value='0.75'>75 %</option></select></label> <label style='margin-left:14px'><input type='checkbox' id='fv_near' checked> jen nejbližší zápas každého týmu</label> <span id='fv_cnt' class='mut'></span></div>")
    out.append("<div class='wrap'><table id='fv'><tr><th></th><th class='l'>Začátek</th><th class='l'>Liga</th>"
               "<th class='l'>Zápas</th><th class='l'>Favorit</th><th>Jistota modelu</th><th>Férový kurz</th>"
               "<th>Kurz trhu</th><th>Můj kurz</th></tr>")
    rows = 0
    seen = set()                                  # (liga, tym) -> uz mel nejblizsi zapas
    for gm in sorted(games or [], key=lambda x: x["d"]):
        i = max(range(len(gm["p"])), key=lambda j: gm["p"][j])
        pf = gm["p"][i]
        near = 1
        fav_teams = [gm["nm"][i]] if gm["nm"][i] != "Remíza" else [gm["nm"][0], gm["nm"][-1]]
        for t in fav_teams:                       # je to nejbližší zápas favorita?
            if (gm["lg"], t) in seen:
                near = 0
        for t in (gm["nm"][0], gm["nm"][-1]):
            seen.add((gm["lg"], t))
        if pf < 0.5:
            continue
        rows += 1
        fc = "fav2" if pf >= 0.6 else "fav1"
        nm = gm["nm"]
        market = f"{gm['o'][i]:.2f}" if gm["o"] else "<span class='mut'>–</span>"
        mo = f"{gm['o'][i]:.4f}" if gm["o"] else ""
        title = f"{nm[0]} – {nm[-1]}"
        out.append(f"<tr class='fvr' data-n='{near}' data-p='{pf:.5f}' data-o='{mo}' data-m='{e(title)}' data-f='{e(nm[i])}'>"
                   f"<td><input type='checkbox' class='fvc'></td>"
                   f"<td class='l'><span data-t='{e(gm['d'])}'>{e(gm['w'])}</span></td>"
                   f"<td class='l'>{e(data.LEAGUES[gm['lg']]['name'])}</td>"
                   f"<td class='l'>{e(title)}</td><td class='l'><span class='{fc}'>{e(nm[i])}</span></td>"
                   f"<td class='num'><span class='{fc}'>{pf * 100:.0f} %</span></td>"
                   f"<td class='num'>{1 / pf:.2f}</td><td class='num'>{market}</td>"
                   f"<td><input class='o fvo' inputmode='decimal' placeholder='kurz' style='width:64px'></td></tr>")
    if not rows:
        out.append("<tr><td class='l mut' colspan='9'>Žádní favorité na programu.</td></tr>")
    out.append("</table></div>")
    out.append("<h2>Kombinace</h2><div class='cards'>"
               "<div class='card'><b id='kc_n'>0</b><span>vybraných zápasů</span></div>"
               "<div class='card'><b id='kc_p'>–</b><span>pravděpodobnost, že vyjdou všechny</span></div>"
               "<div class='card'><b id='kc_f'>–</b><span>férový kurz kombinace</span></div>"
               "<div class='card'><b id='kc_m'>–</b><span>kurz kombinace podle trhu (jen když mají kurz všechny)</span></div></div>")
    out.append("<h2>Stejný vklad na každý zápas zvlášť</h2><div class='form' style='margin:6px 0'>"
               "<label>Vyjde aspoň <input id='kk' type='number' min='1' max='100' value='60' style='width:56px'> % vybraných favoritů <span id='kk_n' class='mut'></span></label></div>"
               "<div class='cards'>"
               "<div class='card'><b id='ks_p'>–</b><span>pravděpodobnost, že vyjde aspoň tolik zápasů</span></div>"
               "<div class='card'><b id='ks_need'>–</b><span>průměrný kurz, který by na zisk byl potřeba</span></div>"
               "<div class='card'><b id='ks_have'>–</b><span>průměrný kurz vybraných zápasů (trh)</span></div>"
               "<div class='card'><b id='ks_ev'>–</b><span>očekávaný zisk podle modelu (v násobcích vkladu na zápas)</span></div></div>"
               "<div class='note' id='ks_msg'></div>"
               "<div class='wrap'><table id='ks_tb'></table></div>")
    out.append("<h2>Rozpis kombinací</h2><div class='note' style='margin:0 0 8px'>Z vybraných favoritů sestaví nejmenší sadu tiketů tak, "
               "aby při požadovaném procentu výher (políčko výše) vyšel vždy aspoň jeden tiket celý. Kurzy bere z políčka „Můj kurz“, jinak z kurzu trhu. "
               "Pro rozumnou rychlost vyber nejvýše 12 favoritů.</div>"
               "<div class='form' style='margin:6px 0'><label>Tiket obsahuje <select id='rp_m'><option>2</option><option selected>3</option><option>4</option></select> týmy</label> "
               "<label style='margin-left:12px'>Vklad na tiket <input id='rp_st' type='number' min='1' value='10' style='width:64px'> Kč</label> "
               "<button id='rp_go' class='bet' style='margin-left:12px;width:auto;padding:4px 14px'>Sestavit rozpis</button></div>"
               "<div id='rp_out'></div>")
    out.append("<div class='note'>Pravděpodobnost kombinace je součin jednotlivých pravděpodobností, takže rychle klesá: "
               "pět favoritů po 70 % vyjde jen v 17 % případů. Model nezná zranění ani sestavy, "
               "a vysoká jistota ještě neznamená, že se sázka vyplatí – záleží na kurzu.</div>")
    js = """
(function(){
var sel=document.getElementById('fv_min'),near=document.getElementById('fv_near');
try{var sv=localStorage.getItem('sb_fvmin');if(sv)sel.value=sv;var sn=localStorage.getItem('sb_fvnear');if(sn!==null)near.checked=sn==='1'}catch(e){}
near.onchange=function(){try{localStorage.setItem('sb_fvnear',near.checked?'1':'0')}catch(e){}calc()};
function calc(){
  var min=parseFloat(sel.value),shown=0,n=0,p=1,m=1,allm=true;
  document.querySelectorAll('tr.fvr').forEach(function(tr){
    var ok=parseFloat(tr.dataset.p)>=min&&(!near.checked||tr.dataset.n==='1');tr.style.display=ok?'':'none';
    var cb=tr.querySelector('.fvc');if(!ok){cb.checked=false}else{shown++}
    if(cb.checked){n++;p*=parseFloat(tr.dataset.p);if(tr.dataset.o){m*=parseFloat(tr.dataset.o)}else{allm=false}}
  });
  document.getElementById('fv_cnt').textContent='zobrazeno '+shown+' zápasů';
  document.getElementById('kc_n').textContent=n;
  document.getElementById('kc_p').textContent=n?(p*100).toFixed(1)+' %':'–';
  document.getElementById('kc_f').textContent=n?(1/p).toFixed(2):'–';
  document.getElementById('kc_m').textContent=(n&&allm)?m.toFixed(2):'–';
  singles();
}
function singles(){
  var ps=[],os=[],allo=true;
  document.querySelectorAll('tr.fvr').forEach(function(tr){
    if(tr.style.display==='none'||!tr.querySelector('.fvc').checked)return;
    ps.push(parseFloat(tr.dataset.p));if(tr.dataset.o)os.push(parseFloat(tr.dataset.o));else allo=false});
  var n=ps.length,$=function(i){return document.getElementById(i)};
  var pctv=Math.min(100,Math.max(1,parseFloat($('kk').value)||60)),k=Math.max(1,Math.ceil(pctv/100*n-1e-9));
  $('kk_n').textContent=n?'= aspoň '+k+' z '+n+' zápasů':'';
  var tb=$('ks_tb');tb.innerHTML='';
  if(!n){$('ks_p').textContent=$('ks_need').textContent=$('ks_have').textContent=$('ks_ev').textContent='–';$('ks_msg').textContent='';return}
  var d=[1];for(var i=0;i<n;i++){var nd=new Array(d.length+1).fill(0);
    for(var j=0;j<d.length;j++){nd[j]+=d[j]*(1-ps[i]);nd[j+1]+=d[j]*ps[i]}d=nd}
  var pk=0;for(var j=k;j<=n;j++)pk+=d[j];
  var avg=allo?os.reduce(function(x,y){return x+y},0)/n:null,cum=1,h='<tr><th>Vyjde zápasů</th><th>Pravděpodobnost přesně</th><th>Aspoň tolik</th><th>Zisk při průměrném kurzu (násobky vkladu)</th></tr>';
  for(var j=0;j<=n;j++){var pr=d[j];
    var pf='–';
    if(avg!==null){var v=j*avg-n,c=v>0?'good':(v<0?'bad':'');pf="<span class='"+c+"'>"+(v>=0?'+':'')+v.toFixed(2)+"</span>"}
    h+="<tr"+(j===k?" class='pick'":"")+"><td>"+j+" z "+n+" ("+Math.round(100*j/n)+" %)</td><td>"+(pr*100).toFixed(1)+" %</td><td>"+(cum*100).toFixed(1)+" %</td><td>"+pf+"</td></tr>";cum-=pr}
  tb.innerHTML=h;
  $('ks_p').textContent=k>n?'0 %':(pk*100).toFixed(0)+' %';
  $('ks_need').textContent=k>n?'–':(n/k).toFixed(2);
  if(allo){var a=os.reduce(function(x,y){return x+y},0)/n;$('ks_have').textContent=a.toFixed(2);
    var ev=0;for(var i=0;i<n;i++)ev+=ps[i]*os[i]-1;$('ks_ev').textContent=(ev>=0?'+':'')+ev.toFixed(2);
    $('ks_msg').textContent=k>n?'':(a*k>n?'Když vyjde '+k+' z '+n+' favoritů, jsi v zisku (průměrný kurz '+a.toFixed(2)+' stačí).'
      :'Když vyjde '+k+' z '+n+' favoritů, jsi ve ztrátě: potřebuješ průměrný kurz aspoň '+(n/k).toFixed(2)+', vybrané mají '+a.toFixed(2)+'.')}
  else{$('ks_have').textContent='–';$('ks_ev').textContent='–';$('ks_msg').textContent='Některé vybrané zápasy nemají kurz trhu, zisk nejde spočítat. Potřebný průměrný kurz je '+(k>n?'–':(n/k).toFixed(2))+'.'}
}
document.getElementById('kk').oninput=singles;
function pc(x){var c=0;while(x){c+=x&1;x>>=1}return c}
function rozpis(){
  var out=document.getElementById('rp_out'),rows=[];
  document.querySelectorAll('tr.fvr').forEach(function(tr){
    if(tr.style.display==='none'||!tr.querySelector('.fvc').checked)return;
    var mine=parseFloat((tr.querySelector('.fvo').value||'').replace(',','.')),o=mine>1?mine:(tr.dataset.o?parseFloat(tr.dataset.o):null);
    rows.push({p:parseFloat(tr.dataset.p),o:o,f:tr.dataset.f,m:tr.dataset.m})});
  var n=rows.length,m=parseInt(document.getElementById('rp_m').value),st=parseFloat(document.getElementById('rp_st').value)||10;
  var pctv=Math.min(100,Math.max(1,parseFloat(document.getElementById('kk').value)||60)),k=Math.max(1,Math.ceil(pctv/100*n-1e-9));
  if(n<m){out.innerHTML="<div class='note'>Vyber aspoň "+m+" favority.</div>";return}
  if(n>12){out.innerHTML="<div class='note'>Vybráno "+n+" favoritů, maximum je 12.</div>";return}
  if(m>k){out.innerHTML="<div class='note'>Při "+k+" výhrách (z "+n+") nemůže vyjít tiket po "+m+" týmech. Sniž velikost tiketu nebo zvyš procento.</div>";return}
  for(var i=0;i<n;i++)if(rows[i].o===null){out.innerHTML="<div class='note'>Favorit "+rows[i].f+" nemá kurz. Zapiš ho do sloupce „Můj kurz“.</div>";return}
  var N=1<<n,cand=[],targ=[];
  for(var x=0;x<N;x++){var c=pc(x);if(c===m)cand.push(x);if(c===k)targ.push(x)}
  var best=null;
  for(var run=0;run<24;run++){
    var cs=cand.slice();if(run>0)cs.sort(function(){return Math.random()-0.5});
    var unc=targ.slice(),pick=[];
    while(unc.length){var bi=-1,bc=-1;
      for(var a=0;a<cs.length;a++){var t=cs[a],cnt=0;for(var b=0;b<unc.length;b++)if((t&unc[b])===t)cnt++;if(cnt>bc){bc=cnt;bi=a}}
      pick.push(cs[bi]);var t2=cs[bi];unc=unc.filter(function(u){return (t2&u)!==t2})}
    for(var a=pick.length-1;a>=0;a--){var rest=pick.slice(0,a).concat(pick.slice(a+1));
      var okc=targ.every(function(u){return rest.some(function(t){return (t&u)===t})});if(okc)pick=rest}
    if(best===null||pick.length<best.length)best=pick}
  var T=best.length,tprod=best.map(function(t){var pr=1;for(var i=0;i<n;i++)if(t&(1<<i))pr*=rows[i].o;return pr});
  var agg=[];for(var j=0;j<=n;j++)agg.push({pj:0,min:1e18,max:-1e18,ev:0,pw:0,nmin:1e9,nmax:-1});
  var pall=0,evall=0;
  for(var W=0;W<N;W++){var j=pc(W),pr=1;for(var i=0;i<n;i++)pr*=(W&(1<<i))?rows[i].p:(1-rows[i].p);
    var pay=0,nw=0;for(var q=0;q<T;q++)if((best[q]&W)===best[q]){pay+=tprod[q];nw++}
    var prof=(pay-T)*st,A=agg[j];A.pj+=pr;A.ev+=pr*prof;if(prof<A.min)A.min=prof;if(prof>A.max)A.max=prof;
    if(prof>0)A.pw+=pr;if(nw<A.nmin)A.nmin=nw;if(nw>A.nmax)A.nmax=nw;pall+=prof>0?pr:0;evall+=pr*prof}
  var h="<div class='cards'><div class='card'><b>"+T+"</b><span>tiketů v rozpisu (z "+cand.length+" možných)</span></div>"
   +"<div class='card'><b>"+(T*st).toFixed(0)+" Kč</b><span>celkový vklad</span></div>"
   +"<div class='card'><b>"+(pall*100).toFixed(0)+" %</b><span>pravděpodobnost zisku celého rozpisu</span></div>"
   +"<div class='card'><b class='"+(evall>=0?'good':'bad')+"'>"+(evall>=0?'+':'')+evall.toFixed(0)+" Kč</b><span>očekávaný zisk podle modelu</span></div></div>";
  h+="<div class='wrap'><table><tr><th>Vyjde favoritů</th><th>Pravděpodobnost</th><th>Vyherních tiketů</th><th>Nejhorší zisk</th><th>Očekávaný zisk</th><th>Pravděpodobnost zisku</th></tr>";
  for(var j=0;j<=n;j++){var A=agg[j];if(A.pj<1e-9)continue;var e=A.ev/A.pj;
    h+="<tr"+(j===k?" class='pick'":"")+"><td>"+j+" z "+n+"</td><td>"+(A.pj*100).toFixed(1)+" %</td><td>"+A.nmin+(A.nmax!==A.nmin?"–"+A.nmax:"")+"</td>"
     +"<td class='"+(A.min>=0?'good':'bad')+"'>"+(A.min>=0?'+':'')+A.min.toFixed(0)+" Kč</td><td class='"+(e>=0?'good':'bad')+"'>"+(e>=0?'+':'')+e.toFixed(0)+" Kč</td><td>"+(A.pw/A.pj*100).toFixed(0)+" %</td></tr>"}
  h+="</table></div><h3>Tikety</h3><div class='note'>";
  best.forEach(function(t,q){var nm=[];for(var i=0;i<n;i++)if(t&(1<<i))nm.push(rows[i].f);h+=(q+1)+". "+nm.join(" + ")+" (kurz "+tprod[q].toFixed(2)+")<br>"});
  h+="</div><div class='note'>Rozpis je hledaný rychlým heuristickým postupem, takže nemusí mít úplně nejmenší možný počet tiketů. Zisk je po odečtení všech vkladů. "
   +"Pravděpodobnosti počítají favority jako nezávislé.</div>";
  out.innerHTML=h}
document.getElementById('rp_go').onclick=rozpis;
sel.onchange=function(){try{localStorage.setItem('sb_fvmin',sel.value)}catch(e){}calc()};
document.querySelectorAll('.fvc').forEach(function(c){c.onchange=calc});
calc();
})();
"""
    return page_close(out, js, static)


def render_diary(cfg, static=False, games=None):
    out = page_open("SportBayes – deník sázek", "Deník sázek", cfg, "denik", static)
    where = ("Deník je uložen jen v tomto prohlížeči (na GitHubu se nikam neposílá). Sázky se vyhodnocují ručně."
             if static else
             "Deník je uložen v souboru <b>denik.json</b> vedle aplikace (kopie předchozího stavu v denik_zaloha.json). "
             "Sázky zapsané tlačítky D / R / H u zápasu se po jeho odehrání samy vyhodnotí; u hokeje s remízou "
             "(extraliga) podle výsledku po 60 minutách, stejně jako to počítá model. Vyhodnocuje-li tvoje sázkovka "
             "jinak (např. vítěz včetně prodloužení), stav přepiš v rozbalovátku. Ručně zadané sázky se vyhodnocují ručně.")
    out.append(f"<div class='sub'>Evidence tvých sázek, zisku a návratnosti. {where}</div>")
    out.append("<div id='sum' class='cards'></div>")
    out.append("<h2>Nová sázka</h2><div class='form'>"
               "<div><label>1. Liga</label><select id='f_liga'></select></div>"
               "<div style='grid-column:span 2'><label>2. Zápas</label><select id='f_game' disabled></select></div>"
               "<div><label>3. Celkový vklad (Kč)</label><input class='t' id='f_total' inputmode='decimal' placeholder='např. 1000'></div></div>")
    out.append("<div id='blk_split' style='display:none;margin-top:10px'>"
               "<div style='margin-bottom:8px'><button onclick='splitModel()'>Podíly podle modelu</button> "
               "<button onclick='splitEqual()'>Rovným dílem</button></div>"
               "<div class='wrap'><table id='sp'></table></div>"
               "<div class='note' id='sp_sum'></div>"
               "<p><button class='pri' onclick='saveSplit()'>Zapsat označené sázky</button></p>"
               "<div class='note'>Vyber ligu a zápas, napiš celkový vklad a označ výsledky, na které sázíš. Vklad se rozdělí podle podílů "
               "v procentech (předvyplněno podle pravděpodobností modelu, podíly můžeš přepsat). Kurz doplň, pokud chybí. "
               "Sloupec vpravo ukazuje celkový zisk, když daný výsledek vyjde.</div></div>")
    out.append("<div id='blk_manual' class='form' style='display:none;margin-top:10px'>"
               "<div><label>Datum</label><input class='t' type='date' id='f_date'></div>"
               "<div><label>Liga</label><select id='f_lg'></select></div>"
               "<div style='grid-column:span 2'><label>Zápas</label><input class='t' id='f_match' placeholder='Domácí – Hosté'></div>"
               "<div><label>Tip</label><input class='t' id='f_tip' placeholder='na koho sázím'></div>"
               "<div><label>Kurz</label><input class='t' id='f_odds' inputmode='decimal'></div>"
               "<div><label>Vklad (Kč)</label><input class='t' id='f_stake' inputmode='decimal'></div>"
               "<div><label>Poznámka</label><input class='t' id='f_note'></div>"
               "<div><button class='pri' onclick='addManual()'>Přidat</button></div></div>")
    out.append("<h2>Sázky</h2><div class='wrap'><table id='tb'></table></div>"
               "<p><button onclick='exportCsv()'>Stáhnout jako CSV (Excel)</button></p>")
    pre = (f"var LG={json.dumps(_league_names(cfg), ensure_ascii=False)};"
           f"var GAMES={json.dumps(games or [], ensure_ascii=False)};").replace("</", "<\\/")
    return page_close(out, JS_DIARY, static, pre=pre)


def render_settings(cfg, static=False):
    st = load_settings(cfg, not static)
    names = _league_names(cfg)
    status = {}
    if not static:
        for lg in st["order"]:
            if lg in Handler.apps:
                status[lg] = f"načtená ({len(Handler.apps[lg].rows)} zápasů v historii)"
            elif lg in Handler.loading:
                status[lg] = "načítá se na pozadí…"
            elif lg in Handler.failed:
                status[lg] = "chyba při načtení: " + Handler.failed[lg]
            else:
                status[lg] = "skrytá, nenačtená"
    out = page_open("SportBayes – nastavení", "Nastavení", cfg, "nastaveni", static)
    out.append("<div class='sub'>Které ligy se zobrazují a v jakém pořadí.</div>")
    out.append("<div class='wrap'><table id='tb' class='lgs'></table></div>"
               "<p><button class='pri' onclick='save()'>Uložit</button></p>")
    note = ("Nastavení se ukládá jen v tomto prohlížeči; stránky na GitHubu se vždy počítají pro všechny ligy."
            if static else
            "Skrytá liga zmizí z nabídky a aplikace pro ni nestahuje žádná data (šetří i denní limit dotazů u české "
            "extraligy a české fotbalové ligy). Po opětovném zapnutí se liga načte na pozadí; první načtení nové "
            "ligy může trvat i několik minut. Nastavení je v souboru nastaveni.json.")
    out.append(f"<div class='note'>{note}</div>")
    pre = "var SET=" + json.dumps({"order": st["order"], "hidden": st["hidden"], "names": names, "status": status},
                                  ensure_ascii=False) + ";"
    return page_close(out, JS_SET, static, pre=pre)


def info_page(cfg, title, text, refresh=None):
    out = page_open("SportBayes", title, cfg, None, False, refresh)
    out.append(f"<div class='note'>{text}</div>")
    return page_close(out, "")


class Server(ThreadingHTTPServer):
    # na Windows by jinak sly spustit dve kopie na stejnem portu a prohlizec by videl tu starou
    allow_reuse_address = False


class Handler(BaseHTTPRequestHandler):
    apps = {}
    cfg = {}
    loading = set()
    failed = {}
    slock = threading.Lock()

    def _send(self, body, ctype="text/html; charset=utf-8", code=200):
        body = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            self._do_get()
        except Exception:                      # misto prazdne odpovedi ukaz chybu a ulozit ji do chyby.log
            import traceback
            tb = traceback.format_exc()
            print(f"[chyba] {self.path}\n{tb}")
            try:
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "chyby.log"), "a", encoding="utf-8") as f:
                    f.write(f"--- {dt.datetime.now():%Y-%m-%d %H:%M:%S} {self.path}\n{tb}\n")
            except OSError:
                pass
            try:
                self._send("<pre>Chyba při sestavení stránky:\n\n" + html.escape(tb) + "</pre>", code=500)
            except Exception:
                pass

    def _do_get(self):
        cfg = Handler.cfg
        path = self.path.split("?")[0].strip("/")
        name = path.replace(".html", "") or "index"
        if name == "api/denik":
            self._send(json.dumps(diary_settle(Handler.apps), ensure_ascii=False), "application/json; charset=utf-8")
            return
        if name == "denik":
            self._send(render_diary(cfg, games=upcoming_games(Handler.apps, cfg)))
            return
        if name == "favoriti":
            self._send(render_favorites(cfg, games=upcoming_games(Handler.apps, cfg)))
            return
        if name == "nastaveni":
            self._send(render_settings(cfg))
            return
        lg = name
        if name == "index":
            vl = visible_leagues(cfg)
            lg = next((l for l in vl if l in Handler.apps), vl[0] if vl else None)
            if lg is None:
                self.send_response(302)
                self.send_header("Location", "/nastaveni.html")
                self.end_headers()
                return
        app = Handler.apps.get(lg)
        if app is None:
            if lg not in data.LEAGUES or lg not in cfg.get("leagues", []):
                self.send_error(404)
            elif lg in Handler.loading:
                self._send(info_page(cfg, "Načítám ligu", f"Liga <b>{data.LEAGUES[lg]['name']}</b> se načítá "
                                     "(první načtení stahuje historii a může trvat několik minut). Stránka se sama obnoví.", 5))
            elif lg in Handler.failed and "retry=1" not in self.path:
                self._send(info_page(cfg, "Liga se nenačetla", f"{html.escape(Handler.failed[lg])}<br><br>"
                                     f"<a href='{lg}.html?retry=1'>Zkusit znovu</a>"))
            elif lg in visible_leagues(cfg):
                load_in_background(lg)
                self._send(info_page(cfg, "Načítám ligu", "Liga se načítá na pozadí, stránka se sama obnoví.", 5))
            else:
                self._send(info_page(cfg, "Liga je skrytá", "Tuto ligu zapni v <a href='nastaveni.html'>Nastavení</a>."))
            return
        app.ensure_fresh()
        self._send(render(app))

    def do_POST(self):
        path = self.path.split("?")[0].strip("/")
        try:
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                raise ValueError("očekávám JSON")
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                raise ValueError("příliš velký požadavek")
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            if path == "api/denik":
                res = diary_apply(body)
            elif path == "api/nastaveni":
                res = apply_settings(Handler.cfg, body)
            else:
                self.send_error(404)
                return
            self._send(json.dumps(res, ensure_ascii=False), "application/json; charset=utf-8")
        except (ValueError, KeyError, TypeError, AttributeError) as ex:
            self._send(json.dumps({"error": str(ex)}, ensure_ascii=False), "application/json; charset=utf-8", 400)

    def log_message(self, *a):
        pass


def load_in_background(lg):
    """Nacte ligu v samostatnem vlakne (pouziva se, kdyz ji uzivatel zapne v Nastaveni)."""
    with Handler.slock:
        if lg in Handler.apps or lg in Handler.loading:
            return
        Handler.loading.add(lg)
        Handler.failed.pop(lg, None)

    def job():
        try:
            app = start(Handler.cfg, lg)
            app.ensure_fresh(force=True)
            Handler.apps[lg] = app
        except Exception as ex:
            Handler.failed[lg] = f"{type(ex).__name__}: {ex}"
        finally:
            Handler.loading.discard(lg)
    threading.Thread(target=job, daemon=True).start()


def apply_settings(cfg, body):
    allowed = [l for l in cfg.get("leagues") or [] if l in data.LEAGUES]
    order = [l for l in (body.get("order") or []) if l in allowed]
    order += [l for l in allowed if l not in order]
    hidden = [l for l in (body.get("hidden") or []) if l in allowed]
    if not [l for l in order if l not in hidden]:
        raise ValueError("aspoň jedna liga musí zůstat zapnutá")
    save_json(SETTINGS_FILE, {"order": order, "hidden": hidden})
    loading = [l for l in order if l not in hidden and l not in Handler.apps]
    for l in loading:
        load_in_background(l)
    return {"ok": True, "loading": loading}


# ---------------------------------------------------------------- prikazy
def _progress(d, n):
    print(f"  {d:%Y-%m-%d}: {n} zápasů", flush=True)


def leagues(cfg, only=None):
    ls = [l for l in (cfg.get("leagues") or ["nba"]) if l in data.LEAGUES]
    if only:
        if only not in data.LEAGUES:
            print("Neznámá liga:", only, "- k dispozici:", ", ".join(data.LEAGUES))
            sys.exit(1)
        ls = [only]
    return ls


def start(cfg, lg="nba"):
    app = App(cfg, lg)
    print(f"[{app.info['name']}] Načítám historii zápasů (první spuštění trvá déle)...")
    new = app.init(_progress)
    print(f"[{app.info['name']}] Historie: {len(app.rows)} zápasů ({new} nových).")
    if len(app.rows) < 200:
        print("POZOR: historie je krátká, pravděpodobnosti budou nepřesné. Spusť: python sportbayes.py test")
    return app


def cmd_serve(cfg, only=None):
    apps = {}
    hidden = [] if only else load_settings(cfg)["hidden"]
    Handler.cfg = cfg
    for lg in leagues(cfg, only):
        if lg in hidden:
            print(f"[{lg}] Liga je skrytá v Nastavení, nenačítám ji.")
            continue
        try:
            app = start(cfg, lg)
        except Exception as ex:
            print(f"[{lg}] Ligu se nepodařilo načíst ({type(ex).__name__}: {ex}), přeskakuji.")
            Handler.failed[lg] = f"{type(ex).__name__}: {ex}"
            continue
        app.ensure_fresh(force=True)
        if app.error:
            print(f"[{lg}] Chyba při načítání dnešních zápasů:", app.error)
        apps[lg] = app
    if not apps and not hidden:
        print("Nepodařilo se načíst žádnou ligu.")
        return
    Handler.apps = apps
    port = int(cfg["port"])
    srv = None
    for p in range(port, port + 10):
        try:
            srv = Server(("127.0.0.1", p), Handler)
            break
        except OSError:
            print(f"Port {p} je obsazený (asi běží starší okno aplikace - zavři ho), zkouším další...")
    if srv is None:
        print("Nepodařilo se najít volný port. Zavři stará okna aplikace a spusť znovu.")
        return
    port = srv.server_address[1]
    url = f"http://127.0.0.1:{port}/" + ("" if apps else "nastaveni.html")
    print(f"Dashboard běží na {url}  (ukončení: Ctrl+C)")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nUkončeno.")


def cmd_today(cfg, only=None):
    for lg in leagues(cfg, only):
        app = start(cfg, lg)
        app.ensure_fresh(force=True)
        if app.error:
            print("Chyba:", app.error)
        sides = app.sides
        print(f"\n{'začátek':15s} {'zápas':15s} {'stav':14s} {'model ' + '/'.join(LBL[k] for k in sides):>12s} {'kurz':>16s}  tip")
        for r in app.board:
            o, ps = r["odds"], r["p"]
            kurz = "/".join(f"{float(o[k]):.2f}" for k in sides) if o else "-"
            stav = "před" if r["state"] == "pre" else f"{r['home_pts']}:{r['away_pts']} {r['detail']}"
            tip = "" if not r["pick"] else {"home": r["home"], "away": r["away"], "draw": "remíza"}[r["pick"]]
            if tip:
                tip += f" ({r['edge'][r['pick']] * 100:+.0f} %)"
            p = "-" if ps is None else "/".join(f"{ps[k] * 100:.0f}" for k in sides) + " %"
            print(f"{r['when']:15s} {r['home'] + ' – ' + r['away']:15s} {stav[:14]:14s} {p:>12s} {kurz:>16s}  {tip}")
        print()


def cmd_build(cfg, only=None):
    """Vygeneruje hotove stranky docs/index.html, docs/nhl.html (pro GitHub Pages)."""
    out_dir = os.path.join(HERE, "docs")
    os.makedirs(out_dir, exist_ok=True)
    ok = 0
    built = {}
    for lg in leagues(cfg, only):
        app = App(cfg, lg)
        try:
            new = app.init(_progress)
            print(f"[{lg}] Historie: {len(app.rows)} zápasů ({new} nových).")
        except Exception as ex:
            app.rows = data.load_games(lg)
            if not app.rows:
                print(f"[{lg}] Historii se nepodařilo stáhnout:", ex)
                continue
            app.rebuild()
            print(f"[{lg}] Historii se nepodařilo doplnit, používám uloženou:", ex)
        app.ensure_fresh(force=True)
        if app.error:
            print(f"[{lg}] Chyba při načítání zápasů:", app.error)
        name = "index.html" if lg == "nba" else lg + ".html"
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
            f.write(render(app, static=True))
        print(f"[{lg}] Hotovo:", os.path.join(out_dir, name), "| zápasů na stránce:", len(app.board))
        ok += 1
        built[lg] = app
    if not ok:
        sys.exit(1)
    for name, fn in (("denik.html", lambda c, static: render_diary(c, static, upcoming_games(built, c, True))),
                     ("favoriti.html", lambda c, static: render_favorites(c, static, upcoming_games(built, c, True))),
                     ("nastaveni.html", render_settings)):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
            f.write(fn(cfg, static=True))


def cmd_backtest(cfg, only=None):
    lg = only or "nba"
    leagues(cfg, lg)
    app = start(cfg, lg)
    games = data.to_model_games(app.rows, lg)
    seasons = sorted({g["season"] for g in games})
    if len(seasons) < 3:
        print("Na backtest je potřeba historie alespoň 3 sezón.")
        return
    test_from = seasons[-2]
    train = [g for g in games if g["season"] < test_from]
    print(f"\nLadění parametrů na sezónách {seasons[0]}–{seasons[-3]} (cca půl minuty)...")
    best, _ = tune(train, get_params(cfg, lg), collect_from=seasons[1] if len(seasons) > 3 else None, verbose=True)
    best = tune_draw(train, best, collect_from=seasons[1] if len(seasons) > 3 else None)
    _, preds, _ = run(games, best, collect_from=test_from)
    m = metrics(preds)
    coin = 1.0986 if m["three_way"] else 0.6931
    print(f"\nTEST mimo vzorek – sezóny {test_from}–{seasons[-1]}, {m['n']} zápasů")
    print(f"  trefený vítěz : {m['accuracy'] * 100:.1f} %   (vždy tipovat domácí: {m['home_win_rate'] * 100:.1f} %)")
    print(f"  log-loss      : {m['logloss']:.4f}   (náhodný tip {coin:.4f}, menší = lepší)")
    if m["three_way"]:
        print(f"  remíz         : {m['draw_rate'] * 100:.1f} %")
    print("\nKalibrace (model řekl X % pro favorita -> jak často favorit vyhrál):")
    for r in calibration(preds):
        print(f"  {r['od'] * 100:3.0f}–{r['do'] * 100:3.0f} %  n={r['n']:4d}  model {r['model'] * 100:5.1f} %  skutečnost {r['skutecnost'] * 100:5.1f} %")
    cfg[params_key(lg)] = best.to_dict()
    save_config(cfg)
    print("\nVyladěné parametry uloženy do config.json.")


def cmd_test(cfg, only=None):
    for lg in leagues(cfg, only):
        print(f"===== {data.LEAGUES[lg]['name']} =====")
        _test_league(cfg, lg)


def _test_league(cfg, lg):
    if data.is_ch(lg):
        try:
            evs = data.ch_window(lg)
            print(f"   OK: {len(evs)} zápasů od včerejška do +7 dní (zdroj ceskyhokej.cz)")
            for x in evs[:6]:
                print("  ", x["date_utc"], x["home"], "–", x["away"], "|", x["state"], x.get("score_txt"), x["detail"])
        except Exception as ex:
            print("   CHYBA:", type(ex).__name__, ex)
        return
    if data.is_apisports(lg):
        try:
            evs = data.as_window(lg)
            print(f"   OK: {len(evs)} zápasů včera/dnes/zítra | dnes využito dotazů: {data.as_used(data.LEAGUES[lg]['host'])}")
            for x in evs[:4]:
                print("  ", x["date_utc"], x["home"], "–", x["away"], "|", x["state"], x.get("score_txt"), x["detail"])
        except Exception as ex:
            print("   CHYBA:", type(ex).__name__, ex)
        return
    print("1) Dnešní rozpis ze zdroje výsledků...")
    try:
        today = data.us_today()
        js = data.http_json(data.espn_url(lg) + "?dates=" + today.strftime("%Y%m%d"))
        raw = js.get("events") or []
        evs = [p for p in (data.parse_event(x) for x in raw) if p]
        print(f"   OK: {len(raw)} událostí, z toho čitelných {len(evs)}")
        if raw and not evs:
            os.makedirs(data.DATA_DIR, exist_ok=True)
            pth = os.path.join(data.DATA_DIR, lg + "_raw_sample.json")
            with open(pth, "w", encoding="utf-8") as f:
                json.dump(raw[0], f, indent=1)
            print("   CHYBA: formát se změnil. Vzorek uložen do", pth)
        for x in evs[:3]:
            print("  ", x["date_utc"], x["home"], "–", x["away"], "| stav:", x["state"], "| kurzy:", x["odds"])
        if evs and not any(x["odds"] for x in evs if x["state"] == "pre") and any(x["state"] == "pre" for x in evs):
            os.makedirs(data.DATA_DIR, exist_ok=True)
            pth = os.path.join(data.DATA_DIR, lg + "_raw_sample.json")
            pre = [x for x in raw if ((x.get("status") or {}).get("type") or {}).get("state") == "pre"]
            with open(pth, "w", encoding="utf-8") as f:
                json.dump((pre or raw)[0], f, indent=1)
            print("   Pozn.: zdroj teď neposílá kurzy (nebo v jiném tvaru). Vzorek uložen do", pth)
    except Exception as ex:
        print("   CHYBA:", type(ex).__name__, ex)
        return
    print("2) Dotaz na rozsah dní (historie)...")
    try:
        d1 = dt.date(today.year - 1, 12, 1)
        h = data.fetch_scoreboard(d1, d1 + dt.timedelta(days=6), lg)
        days = {x["date_utc"][:10] for x in h}
        print(f"   OK: {len(h)} zápasů v {len(days)} dnech" + ("" if len(days) > 1 else "  (rozsah nefunguje, použije se stahování po dnech)"))
    except Exception as ex:
        print("   CHYBA:", type(ex).__name__, ex)
    if cfg.get("odds_api_key"):
        print("3) The Odds API...")
        o = data.fetch_odds_api(cfg["odds_api_key"], cfg.get("odds_api_region", "eu"), 0, lg)
        print(f"   kurzy pro {len(o)} zápasů")
    else:
        print("3) The Odds API: klíč není vyplněn v config.json (nepovinné).")


def main():
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(errors="replace")
        except Exception:
            pass
    cfg = load_config()
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "serve"
    only = sys.argv[2].lower() if len(sys.argv) > 2 else None
    {"serve": cmd_serve, "today": cmd_today, "backtest": cmd_backtest,
     "test": cmd_test, "build": cmd_build}.get(cmd, lambda c, o=None: print(__doc__))(cfg, only)


if __name__ == "__main__":
    main()
