"""
Turns the college basketball ratings (data/ratings_cbb.json, built by ratings.py)
into a projected spread and total for each game on the odds board, and flags
"model picks": spreads where our projection beats the best available line by
at least `model.min_edge` points.
"""
import difflib
import json
import math
import re
from pathlib import Path

OVERRIDES = {  # odds-feed name -> stats-data name, for names the matcher can't work out
    "Army Knights": "Army Black Knights",
}


def norm(name):
    s = name.lower().replace("&", "and")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    toks = s.split()
    # "Kansas St Wildcats" -> "kansas state wildcats" ("St." at the start stays Saint)
    toks = [("state" if t == "st" and i > 0 else t) for i, t in enumerate(toks)]
    return " ".join(toks)


class Model:
    def __init__(self, path):
        self.m = json.loads(Path(path).read_text())
        self.by_name = {t["name"]: tid for tid, t in self.m["teams"].items()}
        self.by_norm = {norm(n): tid for n, tid in self.by_name.items()}
        self._norm_keys = list(self.by_norm)

    def team_id(self, name):
        name = OVERRIDES.get(name, name)
        if name in self.by_name:
            return self.by_name[name]
        n = norm(name)
        if n in self.by_norm:
            return self.by_norm[n]
        close = difflib.get_close_matches(n, self._norm_keys, n=1, cutoff=0.88)
        return self.by_norm[close[0]] if close else None

    def neutral(self, hid, aid):
        for g in self.m.get("upcoming", []):
            if {g["home_id"], g["away_id"]} == {hid, aid}:
                return g["neutral"], g["home_id"] == hid
        return False, True

    def project(self, home_name, away_name):
        hid, aid = self.team_id(home_name), self.team_id(away_name)
        if not hid or not aid:
            return None
        neutral, same_order = self.neutral(hid, aid)
        if not same_order and not neutral:   # stats data lists the other team as host
            hid, aid = aid, hid
            flip = True
        else:
            flip = False
        m, h, a = self.m, self.m["teams"][hid], self.m["teams"][aid]
        loc = 0 if neutral else 1
        poss = m["tempo"] + h["t"] + a["t"]
        hp = (m["mu"] + h["o"] + a["d"] + m["hca"] * loc) * poss / 100
        ap = (m["mu"] + a["o"] + h["d"] - m["hca"] * loc) * poss / 100
        margin = hp - ap
        if flip:
            margin = -margin
        return {"margin": round(margin, 1), "total": round(hp + ap, 1), "neutral": neutral,
                "games": min(h["games"], a["games"])}


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def dec(american):
    return 1 + (american / 100 if american > 0 else 100 / -american)


def evaluate(event, shop, proj, cfg, model, market_plays, trend_fn):
    """Model picks for one game. shop = analyze_event's line-shop dict."""
    mc = cfg["model"]
    rc = cfg["recommend"]
    picks = []
    if not proj or proj["games"] < mc["min_team_games"]:
        return picks
    home, away = event["home_team"], event["away_team"]
    sd = model.m.get("sd_margin", 11.1)
    rows = shop.get("spreads", {})
    for side in (home, away):
        best = None
        for r in rows.get(side, []):
            if r["book"] == "Pinnacle" or r["point"] is None or r["odds"] is None:
                continue
            if not (rc["odds_min"] <= r["odds"] <= rc["odds_max"]):
                continue
            edge = (proj["margin"] + r["point"]) if side == home else (r["point"] - proj["margin"])
            if best is None or edge > best["edge"] or (edge == best["edge"] and r["odds"] > best["odds"]):
                best = {**r, "edge": round(edge, 1)}
        if not best or best["edge"] < mc["min_edge"] or best["edge"] > mc.get("max_edge", 99):
            continue
        p = {"event_id": event["id"], "sport": "CBB", "commence": event["commence_time"],
             "home": home, "away": away, "market": "spreads", "side": side}
        trend = trend_fn(p)
        if trend <= -rc["against_move_block"]:
            continue
        cover = phi(best["edge"] / sd)
        model_line = -proj["margin"] if side == home else proj["margin"]
        why = [f"Model projects {side_short(side)} {fmt_pt(model_line)}; best line is {fmt_pt(best['point'])}"
               f" ({best['edge']:g} pts of value)"]
        agree = next((m for m in market_plays if m["market"] == "spreads" and m["side"] == side), None)
        if agree:
            why.append(f"Market agrees: +{agree['ev'] * 100:.1f}% EV vs. Pinnacle at {agree['book']}")
        if trend >= rc["with_move_boost"]:
            why.append(f"Sharp line moved {trend:g} pts this way")
        if proj["neutral"]:
            why.append("Neutral-site game (no home-court edge)")
        picks.append({**p, "book": best["book"], "point": best["point"], "odds": best["odds"],
                      "edge_pts": best["edge"], "model_line": round(model_line, 1),
                      "cover_prob": round(cover, 3), "ev": round(cover * dec(best["odds"]) - 1, 4),
                      "tier": "strong" if agree or trend >= rc["with_move_boost"] else "model",
                      "units": 1.0 if agree else 0.5, "why": why, "trend": trend})
    return picks


def side_short(t):
    return t


def fmt_pt(p):
    return "PK" if p == 0 else (f"+{p:g}" if p > 0 else f"{p:g}")
