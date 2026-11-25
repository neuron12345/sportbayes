# -*- coding: utf-8 -*-
"""Stahovani vysledku, rozpisu a kurzu NBA + lokalni cache v data/games.csv."""
import csv
import html
import re
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
def _soccer(name, country, path, odds_key, prefix):
    return {"name": name, "sport": country, "path": "soccer/" + path, "odds_key": odds_key,
            "prefix": prefix, "unit": "g.", "dec": 2, "params": "soccer", "three_way": True,
            "keep_types": None, "off_months": (6, 7), "start_month": 8, "min_team_games": 0,
            "window": (-4, 14), "b2b": False}


LEAGUES = {
    "nba": {"name": "NBA", "sport": "basketbal", "path": "basketball/nba", "odds_key": "basketball_nba",
            "prefix": "", "unit": "b.", "dec": 1, "params": "nba", "three_way": False,
            "keep_types": (2, 3, 5), "off_months": (7, 8, 9), "start_month": 10, "min_team_games": 10,
            "window": (-1, 1), "b2b": True},
    "nhl": {"name": "NHL", "sport": "hokej", "path": "hockey/nhl", "odds_key": "icehockey_nhl",
            "prefix": "nhl_", "unit": "g.", "dec": 2, "params": "nhl", "three_way": False,
            "keep_types": (2, 3, 5), "off_months": (7, 8, 9), "start_month": 10, "min_team_games": 10,
            "window": (-1, 1), "b2b": True},
    "epl": _soccer("Premier League", "Anglie", "eng.1", "soccer_epl", "epl_"),
    "laliga": _soccer("La Liga", "Španělsko", "esp.1", "soccer_spain_la_liga", "laliga_"),
    "seriea": _soccer("Serie A", "Itálie", "ita.1", "soccer_italy_serie_a", "seriea_"),
    "ligue1": _soccer("Ligue 1", "Francie", "fra.1", "soccer_france_ligue_one", "ligue1_"),
    "bundesliga": _soccer("Bundesliga", "Německo", "ger.1", "soccer_germany_bundesliga", "bundesliga_"),
}


def _apisports(name, sport, host, as_path, league_id, prefix, params, hockey):
    return {"name": name, "sport": sport, "provider": "apisports", "host": host, "as_path": as_path,
            "league_id": league_id, "path": "", "odds_key": "", "prefix": prefix, "unit": "g.", "dec": 2,
            "params": params, "three_way": True, "keep_types": None, "off_months": (), "start_month": 8,
            "min_team_games": 0, "window": (-1, 1), "b2b": hockey, "hockey": hockey,
            "free_seasons": (2022, 2023, 2024)}


# Extraliga: zdroj je web Českého hokeje (ceskyhokej.cz), API-Sports se nepouziva.
# Nove soubory maji predponu "extrach_", aby se nemichaly se starymi daty z API-Sports.
LEAGUES["extraliga"] = {"name": "Extraliga", "sport": "Česko · hokej", "provider": "ceskyhokej",
                        "comps": {2026: 14}, "hk_seasons": (2025, 2024, 2023, 2022), "path": "", "odds_key": "", "prefix": "extrach_", "unit": "g.",
                        "dec": 2, "params": "hockey3", "three_way": True, "keep_types": None,
                        "off_months": (), "start_month": 9, "min_team_games": 0, "window": (-1, 1),
                        "b2b": True, "hockey": True}
LEAGUES["czliga"] = _apisports("1. liga", "Česko · fotbal", "v3.football.api-sports.io", "/fixtures", 345,
                               "czliga_", "soccer", False)


def is_apisports(lg):
    return LEAGUES[lg].get("provider") == "apisports"


def is_ch(lg):
    return LEAGUES[lg].get("provider") == "ceskyhokej"


def keep_event(e, lg="nba"):
    kt = LEAGUES[lg]["keep_types"]
    return kt is None or e["season_type"] in kt


def games_csv(lg="nba"):
    return os.path.join(DATA_DIR, LEAGUES[lg]["prefix"] + "games.csv")


def preds_csv(lg="nba"):
    return os.path.join(DATA_DIR, LEAGUES[lg]["prefix"] + "predictions.csv")


def odds_cache(lg="nba"):
    return os.path.join(DATA_DIR, LEAGUES[lg]["prefix"] + "odds_cache.json")


def espn_url(lg="nba"):
    return "https://site.api.espn.com/apis/site/v2/sports/" + LEAGUES[lg]["path"] + "/scoreboard"


def odds_url(lg="nba"):
    return "https://api.the-odds-api.com/v4/sports/" + LEAGUES[lg]["odds_key"] + "/odds"


FIELDS = ["event_id", "date_utc", "season", "season_type", "home", "away",
          "home_pts", "away_pts", "neutral"]
US_OFFSET = dt.timedelta(hours=5)   # UTC -> priblizne vychodni cas USA


def http_json(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 sportbayes"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------- cas
def parse_utc(s):
    s = s.strip().replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        d = dt.datetime.strptime(s[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=dt.timezone.utc)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def us_day(d_utc):
    """Herni den v USA (zapas v 01:00 UTC patri k predchozimu dni)."""
    return (d_utc - US_OFFSET).date()


def us_today():
    return us_day(dt.datetime.now(dt.timezone.utc))


# ---------------------------------------------------------------- kurzy
def american_to_decimal(v):
    if v is None:
        return None
    if isinstance(v, str):
        t = v.strip().upper().replace("−", "-")
        if t in ("EVEN", "EV", "PK"):
            return 2.0
        try:
            v = float(t)
        except ValueError:
            return None
    v = float(v)
    if v == 0:
        return None
    if -100 < v < 100:          # uz je to desetinny kurz
        return v if v > 1 else None
    return 1 + v / 100.0 if v > 0 else 1 + 100.0 / (-v)


def _dig(d, *path):
    for k in path:
        if isinstance(d, dict):
            d = d.get(k)
        else:
            return None
    return d


def _first_odds(cands):
    for c in cands:
        v = american_to_decimal(c)
        if v:
            return v
    return None


def _espn_odds(comp):
    """Zkusi vsechny zname tvary kurzu v odpovedi ESPN. Vrati dict nebo None."""
    for o in comp.get("odds") or []:
        if not isinstance(o, dict):
            continue
        res = {}
        for side, team_key in (("home", "homeTeamOdds"), ("away", "awayTeamOdds"), ("draw", "drawOdds")):
            res[side] = _first_odds([_dig(o, "moneyline", side, "close", "odds"),
                                     _dig(o, "moneyline", side, "current", "odds"),
                                     _dig(o, "moneyline", side, "open", "odds"),
                                     _dig(o, team_key, "moneyLine")])
        if res["home"] and res["away"]:
            out = {"home": round(res["home"], 3), "away": round(res["away"], 3),
                   "source": _dig(o, "provider", "name") or "ESPN", "line": o.get("details") or ""}
            if res["draw"]:
                out["draw"] = round(res["draw"], 3)
            return out
    return None


# ---------------------------------------------------------------- ESPN
def parse_event(ev):
    """Prevede jednu udalost ESPN na jednoduchy dict; None kdyz neni pouzitelna."""
    try:
        comp = ev["competitions"][0]
        home = away = None
        for c in comp["competitors"]:
            if c.get("homeAway") == "home":
                home = c
            elif c.get("homeAway") == "away":
                away = c
        if not home or not away:
            return None
        st = (comp.get("status") or ev.get("status") or {})
        typ = st.get("type") or {}

        def pts(c):
            try:
                return int(float(c.get("score")))
            except (TypeError, ValueError):
                return None

        d = parse_utc(ev.get("date") or comp.get("date"))
        return {
            "event_id": str(ev.get("id")),
            "date_utc": d.strftime("%Y-%m-%dT%H:%MZ"),
            "season": int(_dig(ev, "season", "year") or 0) or (d.year + (1 if d.month >= 7 else 0)),
            "season_type": int(_dig(ev, "season", "type") or 0),
            "home": home["team"].get("abbreviation") or home["team"].get("displayName"),
            "away": away["team"].get("abbreviation") or away["team"].get("displayName"),
            "home_name": home["team"].get("displayName") or "",
            "away_name": away["team"].get("displayName") or "",
            "home_pts": pts(home), "away_pts": pts(away),
            "neutral": 1 if comp.get("neutralSite") else 0,
            "state": typ.get("state") or "",            # pre / in / post
            "completed": bool(typ.get("completed")),
            "detail": typ.get("shortDetail") or typ.get("detail") or "",
            "odds": _espn_odds(comp),
        }
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _events(params, lg="nba"):
    js = http_json(espn_url(lg) + "?" + urllib.parse.urlencode(params))
    out = []
    for ev in js.get("events") or []:
        p = parse_event(ev)
        if p:
            out.append(p)
    return out


def fetch_scoreboard(d1, d2=None, lg="nba"):
    """Udalosti pro den nebo rozsah dni (datum = herni den v USA)."""
    if not d2 or d2 == d1:
        return _events({"dates": d1.strftime("%Y%m%d")}, lg)
    q = d1.strftime("%Y%m%d") + "-" + d2.strftime("%Y%m%d")
    try:
        return _events({"dates": q, "limit": 1000}, lg)
    except urllib.error.HTTPError as ex:
        if ex.code not in (400, 404):
            raise
        return _events({"dates": q}, lg)


def _fetch_days(d1, d2, lg="nba"):
    evs, day = [], d1
    while day <= d2:
        evs += fetch_scoreboard(day, None, lg)
        day += dt.timedelta(days=1)
        time.sleep(0.1)
    return evs


_CHUNK = {"days": 31}       # 31 = po mesicich, 7 = po tydnech, 1 = po dnech


def fetch_range(d1, d2, progress=None, on_chunk=None, lg="nba"):
    """Po mesicich; kdyz server rozsah odmitne, zkusi tydny a nakonec jednotlive dny."""
    out = {}
    cur = d1
    while cur <= d2:
        if cur.month in LEAGUES[lg]["off_months"]:    # mimo sezonu
            cur = (cur.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
            continue
        month_end = (cur.replace(day=1) + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)
        evs = None
        while evs is None:
            n = _CHUNK["days"]
            end = min(d2, month_end, cur + dt.timedelta(days=n - 1))
            try:
                if n == 1 or end == cur:
                    evs = fetch_scoreboard(cur, None, lg)
                    end = cur
                else:
                    evs = fetch_scoreboard(cur, end, lg)
                    days = {e["date_utc"][:10] for e in evs}
                    if (end - cur).days >= 3 and len(days) == 1 and cur.month in (11, 12, 1, 2, 3):
                        raise urllib.error.HTTPError("", 400, "rozsah ignorovan", None, None)
            except urllib.error.HTTPError as ex:
                if ex.code not in (400, 404) or n == 1:
                    raise
                _CHUNK["days"] = 7 if n > 7 else 1
                print(f"  (zdroj odmitl rozsah {n} dni, zkousim po {_CHUNK['days']})")
        for e in evs:
            out[e["event_id"]] = e
        if on_chunk:
            on_chunk(evs)
        if progress:
            progress(cur, len(evs))
        cur = end + dt.timedelta(days=1)
        time.sleep(0.15)
    return list(out.values())


# ---------------------------------------------------------------- cache
def load_games(lg="nba"):
    rows = []
    if os.path.exists(games_csv(lg)):
        with open(games_csv(lg), newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    r["season"] = int(r["season"]); r["season_type"] = int(r["season_type"])
                    r["home_pts"] = int(r["home_pts"]); r["away_pts"] = int(r["away_pts"])
                    r["neutral"] = int(r.get("neutral") or 0)
                    rows.append(r)
                except (ValueError, KeyError):
                    continue
    return rows


def save_games(rows, lg="nba"):
    os.makedirs(DATA_DIR, exist_ok=True)
    rows = sorted(rows, key=lambda r: (r["date_utc"], r["event_id"]))
    tmp = games_csv(lg) + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, games_csv(lg))


def is_final(e, lg="nba"):
    return (e.get("completed") and e["home_pts"] is not None and e["away_pts"] is not None
            and (e["home_pts"] != e["away_pts"] or LEAGUES[lg]["three_way"]) and keep_event(e, lg))


def merge_finals(rows, events, lg="nba"):
    """Prida dohrane zapasy do cache. Vrati pocet novych."""
    have = {r["event_id"] for r in rows}
    n = 0
    for e in events:
        if is_final(e, lg) and e["event_id"] not in have:
            rows.append({k: e[k] for k in FIELDS})
            have.add(e["event_id"])
            n += 1
    return n


def update_history(seasons_back=3, progress=None, lg="nba"):
    """Prvni beh stahne posledni sezony, dalsi behy jen doplni chybejici dny."""
    if is_apisports(lg):
        return as_history(lg, progress)
    if is_ch(lg):
        return ch_history(lg, progress)
    rows = load_games(lg)
    today = us_today()
    if rows:
        last = parse_utc(max(r["date_utc"] for r in rows))
        start = us_day(last) - dt.timedelta(days=3)
    else:
        sm = LEAGUES[lg]["start_month"]
        y = today.year if today.month >= sm else today.year - 1
        start = dt.date(y - (seasons_back - 1), sm, 1)
    new = [0]

    def on_chunk(evs):                 # prubezne ukladani - preruseni nic neztrati
        n = merge_finals(rows, evs, lg)
        if n:
            new[0] += n
            save_games(rows, lg)

    if start <= today:
        fetch_range(start, today, progress, on_chunk, lg)
    return rows, new[0]


def to_model_games(rows, lg="nba"):
    """Cache -> vstup modelu. Vyradi exhibice (napr. All-Star) podle poctu zapasu tymu."""
    cnt = {}
    for r in rows:
        cnt[r["home"]] = cnt.get(r["home"], 0) + 1
        cnt[r["away"]] = cnt.get(r["away"], 0) + 1
    limit = LEAGUES[lg]["min_team_games"] if len(rows) > 400 else 0
    out = []
    for r in sorted(rows, key=lambda r: (r["date_utc"], r["event_id"])):
        if cnt[r["home"]] < limit or cnt[r["away"]] < limit:
            continue
        d = parse_utc(r["date_utc"])
        out.append({"event_id": r["event_id"], "season": r["season"],
                    "day": us_day(d).toordinal(), "home": r["home"], "away": r["away"],
                    "margin": r["home_pts"] - r["away_pts"], "neutral": bool(r["neutral"])})
    return out


# ---------------------------------------------------------------- The Odds API (volitelne)
def _nick(name):
    import unicodedata
    t = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    t = t.replace("-", " ").replace(".", " ").replace("&", "and").split()
    return t[-1] if t else ""


def fetch_odds_api(key, region="eu", max_age_min=120, lg="nba"):
    """Kurzy z The Odds API (1 kredit na dotaz). Vysledek se cachuje, aby stacil tarif zdarma."""
    if not key:
        return {}
    now = time.time()
    try:
        with open(odds_cache(lg), encoding="utf-8") as f:
            c = json.load(f)
        if now - c.get("ts", 0) < max_age_min * 60 and c.get("region") == region:
            return c.get("odds", {})
    except (OSError, ValueError):
        c = {}
    url = odds_url(lg) + "?" + urllib.parse.urlencode(
        {"apiKey": key, "regions": region, "markets": "h2h", "oddsFormat": "decimal"})
    try:
        js = http_json(url)
    except Exception as ex:                      # sit / limit -> pouzij starou cache
        print("The Odds API nedostupne:", ex)
        return c.get("odds", {})
    three = LEAGUES[lg]["three_way"]
    odds = {}
    for g in js if isinstance(js, list) else []:
        hn, an = g.get("home_team"), g.get("away_team")
        best = {"home": 0.0, "draw": 0.0, "away": 0.0}
        fair = []
        for b in g.get("bookmakers") or []:
            for m in b.get("markets") or []:
                if m.get("key") != "h2h":
                    continue
                pr = {o.get("name"): o.get("price") for o in m.get("outcomes") or []}
                o3 = {"home": pr.get(hn), "away": pr.get(an), "draw": pr.get("Draw")}
                sides = ("home", "draw", "away") if three else ("home", "away")
                if any(not o3[k] or o3[k] <= 1 for k in sides):
                    continue
                tot = sum(1 / o3[k] for k in sides)
                fair.append({k: (1 / o3[k]) / tot for k in sides})
                for k in sides:
                    best[k] = max(best[k], o3[k])
        if fair:
            k = _nick(hn) + "|" + _nick(an) + "|" + (g.get("commence_time") or "")[:10]
            rec = {"home": best["home"], "away": best["away"],
                   "source": f"nejlepsi z {len(fair)} sazkovek",
                   "fair_home": sum(f["home"] for f in fair) / len(fair),
                   "fair_away": sum(f["away"] for f in fair) / len(fair)}
            if three:
                rec["draw"] = best["draw"]
                rec["fair_draw"] = sum(f["draw"] for f in fair) / len(fair)
            odds[k] = rec
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(odds_cache(lg), "w", encoding="utf-8") as f:
        json.dump({"ts": now, "region": region, "odds": odds}, f)
    return odds


def match_odds_api(odds, ev):
    if not odds:
        return None
    d = parse_utc(ev["date_utc"])
    for delta in (0, -1, 1):
        day = (d + dt.timedelta(days=delta)).strftime("%Y-%m-%d")
        k = _nick(ev["home_name"]) + "|" + _nick(ev["away_name"]) + "|" + day
        if k in odds:
            return odds[k]
    return None


# ---------------------------------------------------------------- API-Sports (ceske ligy)
# Bezplatny tarif: 100 dotazu denne, cele sezony jen 2022-2024, podle data jen vcera/dnes/zitra.
APISPORTS_KEY = ""
AS_DAILY_LIMIT = 95
_AS_FINAL_H = {"FT": "", "AOT": " pp", "AP": " sn"}
_AS_FINAL_F = {"FT": "", "AET": " pp", "PEN": " pen"}
_AS_DEAD = {"POST", "CANC", "INTR", "ABD", "AW", "PST", "AWD", "WO", "SUSP"}


def _as_usage_path():
    return os.path.join(DATA_DIR, "apisports_usage.json")


def as_used(host):
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    try:
        with open(_as_usage_path(), encoding="utf-8") as f:
            u = json.load(f)
    except (OSError, ValueError):
        u = {}
    return int((u.get(day) or {}).get(host, 0))


def _as_count(host):
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    try:
        with open(_as_usage_path(), encoding="utf-8") as f:
            u = json.load(f)
    except (OSError, ValueError):
        u = {}
    u = {day: u.get(day) or {}}
    u[day][host] = int(u[day].get(host, 0)) + 1
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(_as_usage_path(), "w", encoding="utf-8") as f:
        json.dump(u, f)


def http_json_key(url, key, timeout=30):
    req = urllib.request.Request(url, headers={"x-apisports-key": key, "User-Agent": "sportbayes"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def as_get(lg, **params):
    info = LEAGUES[lg]
    if not APISPORTS_KEY:
        raise RuntimeError("chybi klic: dopln apisports_key v config.json")
    if as_used(info["host"]) >= AS_DAILY_LIMIT:
        raise RuntimeError("denni limit 100 dotazu API-Sports je vycerpan, obnovi se o pulnoci UTC")
    _as_count(info["host"])
    js = http_json_key("https://" + info["host"] + info["as_path"] + "?" + urllib.parse.urlencode(params),
                       APISPORTS_KEY)
    err = js.get("errors")
    if err:
        raise RuntimeError("API-Sports: " + json.dumps(err, ensure_ascii=False)[:200])
    return js.get("response") or []


def as_parse(x, lg):
    """Zapas z API-Sports -> stejny tvar jako parse_event. U hokeje je skore pro model
    po zakladni hraci dobe (prodlouzeni a najezdy = remiza)."""
    info = LEAGUES[lg]
    try:
        if info["hockey"]:
            st = (x.get("status") or {}).get("short") or ""
            long = (x.get("status") or {}).get("long") or st
            hs, aw = (x.get("scores") or {}).get("home"), (x.get("scores") or {}).get("away")
            gid, date = x.get("id"), x.get("date")
            finals = _AS_FINAL_H
            if st in ("AOT", "AP") and hs is not None and aw is not None:
                reg_h = reg_a = min(int(hs), int(aw))
            else:
                reg_h, reg_a = hs, aw
        else:
            fx = x.get("fixture") or {}
            st = (fx.get("status") or {}).get("short") or ""
            long = (fx.get("status") or {}).get("long") or st
            hs, aw = (x.get("goals") or {}).get("home"), (x.get("goals") or {}).get("away")
            gid, date = fx.get("id"), fx.get("date")
            finals = _AS_FINAL_F
            reg_h, reg_a = hs, aw
            ft = (x.get("score") or {}).get("fulltime") or {}
            if st in ("AET", "PEN") and ft.get("home") is not None:
                reg_h, reg_a = ft.get("home"), ft.get("away")
        if (x.get("league") or {}).get("id") != info["league_id"]:
            return None
        if st in finals and hs is not None and aw is not None:
            state, completed = "post", True
        elif st in ("NS", "TBD"):
            state, completed = "pre", False
        elif st in _AS_DEAD:
            state, completed = "post", False
        else:
            state, completed = "in", False
        d = parse_utc(date)
        hn = ((x.get("teams") or {}).get("home") or {}).get("name") or "?"
        an = ((x.get("teams") or {}).get("away") or {}).get("name") or "?"
        txt = ""
        if hs is not None and aw is not None:
            txt = f"{int(hs)}:{int(aw)}" + (finals.get(st, "") if completed else "")
        return {
            "event_id": info["prefix"] + str(gid),
            "date_utc": d.strftime("%Y-%m-%dT%H:%MZ"),
            "season": int((x.get("league") or {}).get("season") or 0) or (d.year - (0 if d.month >= 7 else 1)),
            "season_type": 0, "home": hn, "away": an, "home_name": hn, "away_name": an,
            "home_pts": None if reg_h is None else int(reg_h),
            "away_pts": None if reg_a is None else int(reg_a),
            "neutral": 0, "state": state, "completed": completed,
            "detail": "" if state == "pre" else ("konec" if completed else long),
            "score_txt": txt, "odds": None,
        }
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return None


def as_history(lg, progress=None):
    """Jednorazove stahne sezony, ktere bezplatny tarif povoluje (1 dotaz na sezonu)."""
    rows = load_games(lg)
    have = {r["season"] for r in rows}
    new = 0
    for y in LEAGUES[lg]["free_seasons"]:
        if y in have:
            continue
        evs = [e for e in (as_parse(x, lg) for x in as_get(lg, league=LEAGUES[lg]["league_id"], season=y)) if e]
        n = merge_finals(rows, evs, lg)
        new += n
        if n:
            save_games(rows, lg)
        if progress:
            progress(dt.date(y, 9, 1), n)
    return rows, new


def as_window(lg):
    """Zapasy vcera/dnes/zitra (UTC). Setri denni limit: dnesek se nacita po 30 minutach
    (po 15, kdyz se hraje), vcerejsek a zitrek po 6 hodinach; cache je na disku."""
    path = os.path.join(DATA_DIR, LEAGUES[lg]["prefix"] + "days.json")
    try:
        with open(path, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    today = dt.datetime.now(dt.timezone.utc).date()
    now, out, changed, err = time.time(), {}, False, None
    for off in (0, -1, 1):
        key = (today + dt.timedelta(days=off)).isoformat()
        c = cache.get(key) or {"ts": 0, "events": None}
        evs = c["events"]
        live = any(e["state"] == "in" for e in evs or [])
        open_ = any(not e["completed"] and e["state"] != "post" for e in evs or [])
        slow = 2 if os.environ.get("GITHUB_ACTIONS") else 1     # na GitHubu bezi vypocet kazdych 15 min -> sirsi rozestupy
        if off == 0:
            ttl = (900 if live else 1800) * slow
        elif off == -1:
            ttl = 6 * 3600 if (open_ or evs is None) else 10 ** 9
        else:
            ttl = 6 * 3600
        if evs is None or now - c["ts"] > ttl:
            try:
                evs = [e for e in (as_parse(x, lg) for x in as_get(lg, date=key)) if e]
                cache[key] = {"ts": now, "events": evs}
                changed = True
            except Exception as ex:            # limit / sit -> pouzij, co uz mame
                err = ex
                evs = evs or []
        for e in evs:
            out[e["event_id"]] = e
    if changed:
        keep = {(today + dt.timedelta(days=o)).isoformat() for o in (-2, -1, 0, 1)}
        cache = {k: v for k, v in cache.items() if k in keep}
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    if err is not None and not out:
        raise err
    return list(out.values())


# ---------------------------------------------------------------- Extraliga: ceskyhokej.cz
CH_BASE = "https://www.ceskyhokej.cz"
CH_PAUSE = 1.5          # pauza mezi dotazy na web (s)
CH_MAX_PAGES = 20
_CH_RE_ITEM = re.compile(r'<div class="game-item-wrapper">(.*?)</a>\s*<div></div>\s*</div>', re.S)
_CH_RE_DATE = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4}),\s*(\d{1,2}):(\d{2})")


def _last_sunday(y, m):
    d = dt.date(y, m, 31)
    return d - dt.timedelta(days=(d.weekday() + 1) % 7)


def prague_to_utc(y, mo, d, h, mi):
    """Cas na webu je cesky. Letni cas: od posledni nedele v breznu do posledni nedele v rijnu (01:00 UTC)."""
    naive = dt.datetime(y, mo, d, h, mi)
    start = dt.datetime.combine(_last_sunday(y, 3), dt.time(1, 0)) + dt.timedelta(hours=1)    # 02:00 mistniho -> 03:00
    end = dt.datetime.combine(_last_sunday(y, 10), dt.time(1, 0)) + dt.timedelta(hours=2)     # 01:00 UTC = 03:00 letniho
    off = 2 if start <= naive < end else 1
    return (naive - dt.timedelta(hours=off)).replace(tzinfo=dt.timezone.utc)


def _clean(t):
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(re.sub(r"\s+", " ", t)).strip()


def ch_parse_page(page_html, comp, now=None):
    """HTML stranky se zapasy -> seznam zapasu ve stejnem tvaru jako parse_event.
    Skore pro model je po 60 minutach = soucet prvnich tri tretin (prodlouzeni a najezdy = remiza)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    out = []
    for it in _CH_RE_ITEM.findall(page_html):
        try:
            gid = re.search(r"/game/detail/(\d+)", it).group(1)
            hn = _clean(re.search(r"team-left.*?<h4 class=\"h4\">(.*?)</h4>", it, re.S).group(1))
            an = _clean(re.search(r"team-right.*?<h4 class=\"h4\">(.*?)</h4>", it, re.S).group(1))
            sc = re.findall(r'<h6 class="score[^"]*">(.*?)</h6>', it, re.S)
            m = _CH_RE_DATE.search(it)
            if not m:
                continue
            d, mo, y, h, mi = (int(x) for x in m.groups())
            when = prague_to_utc(y, mo, d, h, mi)
            ot = [_clean(x) for x in re.findall(r'overtime-result[^"]*">(.*?)<', it, re.S)]
            ot = (ot[0] if ot else "").upper()
            info = re.search(r'<div class="game-info">\s*<p>(.*?)</p>', it, re.S)
            per_txt = _clean(info.group(1)) if info else ""
            regpart = per_txt.split(" - ")[0]
            per = re.findall(r"(\d+):(\d+)", regpart)
            has_periods = bool(per) and not _CH_RE_DATE.search(per_txt)
            fh = int(sc[0]) if len(sc) > 0 and sc[0].strip().isdigit() else None
            fa = int(sc[1]) if len(sc) > 1 and sc[1].strip().isdigit() else None
            started = (now - when) > dt.timedelta(minutes=0)
            if not has_periods or fh is None or fa is None:
                state, completed, reg_h, reg_a, txt, det = "pre", False, None, None, "", ""
                if started and (now - when) > dt.timedelta(hours=4) and not has_periods:
                    state, det = "post", "odloženo"          # bez vysledku dlouho po zacatku
            elif (now - when) < dt.timedelta(minutes=140) and not ot:
                state, completed, reg_h, reg_a = "in", False, None, None
                txt, det = f"{fh}:{fa}", "probíhá"
            else:
                state, completed, det = "post", True, "konec"
                if len(per) >= 3:
                    reg_h, reg_a = sum(int(a) for a, _ in per[:3]), sum(int(b) for _, b in per[:3])
                else:
                    reg_h = reg_a = min(fh, fa) if ot else None
                    if not ot:
                        reg_h, reg_a = fh, fa
                if ot and reg_h != reg_a:                    # kdyby soucet nesedel, remiza je jistota
                    reg_h = reg_a = min(fh, fa)
                txt = f"{fh}:{fa}" + (" pp" if ot == "P" else " sn" if ot == "SN" else "")
            out.append({
                "event_id": "extrach_" + gid,
                "date_utc": when.strftime("%Y-%m-%dT%H:%MZ"),
                "season": y if mo >= 7 else y - 1,
                "season_type": 0, "home": hn, "away": an, "home_name": hn, "away_name": an,
                "home_pts": reg_h, "away_pts": reg_a, "neutral": 0, "state": state, "completed": completed,
                "detail": det, "score_txt": txt, "odds": None,
            })
        except (AttributeError, ValueError, IndexError):
            continue
    return out


def _ch_get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (SportBayes; osobni nekomercni projekt)",
        "Accept-Language": "cs"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def ch_fetch_season(comp, pause=CH_PAUSE):
    """Projde strankovani rozpisu soutezi (cca 7 stranek). Kazda stranka se stahuje jednou."""
    seen, out = set(), []
    for page in range(1, CH_MAX_PAGES + 1):
        url = f"{CH_BASE}/competition/games/{comp}"
        if page > 1:
            url += f"?gameListWidget-page={page}&do=gameListWidget-changePage"
        evs = ch_parse_page(_ch_get(url), comp)
        new = [e for e in evs if e["event_id"] not in seen]
        if not new:
            break
        for e in new:
            seen.add(e["event_id"])
            out.append(e)
        time.sleep(pause)
    return out


def _ch_cache_path(lg, comp):
    return os.path.join(DATA_DIR, LEAGUES[lg]["prefix"] + f"comp{comp}.json")


def ch_events(lg, comp=None, force=False):
    """Cela sezona s mezipameti na disku. Obnova: 15 min kdyz se hraje, 30 min pred zacatkem
    zapasu, jinak po 3 hodinach (na GitHubu 2x delsi)."""
    comp = comp or max(LEAGUES[lg]["comps"].values())
    path = _ch_cache_path(lg, comp)
    try:
        with open(path, encoding="utf-8") as f:
            c = json.load(f)
    except (OSError, ValueError):
        c = {"ts": 0, "events": []}
    now = time.time()
    nowd = dt.datetime.now(dt.timezone.utc)
    evs = c["events"]
    live = any(e["state"] != "pre" and not e["completed"] and
               abs((nowd - parse_utc(e["date_utc"])).total_seconds()) < 4 * 3600 for e in evs)
    soon = any(e["state"] == "pre" and 0 <= (parse_utc(e["date_utc"]) - nowd).total_seconds() < 3600 for e in evs)
    slow = 2 if os.environ.get("GITHUB_ACTIONS") else 1
    ttl = (900 if live else 1800 if soon else 3 * 3600) * slow
    if force or not evs or now - c["ts"] > ttl:
        try:
            new = ch_fetch_season(comp)
            if new:
                evs = new
                os.makedirs(DATA_DIR, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"ts": now, "events": evs}, f, ensure_ascii=False)
        except Exception:
            if not evs:
                raise
    return evs


def ch_window(lg, back=1, ahead=7):
    """Zapasy od vcerejska do +7 dni (rozpis dalsich kol)."""
    evs = ch_events(lg)
    today = dt.datetime.now(dt.timezone.utc).date()
    lo, hi = today - dt.timedelta(days=back), today + dt.timedelta(days=ahead)
    return [e for e in evs if lo <= parse_utc(e["date_utc"]).date() <= hi]


def ch_history(lg, progress=None):
    """Dohrane zapasy sezony do cache games.csv; starsi sezony (kdyz jsou v LEAGUES[...]['comps']) jen jednou."""
    rows = load_games(lg)
    have = {r["season"] for r in rows}
    cur = max(LEAGUES[lg]["comps"])
    new = 0
    for season, comp in sorted(LEAGUES[lg]["comps"].items()):
        if season != cur and season in have:
            continue
        evs = ch_events(lg, comp, force=True) if season != cur else ch_events(lg, comp)
        n = merge_finals(rows, evs, lg)
        new += n
        if n:
            save_games(rows, lg)
        if progress:
            progress(dt.date(season, 9, 1), n)
    cnt = {}
    for r in rows:
        cnt[r["season"]] = cnt.get(r["season"], 0) + 1
    for season in LEAGUES[lg].get("hk_seasons", ()):          # starsi sezony z hokej.cz (jednorazove)
        if cnt.get(season, 0) >= 150:                         # sezona uz je stazena celá
            continue
        try:
            evs = hk_season(season)
        except Exception:
            continue
        n = merge_finals(rows, evs, lg)
        new += n
        if n:
            save_games(rows, lg)
        if progress:
            progress(dt.date(season, 9, 1), n)
    return rows, new


# ---------------------------------------------------------------- Extraliga: starsi sezony z hokej.cz
HK_BASE = "https://www.hokej.cz/tipsport-extraliga/zapasy"
HK_COMP = {2026: 7562, 2025: 7397, 2024: 7230, 2023: 7071}      # cislo soutezi "Extraliga" v dane sezone
_HK_MONTHS = {"leden": 1, "unor": 2, "brezen": 3, "duben": 4, "kveten": 5, "cerven": 6, "cervenec": 7,
              "srpen": 8, "zari": 9, "rijen": 10, "listopad": 11, "prosinec": 12}
_HK_TEAMS = [("budejovice", "BANES Motor České Budějovice"), ("liberec", "Bílí Tygři Liberec"),
             ("boleslav", "BK Mladá Boleslav"), ("karlovy", "BYD Energie Karlovy Vary"),
             ("pardubice", "HC DYNAMO PARDUBICE"), ("kometa", "HC KOMETA BRNO"),
             ("trinec", "HC Oceláři Třinec"), ("olomouc", "HC Olomouc"), ("sparta", "HC Sparta Praha"),
             ("plzen", "HC ŠKODA PLZEŇ"), ("litvinov", "HC Litvínov"), ("vitkovice", "HC VÍTKOVICE RIDERA"),
             ("mountfield", "Mountfield HK, a.s."), ("hradec", "Mountfield HK, a.s."),
             ("kladno", "Rytíři Kladno")]


def _ascii(t):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", t.lower()) if unicodedata.category(c) != "Mn")


def canon_team(name):
    """Sjednoti nazvy tymu z hokej.cz s nazvy z ceskyhokej.cz (jinak by model videl dva ruzne tymy)."""
    a = _ascii(name)
    for needle, full in _HK_TEAMS:
        if needle in a:
            return full
    return name.strip()


def hk_parse(page_html, season):
    """Dohrane zapasy zakladni casti z hokej.cz (bez casu zacatku). Skore pro model = soucet prvnich 3 tretin."""
    out, year = [], season
    pos = [(m.start(), "h", m.group(1)) for m in re.finditer(r"<h3>(.*?)</h3>", page_html, re.S)]
    pos += [(m.start(), "r", m) for m in re.finditer(r'<tr data-href="/zapas/(\d+)"[^>]*>(.*?)</tr>', page_html, re.S)]
    for _, kind, v in sorted(pos, key=lambda x: x[0]):
        if kind == "h":
            t = _ascii(_clean(v)).split()
            if len(t) == 2 and t[0] in _HK_MONTHS and t[1].isdigit():
                year = int(t[1])
            continue
        gid, row = v.group(1), v.group(2)
        try:
            names = re.findall(r'preview__name--long">(.*?)</span>', row, re.S)
            sc = re.findall(r'<td class="preview__score[^"]*"><span[^>]*>(\d+)</span>', row)
            dm = re.search(r"(\d{1,2})\.\s*(\d{1,2})\.", re.search(r"match-start-time\">(.*?)<", row, re.S).group(1))
            per_txt = _clean(re.search(r"match-start-time.*?</span>\s*<span>(.*?)</span>", row, re.S).group(1))
            if len(names) < 2 or len(sc) < 2:
                continue
            per = re.findall(r"(\d+):(\d+)", per_txt.split(" - ")[0])
            if len(per) < 3:
                continue
            fh, fa = int(sc[0]), int(sc[1])
            reg_h, reg_a = sum(int(a) for a, _ in per[:3]), sum(int(b) for _, b in per[:3])
            ot = " - " in per_txt
            if ot and reg_h != reg_a:
                reg_h = reg_a = min(fh, fa)
            if not ot and (reg_h, reg_a) != (fh, fa):
                continue                                    # nesedi -> radsi vynechat
            d, mo = int(dm.group(1)), int(dm.group(2))
            when = dt.datetime(year, mo, d, 17, 0, tzinfo=dt.timezone.utc)
            h_, a_ = canon_team(_clean(names[0])), canon_team(_clean(names[1]))
            out.append({"event_id": "extrach_h" + gid, "date_utc": when.strftime("%Y-%m-%dT%H:%MZ"),
                        "season": season, "season_type": 0, "home": h_, "away": a_, "home_name": h_,
                        "away_name": a_, "home_pts": reg_h, "away_pts": reg_a, "neutral": 0, "state": "post",
                        "completed": True, "detail": "konec", "score_txt": f"{fh}:{fa}" + (" pp/sn" if ot else ""),
                        "odds": None})
        except (AttributeError, ValueError, IndexError):
            continue
    return out


def hk_comp_id(season, pause=CH_PAUSE):
    if season in HK_COMP:
        return HK_COMP[season]
    h = _ch_get(f"{HK_BASE}?matchList-filter-season={season}&matchList-filter-competition=0")
    time.sleep(pause)
    sel = re.search(r'<select[^>]*name="competition"[^>]*>(.*?)</select>', h, re.S)
    for val, label in re.findall(r'value="(\d+)"[^>]*>([^<]*)<', sel.group(1) if sel else ""):
        l = _ascii(label)
        if "extraliga" in l and not any(w in l for w in ("play", "baraz", "predkolo", "1.", "2.", "liga junioru")):
            return int(val)
    raise RuntimeError(f"nenalezena soutez extraligy pro sezonu {season}")


def hk_season(season, pause=CH_PAUSE):
    comp = hk_comp_id(season, pause)
    h = _ch_get(f"{HK_BASE}?matchList-filter-season={season}&matchList-filter-competition={comp}"
                f"&matchList-view-round-round=1&do=matchList-view-all")      # vsechna kola, ne jen jedno
    time.sleep(pause)
    return hk_parse(h, season)
