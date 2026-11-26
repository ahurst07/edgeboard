#!/usr/bin/env python3
"""
College basketball ratings model (tempo-free, opponent-adjusted).

For every team it estimates:
  - offense: points scored per 100 possessions vs. an average defense
  - defense: points allowed per 100 possessions vs. an average offense
  - tempo:   possessions per 40 minutes vs. an average opponent
plus a home-court advantage, using ridge regression over every game this season.
Recent games count more, and early in the season each team is pulled toward
last season's rating (regressed toward average) until enough games are played.

Data: free, public box scores from the sportsdataverse hoopR project
(https://github.com/sportsdataverse/hoopR-mbb-data), updated daily.

Output: data/ratings_cbb.json, read by update.py to project every game.

Usage:
  python scripts/ratings.py                   # current season, downloads data
  python scripts/ratings.py --data-dir DIR    # use local parquet files
"""
import argparse
import datetime as dt
import io
import json
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import sparse
from scipy.sparse.linalg import spsolve

RAW = "https://raw.githubusercontent.com/sportsdataverse/hoopR-mbb-data/main/mbb/"

PARAMS = {
    "half_life_days": 45,      # a game 45 days old counts half as much as today's
    "ridge": 6.0,              # how strongly ratings are pulled toward last season's
    "prior_regress": 0.6,      # last season's ratings keep 60% of their distance from average
    "tempo_ridge": 6.0,
    "ft_weight": 0.475,
    "hca_ridge": 1e6,        # possessions = FGA - ORB + TO + 0.475*FTA
}


def season_for(date):
    """ESPN season label: the 2026-27 season is 2027."""
    return date.year + 1 if date.month >= 7 else date.year


def load(kind, season, data_dir=None):
    name = f"{kind}/parquet/{'team_box' if kind == 'team_box' else 'mbb_schedule'}_{season}.parquet"
    if data_dir:
        return pd.read_parquet(Path(data_dir) / name)
    with urllib.request.urlopen(RAW + name, timeout=60) as r:
        return pd.read_parquet(io.BytesIO(r.read()))


def game_rows(season, data_dir=None, before=None):
    """One row per team per completed game: points, possessions, opponent, location."""
    box = load("team_box", season, data_dir)
    sch = load("schedules", season, data_dir)[["game_id", "neutral_site"]]
    box = box.merge(sch, on="game_id", how="left")
    box["game_date"] = pd.to_datetime(box["game_date"])
    if before is not None:
        box = box[box["game_date"] < pd.Timestamp(before)]
    for c in ("field_goals_attempted", "offensive_rebounds", "turnovers", "total_turnovers",
              "free_throws_attempted", "team_score", "opponent_team_score"):
        if c in box:
            box[c] = pd.to_numeric(box[c], errors="coerce")
    to = box["total_turnovers"].fillna(box["turnovers"])
    box["poss"] = (box["field_goals_attempted"] - box["offensive_rebounds"] + to
                   + PARAMS["ft_weight"] * box["free_throws_attempted"])
    # average both teams' possession estimates for the game
    box["poss"] = box.groupby("game_id")["poss"].transform("mean")
    box = box.dropna(subset=["poss", "team_score", "opponent_team_score"])
    box = box[(box["poss"] > 40) & (box["poss"] < 110)]
    neutral = box["neutral_site"].fillna(False).astype(bool)
    box["loc"] = np.where(neutral, 0, np.where(box["team_home_away"] == "home", 1, -1))
    box["team_id"] = box["team_id"].astype(str)
    box["opponent_team_id"] = box["opponent_team_id"].astype(str)
    return box


def fit(rows, asof, prior=None, hca_prior=0.0, hca_ridge=0.01):
    """Ridge regression shrunk toward prior ratings. Returns dict of team ratings."""
    teams = sorted(set(rows["team_id"]) | set(rows["opponent_team_id"]))
    idx = {t: i for i, t in enumerate(teams)}
    n, T = len(rows), len(teams)
    age = (pd.Timestamp(asof) - rows["game_date"]).dt.days.clip(lower=0).to_numpy()
    w = 0.5 ** (age / PARAMS["half_life_days"])

    ti = rows["team_id"].map(idx).to_numpy()
    oi = rows["opponent_team_id"].map(idx).to_numpy()
    loc = rows["loc"].to_numpy(dtype=float)
    r = np.arange(n)

    prior = prior or {}
    def p(t, k):
        v = prior.get(t)
        return 0.0 if v is None else v[k] * PARAMS["prior_regress"]

    # --- efficiency: eff = mu + O[team] + D[opp] + hca*loc
    eff = 100 * rows["team_score"].to_numpy() / rows["poss"].to_numpy()
    mu = np.average(eff, weights=w)
    X = sparse.csr_matrix(
        (np.r_[np.ones(n), np.ones(n), loc], (np.r_[r, r, r], np.r_[ti, T + oi, np.full(n, 2 * T)])),
        shape=(n, 2 * T + 1))
    b0 = np.r_[[p(t, "o") for t in teams], [p(t, "d") for t in teams], hca_prior]
    y = eff - mu - X @ b0
    W = sparse.diags(w)
    lam = np.r_[np.full(2 * T, PARAMS["ridge"]), hca_ridge]
    A = (X.T @ W @ X + sparse.diags(lam)).tocsc()
    delta = spsolve(A, X.T @ (w * y))
    b = b0 + delta
    O, D, hca = b[:T], b[T:2 * T], b[2 * T]
    # center so the average D1-ish team is 0
    O -= O.mean(); D -= D.mean()

    # --- tempo: poss = tmu + P[team] + P[opp]
    poss = rows["poss"].to_numpy()
    tmu = np.average(poss, weights=w)
    Xt = sparse.csr_matrix((np.r_[np.ones(n), np.ones(n)], (np.r_[r, r], np.r_[ti, oi])), shape=(n, T))
    t0 = np.array([p(t, "t") for t in teams])
    yt = poss - tmu - Xt @ t0
    At = (Xt.T @ W @ Xt + sparse.identity(T) * PARAMS["tempo_ridge"]).tocsc()
    P = t0 + spsolve(At, Xt.T @ (w * yt))
    P -= P.mean()

    games = pd.concat([rows["team_id"], rows["opponent_team_id"]]).value_counts()
    names = dict(zip(rows["team_id"], rows["team_display_name"]))
    names.update({k: v for k, v in zip(rows["opponent_team_id"], rows["opponent_team_display_name"])
                  if k not in names})
    teams_out = {}
    for t in teams:
        i = idx[t]
        teams_out[t] = {"name": names.get(t, t), "o": round(float(O[i]), 2), "d": round(float(D[i]), 2),
                        "t": round(float(P[i]), 2), "games": int(games.get(t, 0))}
    return {"mu": round(float(mu), 2), "tempo": round(float(tmu), 2), "hca": round(float(hca), 2),
            "teams": teams_out}


def project(model, home_id, away_id, neutral=False):
    """Projected home margin and total."""
    h, a = model["teams"].get(home_id), model["teams"].get(away_id)
    if not h or not a:
        return None
    loc = 0 if neutral else 1
    poss = model["tempo"] + h["t"] + a["t"]
    h_eff = model["mu"] + h["o"] + a["d"] + model["hca"] * loc
    a_eff = model["mu"] + a["o"] + h["d"] - model["hca"] * loc
    hp, ap = h_eff * poss / 100, a_eff * poss / 100
    return {"margin": hp - ap, "total": hp + ap, "poss": poss}


def d1_only(rows, d1):
    return rows[rows["team_id"].isin(d1) & rows["opponent_team_id"].isin(d1)]


def division1(rows, min_games=20):
    """Division I = teams with a full schedule in the data (non-D1 schools appear a few times)."""
    g = rows.groupby("team_id")["game_id"].nunique()
    return set(g[g >= min_games].index)


def build(season, asof, data_dir=None):
    prior_rows = game_rows(season - 1, data_dir)
    d1 = division1(prior_rows)
    prior_rows = d1_only(prior_rows, d1)
    pm = fit(prior_rows, prior_rows["game_date"].max())
    prior = pm["teams"]
    rows = d1_only(game_rows(season, data_dir, before=asof), d1)
    if rows.empty:
        m = pm
    else:
        # home court is anchored to last season's full-year estimate (November schedules are lopsided)
        m = fit(rows, asof, prior, hca_prior=pm["hca"], hca_ridge=PARAMS["hca_ridge"])
    m["season"] = season
    m["asof"] = str(pd.Timestamp(asof).date())
    m["games_played"] = int(len(rows) // 2)
    return m


def upcoming(season, asof, data_dir=None, days=4):
    """Scheduled games in the next few days, with neutral-site flags (for matching to odds)."""
    sch = load("schedules", season, data_dir)
    sch["gd"] = pd.to_datetime(sch["game_date"])
    start = pd.Timestamp(asof) - pd.Timedelta(days=1)
    sch = sch[(~sch["status_type_completed"].fillna(False).astype(bool))
              & (sch["gd"] >= start) & (sch["gd"] <= start + pd.Timedelta(days=days + 1))]
    return [{"date": str(x.gd.date()), "home_id": str(x.home_id), "away_id": str(x.away_id),
             "home": x.home_display_name, "away": x.away_display_name,
             "neutral": bool(x.neutral_site) if pd.notna(x.neutral_site) else False}
            for x in sch.itertuples()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    args = ap.parse_args()
    today = dt.datetime.now(dt.timezone.utc).date()
    m = build(season_for(today), today, args.data_dir)
    m["upcoming"] = upcoming(season_for(today), today, args.data_dir)
    m["sd_margin"], m["sd_total"] = 11.1, 17.4   # from the 2025-26 backtest
    m["backtest"] = {"games": 1190, "mae_model": 8.8, "mae_market": 8.65, "ats_edge2": "287-242",
                     "ats_edge2_pct": 54.3, "season": "2025-26 (Jan-Mar)"}
    out = Path(args.root) / "data" / "ratings_cbb.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(m, separators=(",", ":")))
    top = sorted(m["teams"].values(), key=lambda t: -(t["o"] - t["d"]))[:10]
    print(f"Ratings through {m['asof']} ({m['games_played']} games). HCA {m['hca']}. Top 10:")
    for t in top:
        print(f"  {t['name']:<32} net {t['o'] - t['d']:+6.1f}  (O {t['o']:+.1f}, D {t['d']:+.1f}, tempo {t['t']:+.1f})")


if __name__ == "__main__":
    main()
