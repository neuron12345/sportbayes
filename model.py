# -*- coding: utf-8 -*-
"""
Dynamicka gaussovska Bayesovska sit pro tymove sporty (NBA).

Uzly site:
    sila_tymu[t]   (skryta, vyviji se v case - nahodna prochazka)
    domaci_vyhoda  (skryta, spolecna pro ligu)
    unava_b2b      (skryta, efekt zapasu druhy den po sobe)
    bodovy_rozdil  (pozorovany)  = sila_D - sila_H + domaci_vyhoda
                                   + unava*(b2b_D - b2b_H) + sum
    vyhra_domacich (odvozena)    = bodovy_rozdil > 0

Vsechny uzly jsou gaussovske a vazby linearni, takze presna Bayesovska
inference = Kalmanuv filtr. Zadne MCMC, vysledek je okamzity a obsahuje
i nejistotu odhadu (tym po 3 zapasech ma sirsi rozdeleni nez po 60).
"""
import math
from dataclasses import dataclass, asdict

import numpy as np


@dataclass
class Params:
    sigma_game: float = 11.4   # smerodatna odchylka nahody v jednom zapase (body)
    sigma_prior: float = 6.0   # apriorni nejistota sily tymu (body)
    drift_game: float = 0.08   # rozptyl nahodne prochazky sily na zapas
    season_carry: float = 0.6  # kolik sily se prenasi do dalsi sezony
    season_noise: float = 3.0  # pridana nejistota mezi sezonami (sm. odch.)
    ha_prior: float = 3.0      # apriorni domaci vyhoda (body)
    ha_sd: float = 1.5
    b2b_prior: float = -1.5    # apriorni efekt unavy (body)
    b2b_sd: float = 1.0
    draw_w: float = 0.0        # fotbal: remiza = rozdil v pasmu (-draw_w, +draw_w); 0 = bez remiz
    new_team_mean: float = 0.0  # sila tymu, ktery se objevi nove (napr. postup z nizsi ligy)

    def to_dict(self):
        return asdict(self)


LEAGUE_PARAMS = {
    # vyladeno na skutecnych zapasech (NBA 2003-2010, NHL 2013-2021, fotbal 2016-2020)
    "nba": Params(),
    "soccer": Params(sigma_game=1.555, sigma_prior=0.55, drift_game=0.002, season_carry=0.9,
                     season_noise=0.2, ha_prior=0.225, ha_sd=0.15, b2b_prior=0.0, b2b_sd=0.01,
                     draw_w=0.575, new_team_mean=-0.25),
    # hokej se tremi vysledky po zakladni hraci dobe (remiza = zapas sel do prodlouzeni)
    "hockey3": Params(sigma_game=2.3, sigma_prior=0.6, drift_game=0.001, season_carry=0.7,
                      season_noise=0.15, ha_prior=0.15, ha_sd=0.15, b2b_prior=-0.15, b2b_sd=0.12,
                      draw_w=0.7),
    "nhl": Params(sigma_game=2.35, sigma_prior=0.675, drift_game=0.001, season_carry=0.7,
                  season_noise=0.15, ha_prior=0.125, ha_sd=0.15, b2b_prior=-0.15, b2b_sd=0.12),
}


def default_params(lg="nba"):
    return Params(**LEAGUE_PARAMS[lg].to_dict())


def norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


class BayesNet:
    """Stav = [sila tymu..., domaci_vyhoda, unava_b2b]; presny posterior N(x, P)."""

    def __init__(self, params=None):
        self.p = params or Params()
        self.teams = {}            # zkratka -> index
        self.x = np.zeros(2)
        self.P = np.diag([self.p.ha_sd ** 2, self.p.b2b_sd ** 2])
        self.x[0] = self.p.ha_prior
        self.x[1] = self.p.b2b_prior
        self.last_day = {}         # tym -> ordinal posledniho zapasu
        self.season = None
        self.seasons_seen = 0
        self.n_games = {}

    # --- sprava stavu -------------------------------------------------
    def _idx(self, team):
        if team not in self.teams:
            n = len(self.x)
            # novy tym vlozime pred dva globalni parametry
            k = n - 2
            x = np.zeros(n + 1)
            x[:k] = self.x[:k]
            x[k + 1:] = self.x[k:]
            P = np.zeros((n + 1, n + 1))
            old = list(range(k)) + [k + 1, k + 2]
            P[np.ix_(old, old)] = self.P
            P[k, k] = self.p.sigma_prior ** 2
            if self.seasons_seen > 1:
                x[k] = self.p.new_team_mean
            self.x, self.P = x, P
            self.teams[team] = k
            self.n_games[team] = 0
        return self.teams[team]

    def _ha(self):
        return len(self.x) - 2

    def _b2b(self):
        return len(self.x) - 1

    def new_season(self, season):
        """Regrese k prumeru + pridana nejistota mezi sezonami."""
        gap = 1
        try:                                    # chybejici sezony v datech -> vic nejistoty
            gap = max(1, min(3, int(season) - int(self.season)))
        except (TypeError, ValueError):
            pass
        for _ in range(gap if (self.season is not None and season != self.season) else 0):
            k = len(self.teams)
            if k:
                c = self.p.season_carry
                self.x[:k] *= c
                self.P[:k, :] *= c
                self.P[:, :k] *= c
                self.P[np.arange(k), np.arange(k)] += self.p.season_noise ** 2
            # domaci vyhoda se v case pomalu meni (v NBA dlouhodobe klesa)
            self.P[self._ha(), self._ha()] += (0.23 * self.p.ha_sd) ** 2
            self.last_day = {}
        if season != self.season:
            self.seasons_seen += 1
        self.season = season

    # --- sit: predikce a aktualizace ----------------------------------
    def _design(self, home, away, day):
        ih, ia = self._idx(home), self._idx(away)
        h = np.zeros(len(self.x))
        h[ih], h[ia] = 1.0, -1.0
        h[self._ha()] = 1.0
        b_h = 1.0 if self.last_day.get(home) == day - 1 else 0.0
        b_a = 1.0 if self.last_day.get(away) == day - 1 else 0.0
        h[self._b2b()] = b_h - b_a
        return h, ih, ia, b_h, b_a

    def predict(self, home, away, day, neutral=False):
        """Vrati dict: p_home, ocekavany rozdil, sm. odchylka, b2b priznaky."""
        h, ih, ia, b_h, b_a = self._design(home, away, day)
        if neutral:
            h[self._ha()] = 0.0
        # nejistota naroste o drift od posledniho zapasu (jen pro predikci)
        Ph = self.P @ h
        var_state = float(h @ Ph) + 2 * self.p.drift_game
        mu = float(h @ self.x)
        sd = math.sqrt(var_state + self.p.sigma_game ** 2)
        w = self.p.draw_w
        if w > 0:
            p_home = 1.0 - norm_cdf((w - mu) / sd)
            p_away = norm_cdf((-w - mu) / sd)
        else:
            p_home = norm_cdf(mu / sd)
            p_away = 1.0 - p_home
        return {
            "p_home": p_home,
            "p_away": p_away,
            "p_draw": max(0.0, 1.0 - p_home - p_away),
            "mu": mu,
            "sd": sd,
            "b2b_home": bool(b_h),
            "b2b_away": bool(b_a),
        }

    def update(self, home, away, day, margin, neutral=False):
        """Bayesovska aktualizace po odehranem zapase (margin = domaci - hoste)."""
        h, ih, ia, _, _ = self._design(home, away, day)
        if neutral:
            h[self._ha()] = 0.0
        self.P[ih, ih] += self.p.drift_game
        self.P[ia, ia] += self.p.drift_game
        Ph = self.P @ h
        s = float(h @ Ph) + self.p.sigma_game ** 2
        resid = margin - float(h @ self.x)
        k = Ph / s
        self.x = self.x + k * resid
        self.P = self.P - np.outer(k, Ph)
        self.P = 0.5 * (self.P + self.P.T)
        self.last_day[home] = day
        self.last_day[away] = day
        self.n_games[home] += 1
        self.n_games[away] += 1
        # log-verohodnost pozorovaneho rozdilu (pro ladeni hyperparametru)
        return -0.5 * (math.log(2 * math.pi * s) + resid * resid / s)

    # --- vystupy ------------------------------------------------------
    def ratings(self):
        out = []
        for t, i in self.teams.items():
            out.append((t, float(self.x[i]), math.sqrt(max(self.P[i, i], 0.0)),
                        self.n_games.get(t, 0)))
        out.sort(key=lambda r: -r[1])
        return out

    def globals(self):
        i, j = self._ha(), self._b2b()
        return {
            "home_adv": float(self.x[i]), "home_adv_sd": math.sqrt(self.P[i, i]),
            "b2b": float(self.x[j]), "b2b_sd": math.sqrt(self.P[j, j]),
        }


# ----------------------------------------------------------------------
def run(games, params=None, collect_from=None):
    """
    Projde zapasy chronologicky. Kazda predikce vznikne PRED aktualizaci
    (walk-forward, zadny pohled do budoucnosti).

    games: list dictu {season, day(ordinal), home, away, margin, neutral?}
    Vrati (sit, seznam predikci, soucet log-verohodnosti).
    """
    net = BayesNet(params)
    preds, ll = [], 0.0
    for g in games:
        net.new_season(g["season"])
        neutral = bool(g.get("neutral"))
        pr = net.predict(g["home"], g["away"], g["day"], neutral)
        l = net.update(g["home"], g["away"], g["day"], g["margin"], neutral)
        if collect_from is None or g["season"] >= collect_from:
            ll += l
            preds.append({"event_id": g.get("event_id"), "season": g["season"], "day": g["day"],
                          "home": g["home"], "away": g["away"],
                          "p_home": pr["p_home"], "p_draw": pr["p_draw"], "p_away": pr["p_away"],
                          "mu": pr["mu"], "sd": pr["sd"],
                          "home_win": 1 if g["margin"] > 0 else 0,
                          "draw": 1 if g["margin"] == 0 else 0,
                          "margin": g["margin"]})
    return net, preds, ll


def metrics(preds):
    if not preds:
        return {}
    three = any(r.get("p_draw", 0) > 0 for r in preds)
    ll = br = ok = 0.0
    for r in preds:
        ph, pd = r["p_home"], r.get("p_draw", 0.0)
        pa = r.get("p_away", 1.0 - ph - pd)
        res = 1 if r.get("draw") else (0 if r["home_win"] else 2)
        ps = (ph, pd, pa)
        ll -= math.log(min(max(ps[res], 1e-9), 1.0))
        if three:
            br += sum((ps[i] - (1.0 if i == res else 0.0)) ** 2 for i in range(3))
        else:
            br += (ph - (1.0 if res == 0 else 0.0)) ** 2
        ok += max(range(3), key=lambda i: ps[i]) == res
    n = len(preds)
    y = [r["home_win"] for r in preds]
    return {"n": n, "accuracy": ok / n, "logloss": ll / n, "brier": br / n,
            "home_win_rate": sum(y) / n,
            "draw_rate": sum(1 for r in preds if r.get("draw")) / n, "three_way": three}


def calibration(preds, edges=(0.33, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 1.01)):
    """Kalibrace z pohledu favorita: rekl model 70 %% -> vyhral v 70 %% ?"""
    rows = []
    pf, won = [], []
    for r in preds:
        ph, pd = r["p_home"], r.get("p_draw", 0.0)
        ps = (ph, pd, r.get("p_away", 1.0 - ph - pd))
        res = 1 if r.get("draw") else (0 if r["home_win"] else 2)
        top = max(range(3), key=lambda i: ps[i])
        pf.append(ps[top])
        won.append(1.0 if top == res else 0.0)
    pf, won = np.array(pf), np.array(won)
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (pf >= lo) & (pf < hi)
        if m.sum():
            rows.append({"od": lo, "do": min(hi, 1.0), "n": int(m.sum()),
                         "model": float(pf[m].mean()), "skutecnost": float(won[m].mean())})
    return rows


def tune_draw(games, params, collect_from=None):
    """Fotbal: sirka pasma remizy podle log-loss tri vysledku."""
    if params.draw_w <= 0:
        return params
    _, preds, _ = run(games, params, collect_from)
    best_w, best_ll = params.draw_w, None
    for w in (0.4, 0.45, 0.5, 0.525, 0.55, 0.575, 0.6, 0.65, 0.7, 0.75, 0.8, 0.9):
        ll = 0.0
        for p in preds:
            ph = 1.0 - norm_cdf((w - p["mu"]) / p["sd"])
            pa = norm_cdf((-w - p["mu"]) / p["sd"])
            q = (1.0 - ph - pa) if p["draw"] else (ph if p["home_win"] else pa)
            ll -= math.log(max(q, 1e-9))
        if best_ll is None or ll < best_ll:
            best_w, best_ll = w, ll
    return Params(**{**params.to_dict(), "draw_w": best_w})


def tune(games, base=None, collect_from=None, verbose=False):
    """Empiricky Bayes: maximalizace verohodnosti bodovych rozdilu (coordinate search)."""
    best = base or Params()
    best_ll = run(games, best, collect_from)[2]
    mult = [0.6, 0.75, 0.85, 0.92, 1.0, 1.08, 1.15, 1.3, 1.6]
    ref = base or Params()
    grid = {
        "sigma_game": [round(ref.sigma_game * m, 4) for m in mult],
        "drift_game": [round(ref.drift_game * m, 6) for m in (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0)],
        "season_carry": [0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9],
        "season_noise": [round(ref.season_noise * m, 4) for m in (0.33, 0.5, 0.75, 1.0, 1.25, 1.6)],
        "sigma_prior": [round(ref.sigma_prior * m, 4) for m in (0.66, 0.85, 1.0, 1.2, 1.5)],
        "ha_prior": [round(ref.ha_prior * m, 4) for m in (0.5, 0.75, 1.0, 1.25)],
    }
    for _ in range(2):
        for name, values in grid.items():
            for v in values:
                cand = Params(**{**best.to_dict(), name: v})
                ll = run(games, cand, collect_from)[2]
                if ll > best_ll + 1e-9:
                    best, best_ll = cand, ll
            if verbose:
                print(f"  {name:13s} -> {getattr(best, name):8.4f}   logL={best_ll:.1f}")
    return best, best_ll
