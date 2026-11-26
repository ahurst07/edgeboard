#!/usr/bin/env python3
"""
Edge finder for college football + college basketball.

Each run:
  1. Pulls current lines from The Odds API (or fake lines with --sample).
  2. Builds a "sharp" reference line per game (Pinnacle no-vig, or a
     consensus of books if Pinnacle is missing).
  3. Flags +EV plays: a book price that beats the fair (no-vig) price.
  4. Saves a snapshot of the sharp line so we can track movement from the
     first line we captured ("open") to now.
  5. Writes site/data/latest.json for the web page, keeps data/history.json
     for line history, and archives finished games to data/archive/.

Usage:
  ODDS_API_KEY=xxxx python scripts/update.py          # live
  python scripts/update.py --sample                   # fake data for testing
"""
import argparse
import datetime as dt
import json
import os
import random
import statistics
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_plays  # noqa: E402
import results  # noqa: E402

API_URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds/"

BOOK_NAMES = {
    "pinnacle": "Pinnacle", "draftkings": "DraftKings", "fanduel": "FanDuel",
    "betmgm": "BetMGM", "espnbet": "ESPN BET", "betrivers": "BetRivers",
    "hardrockbet": "Hard Rock", "bovada": "Bovada", "betonlineag": "BetOnline",
    "lowvig": "LowVig", "williamhill_us": "Caesars", "fanatics": "Fanatics",
}


# ----------------------------------------------------------------- helpers
def now_utc():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def dec_to_american(d):
    if d is None or d <= 1:
        return None
    return round((d - 1) * 100) if d >= 2 else round(-100 / (d - 1))


def prob_to_american(p):
    if p <= 0 or p >= 1:
        return None
    return dec_to_american(1 / p)


def devig(d1, d2):
    """Multiplicative no-vig: returns fair probabilities for a two-way market."""
    i1, i2 = 1 / d1, 1 / d2
    s = i1 + i2
    return i1 / s, i2 / s


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj, pretty=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1 if pretty else None, separators=None if pretty else (",", ":")))
    tmp.replace(path)


# ------------------------------------------------------------------ fetch
def fetch_live(sport_key, sport_cfg, cfg, api_key):
    t0 = now_utc()
    params = {
        "apiKey": api_key,
        "bookmakers": ",".join(cfg["bookmakers"]),  # each group of 10 books = 1 region of cost
        "markets": ",".join(cfg["markets"]),
        "oddsFormat": "decimal",
        "dateFormat": "iso",
        "commenceTimeFrom": iso(t0),
        "commenceTimeTo": iso(t0 + dt.timedelta(days=sport_cfg.get("days_ahead", 3))),
    }
    url = API_URL.format(sport=sport_key) + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "edge-site/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
        remaining = resp.headers.get("x-requests-remaining")
        last_cost = resp.headers.get("x-requests-last")
    return data, (int(float(remaining)) if remaining else None), last_cost


def fetch_sample(sport_key, cfg, run_index):
    """Fake but realistic-looking data. Lines drift between runs so movement shows up."""
    rng = random.Random(f"{sport_key}-games")
    teams = {
        "americanfootball_ncaaf": ["Iowa Hawkeyes", "Iowa State Cyclones", "Nebraska Cornhuskers",
                                   "Wisconsin Badgers", "Kansas State Wildcats", "Minnesota Golden Gophers",
                                   "Oklahoma State Cowboys", "Northwestern Wildcats", "Kansas Jayhawks",
                                   "Missouri Tigers"],
        "basketball_ncaab": ["Drake Bulldogs", "Iowa State Cyclones", "Iowa Hawkeyes", "Creighton Bluejays",
                             "Kansas Jayhawks", "Houston Cougars", "Purdue Boilermakers", "Marquette Golden Eagles",
                             "Northern Iowa Panthers", "Illinois Fighting Illini", "Baylor Bears", "UConn Huskies"],
    }[sport_key]
    rng.shuffle(teams)
    base_total = 47 if "football" in sport_key else 141
    start = now_utc().replace(minute=0, second=0) + dt.timedelta(hours=20)
    events = []
    for g in range(len(teams) // 2):
        home, away = teams[2 * g], teams[2 * g + 1]
        grng = random.Random(f"{sport_key}-{g}")
        open_spread = grng.choice([-1, 1]) * grng.choice([1.5, 2.5, 3, 4.5, 6.5, 7, 9.5, 12.5])
        drift = grng.choice([0, 0, 0.5, -0.5, 1, -1])  # per run, so some games steam
        open_total = base_total + grng.choice([-6.5, -3, -1.5, 0.5, 2, 4.5, 7])
        tdrift = grng.choice([0, 0, 0.5, -0.5, 1])
        sharp_spread = open_spread + drift * run_index
        sharp_total = open_total + tdrift * run_index
        bms = []
        brng = random.Random(f"{sport_key}-{g}-{run_index}")
        for bk in cfg["bookmakers"]:
            if bk != "pinnacle" and brng.random() < 0.12:
                continue  # some books don't post every game
            lag = 0 if bk == "pinnacle" else brng.choice([0, 0, 0, 0.5, -0.5, 1, -1])
            sp = sharp_spread + lag
            tp = sharp_total + brng.choice([0, 0, 0, 0.5, -0.5, 1])
            vig = 1.035 if bk == "pinnacle" else (1.03 if bk == "lowvig" else 1.048)
            skew = brng.choice([0, 0, 0, 0.01, -0.01, 0.02, -0.02])
            ph, pa = (0.5 + skew) * vig, (0.5 - skew) * vig
            po, pu = (0.5 - skew / 2) * vig, (0.5 + skew / 2) * vig
            mk = []
            if "spreads" in cfg["markets"]:
                mk.append({"key": "spreads", "outcomes": [
                    {"name": home, "price": round(1 / ph, 3), "point": sp},
                    {"name": away, "price": round(1 / pa, 3), "point": -sp}]})
            if "totals" in cfg["markets"]:
                mk.append({"key": "totals", "outcomes": [
                    {"name": "Over", "price": round(1 / po, 3), "point": tp},
                    {"name": "Under", "price": round(1 / pu, 3), "point": tp}]})
            if "h2h" in cfg["markets"]:
                fav_p = min(0.95, 0.5 + abs(sp) * 0.03)
                hp = fav_p if sp < 0 else 1 - fav_p
                mk.append({"key": "h2h", "outcomes": [
                    {"name": home, "price": round(1 / (hp * vig), 3)},
                    {"name": away, "price": round(1 / ((1 - hp) * vig), 3)}]})
            bms.append({"key": bk, "title": BOOK_NAMES.get(bk, bk), "markets": mk})
        events.append({
            "id": f"sample-{sport_key[:4]}-{g}", "sport_key": sport_key,
            "commence_time": iso(start + dt.timedelta(hours=3 * g)),
            "home_team": home, "away_team": away, "bookmakers": bms,
        })
    return events


# -------------------------------------------------------- sharp reference
def two_way(event, book, market_key):
    """Return a dict for one book/market: {sides:{name:(point, decimal)}}"""
    for m in book.get("markets", []):
        if m["key"] != market_key:
            continue
        outs = m.get("outcomes", [])
        if len(outs) != 2:
            return None
        return {o["name"]: (o.get("point"), o["price"]) for o in outs}
    return None


def side_names(event, market_key):
    if market_key == "totals":
        return ["Over", "Under"]
    return [event["home_team"], event["away_team"]]


def sharp_reference(event, market_key, cfg):
    """Fair probabilities + the line they apply to. Pinnacle first, consensus fallback."""
    a, b = side_names(event, market_key)
    sharp_key = cfg["sharp_book"]
    for bk in event["bookmakers"]:
        if bk["key"] == sharp_key:
            m = two_way(event, bk, market_key)
            if m and a in m and b in m:
                pa, pb = devig(m[a][1], m[b][1])
                return {"src": BOOK_NAMES.get(sharp_key, sharp_key), "point": {a: m[a][0], b: m[b][0]},
                        "p": {a: pa, b: pb}}
    # consensus: most common line among books, median no-vig probability at that line
    rows = []
    for bk in event["bookmakers"]:
        m = two_way(event, bk, market_key)
        if m and a in m and b in m:
            pa, pb = devig(m[a][1], m[b][1])
            rows.append((m[a][0], m[b][0], pa))
    if len(rows) < cfg["min_books_for_consensus"]:
        return None
    if market_key == "h2h":
        pa = statistics.median(r[2] for r in rows)
        return {"src": f"Consensus ({len(rows)})", "point": {a: None, b: None}, "p": {a: pa, b: 1 - pa}}
    modal = statistics.mode(r[0] for r in rows)
    at_line = [r for r in rows if r[0] == modal]
    if len(at_line) < 3:
        return None
    pa = statistics.median(r[2] for r in at_line)
    return {"src": f"Consensus ({len(at_line)})", "point": {a: modal, b: at_line[0][1]},
            "p": {a: pa, b: 1 - pa}}


def crosses_key(p1, p2, keys):
    lo, hi = sorted((abs(p1), abs(p2)))
    if (p1 < 0) != (p2 < 0) and p1 != 0 and p2 != 0:
        return True  # line flipped through pick'em
    return any(lo <= k <= hi for k in keys)


def adjusted_prob(market_key, side, ref, book_point, sport_cfg, cfg):
    """Fair win prob at the book's line. Exact if same number, approximate if off by <= max_off_number."""
    p = ref["p"][side]
    if market_key == "h2h":
        return p, False
    sp = ref["point"][side]
    if sp is None or book_point is None:
        return None, False
    if book_point == sp:
        return p, False
    if market_key == "spreads":
        delta = book_point - sp  # more points = better for that side
        slope = sport_cfg["spread_prob_per_point"]
        if crosses_key(sp, book_point, sport_cfg.get("key_numbers", [])):
            return None, False
    else:
        delta = (sp - book_point) if side == "Over" else (book_point - sp)
        slope = sport_cfg["total_prob_per_point"]
    if abs(book_point - sp) > cfg["max_off_number"]:
        return None, False
    return min(0.97, max(0.03, p + delta * slope)), True


# --------------------------------------------------------------- analysis
def analyze_event(event, sport_key, sport_cfg, cfg):
    plays, shop = [], {}
    refs = {}
    for mkey in cfg["markets"]:
        ref = sharp_reference(event, mkey, cfg)
        refs[mkey] = ref
        a, b = side_names(event, mkey)
        shop[mkey] = {a: [], b: []}
        best_for_side = {}
        for bk in event["bookmakers"]:
            m = two_way(event, bk, mkey)
            if not m:
                continue
            for side in (a, b):
                if side not in m:
                    continue
                point, dec = m[side]
                shop[mkey][side].append({"book": BOOK_NAMES.get(bk["key"], bk["key"]), "point": point,
                                         "odds": dec_to_american(dec)})
                if ref is None or bk["key"] == cfg["sharp_book"]:
                    continue
                p, approx = adjusted_prob(mkey, side, ref, point, sport_cfg, cfg)
                if p is None:
                    continue
                ev = p * dec - 1
                if ev >= cfg["min_ev"]:
                    cand = {"book": BOOK_NAMES.get(bk["key"], bk["key"]), "point": point,
                            "odds": dec_to_american(dec), "ev": round(ev, 4), "fair_prob": round(p, 4),
                            "fair_odds": prob_to_american(p), "approx": approx}
                    cur = best_for_side.get(side)
                    if cur is None:
                        best_for_side[side] = {**cand, "others": []}
                    elif ev > cur["ev"]:
                        best_for_side[side] = {**cand, "others": [cur["book"]] + cur["others"]}
                    else:
                        cur["others"].append(cand["book"])
        for side, play in best_for_side.items():
            plays.append({
                "event_id": event["id"], "sport": sport_cfg["label"], "commence": event["commence_time"],
                "home": event["home_team"], "away": event["away_team"], "market": mkey, "side": side,
                "sharp_src": ref["src"], "sharp_point": ref["point"].get(side), **play,
            })
        # mark best price per side for the line-shop view
        for side in (a, b):
            rows = shop[mkey][side]
            if not rows:
                continue
            mid = statistics.median((r["point"] or 0) for r in rows)

            def value(r, mkey=mkey, side=side, mid=mid):
                # rough expected return vs. the middle-of-market number
                o = r["odds"]
                d = 1 + (o / 100 if o > 0 else 100 / -o)
                diff = (r["point"] or 0) - mid
                if mkey == "spreads":
                    adj = diff * sport_cfg["spread_prob_per_point"]
                elif mkey == "totals":
                    adj = (-diff if side == "Over" else diff) * sport_cfg["total_prob_per_point"]
                else:
                    adj = 0
                return (0.5 + adj) * d
            best = max(rows, key=value)
            best["best"] = True
            rows.sort(key=lambda r: -value(r))
    return plays, shop, refs


def snapshot_from_refs(refs, event):
    snap = {}
    s = refs.get("spreads")
    if s and s["point"].get(event["home_team"]) is not None:
        snap["spread"] = s["point"][event["home_team"]]
        snap["spread_src"] = s["src"]
    t = refs.get("totals")
    if t and t["point"].get("Over") is not None:
        snap["total"] = t["point"]["Over"]
    h = refs.get("h2h")
    if h:
        snap["ml_home_p"] = round(h["p"][event["home_team"]], 4)
    return snap


def movement(hist_event, sport_cfg):
    snaps = [s for s in hist_event["snapshots"]]
    out = {}
    for key, alert in (("spread", sport_cfg["spread_move_alert"]), ("total", sport_cfg["total_move_alert"])):
        vals = [(s["t"], s[key]) for s in snaps if s.get(key) is not None]
        if not vals:
            continue
        (t_open, v_open), (t_now, v_now) = vals[0], vals[-1]
        prev = vals[-2][1] if len(vals) > 1 else v_now
        move = round(v_now - v_open, 1)
        recent = round(v_now - prev, 1)
        flags = []
        if abs(move) >= alert:
            flags.append("big")
        if abs(recent) >= max(1.0, alert * 0.75):
            flags.append("steam")
        if key == "spread" and sport_cfg.get("key_numbers") and v_open != v_now:
            if any(min(abs(v_open), abs(v_now)) < k < max(abs(v_open), abs(v_now))
                   for k in sport_cfg["key_numbers"][:2]):
                flags.append("key")
        out[key] = {"open": v_open, "open_t": t_open, "now": v_now, "move": move, "recent": recent,
                    "flags": flags, "series": [v for _, v in vals][-40:]}
    return out


# --------------------------------------------------------- recommendation
def line_trend(p, hist_event):
    """How far the sharp line has moved toward (+) or against (-) this play since we first saw it."""
    key = "spread" if p["market"] == "spreads" else "total"
    vals = [s[key] for s in hist_event["snapshots"] if s.get(key) is not None]
    if len(vals) < 2:
        return 0.0
    delta = vals[-1] - vals[0]
    if key == "spread":   # spread stored from the home team's view; more negative = home more favored
        return -delta if p["side"] == p["home"] else delta
    return delta if p["side"] == "Over" else -delta


def recommend(p, hist_event, cfg):
    """Adds tier ('strong' | 'rec' | None), units and the reasons to a play."""
    r = cfg["recommend"]
    p["tier"], p["units"], p["why"] = None, 0, []
    if p["market"] not in ("spreads", "totals"):
        return
    trend = round(line_trend(p, hist_event), 1)
    p["trend"] = trend
    medge = p.get("model_edge")
    mcfg = cfg.get("model", {})
    model_ok = medge is not None and mcfg.get("min_edge", 99) <= medge <= mcfg.get("max_edge", 99)
    if p["ev"] < r["min_ev"] and not model_ok:
        return
    if r["require_exact_number"] and p["approx"] and trend < r["with_move_boost"] and not model_ok:
        return  # off-number plays only count when the book is lagging a sharp move (or the model agrees)
    if r["require_sharp_book"] and p["sharp_src"] != BOOK_NAMES.get(cfg["sharp_book"]):
        return
    if not (r["odds_min"] <= p["odds"] <= r["odds_max"]):
        return
    if trend <= -r["against_move_block"]:
        return  # the sharp market is moving away from this side, so skip it
    why = [f'+{p["ev"] * 100:.1f}% vs. {p["sharp_src"]} no-vig line']
    strong = p["ev"] >= r["strong_ev"]
    if model_ok:
        why.append(f"Our model agrees: projects {model_plays.fmt_pt(p['model_line'])} ({medge:g} pts of value)")
        strong = True
    elif medge is not None and medge < 0:
        why.append(f"Note: our model leans the other way ({model_plays.fmt_pt(p['model_line'])})")
    if trend >= r["with_move_boost"]:
        why.append(f"Sharp line moved {trend:g} pt{'s' if trend != 1 else ''} this way; {p['book']} hasn't caught up"
                   if p["approx"] else f"Sharp line moved {trend:g} pt{'s' if trend != 1 else ''} this way since we first saw it")
        strong = strong or p["ev"] >= r["min_ev"]
    if p.get("others"):
        n = len(p["others"])
        why.append(f"Also +EV at {n} other book{'s' if n > 1 else ''}")
    # quarter Kelly, 1 unit = 1% of bankroll
    dec = 1 + (p["odds"] / 100 if p["odds"] > 0 else 100 / -p["odds"])
    kelly = p["ev"] / (dec - 1)
    units = round(kelly * r["kelly_fraction"] * 100 * 2) / 2
    p["units"] = max(0.5, min(r["max_units"], units))
    p["tier"] = "strong" if strong else "rec"
    p["why"] = why


# ------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true", help="use fake data instead of the live API")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    args = ap.parse_args()

    root = Path(args.root)
    cfg = json.loads((root / "config.json").read_text())
    hist_path = root / "data" / "history.json"
    out_path = root / "site" / "data" / "latest.json"
    history = load_json(hist_path, {"meta": {}, "events": {}})
    meta = history.setdefault("meta", {})
    t_run = now_utc()

    api_key = os.environ.get("ODDS_API_KEY", "").strip()
    if not args.sample and not api_key:
        sys.exit("ODDS_API_KEY is not set. Add it as a GitHub secret (see README) or use --sample.")

    run_index = meta.get("sample_runs", 0)
    model = None
    mpath = root / "data" / "ratings_cbb.json"
    if mpath.exists() and "model" in cfg:
        try:
            model = model_plays.Model(mpath)
        except Exception as e:
            print(f"[model] could not load ratings: {e}")
    model_picks, unmatched = [], set()
    run_markets = cfg["markets"]
    run_cfg = cfg
    all_plays, games = [], []
    seen_ids = set()
    for sport_key, sport_cfg in cfg["sports"].items():
        if args.sample:
            events, remaining = fetch_sample(sport_key, run_cfg, run_index), None
        else:
            known = meta.get("credits_remaining")
            cost = len(run_markets) * -(-len(cfg["bookmakers"]) // 10)
            if known is not None and known - cost < cfg["min_credits_reserve"]:
                print(f"[skip] {sport_key}: only {known} credits left (reserve {cfg['min_credits_reserve']})")
                continue
            try:
                events, remaining, last_cost = fetch_live(sport_key, sport_cfg, run_cfg, api_key)
            except Exception as e:  # keep going with the other sport
                print(f"[error] {sport_key}: {e}")
                continue
            if remaining is not None:
                meta["credits_remaining"] = remaining
            print(f"[ok] {sport_key}: {len(events)} games, cost {last_cost}, credits left {remaining}")

        for ev in events:
            seen_ids.add(ev["id"])
            plays, shop, refs = analyze_event(ev, sport_key, sport_cfg, run_cfg)
            all_plays.extend(plays)
            h = history["events"].setdefault(ev["id"], {
                "sport": sport_cfg["label"], "sport_key": sport_key, "home": ev["home_team"],
                "away": ev["away_team"], "commence": ev["commence_time"], "snapshots": [], "plays_seen": {}})
            h["commence"] = ev["commence_time"]
            snap = snapshot_from_refs(refs, ev)
            if snap:
                snap["t"] = iso(t_run)
                h["snapshots"].append(snap)
                h["snapshots"] = h["snapshots"][-cfg["max_snapshots_per_game"]:]
            proj = None
            if model and sport_key == "basketball_ncaab":
                proj = model.project(ev["home_team"], ev["away_team"])
                if proj is None:
                    unmatched.update(n for n in (ev["home_team"], ev["away_team"]) if not model.team_id(n))
                elif proj["games"] >= cfg["model"].get("min_team_games", 0):
                    for p in plays:
                        if p["market"] == "spreads" and p["point"] is not None:
                            ml = -proj["margin"] if p["side"] == ev["home_team"] else proj["margin"]
                            p["model_line"] = round(ml, 1)
                            p["model_edge"] = round(p["point"] - ml, 1)
            for p in plays:  # log every play we ever flagged, for grading later
                k = f'{p["market"]}|{p["side"]}'
                prev = h["plays_seen"].get(k)
                if prev is None or p["ev"] > prev["ev"]:
                    h["plays_seen"][k] = {"book": p["book"], "point": p["point"], "odds": p["odds"],
                                          "ev": p["ev"], "t": iso(t_run)}
                recommend(p, h, cfg)
                if p.get("tier"):
                    h["plays_seen"][k]["tier"] = p["tier"]
            if proj:
                picks = model_plays.evaluate(ev, shop, proj, cfg, model, plays, lambda q, h=h: line_trend(q, h))
                model_picks.extend(picks)
                for q in picks:
                    h["plays_seen"][f'model|{q["side"]}'] = {"book": q["book"], "point": q["point"],
                                                              "odds": q["odds"], "edge": q["edge_pts"],
                                                              "t": iso(t_run)}
            games.append({"id": ev["id"], "sport": sport_cfg["label"], "commence": ev["commence_time"],
                          "model": proj,
                          "home": ev["home_team"], "away": ev["away_team"], "shop": shop,
                          "move": movement(h, sport_cfg),
                          "sharp": {k: (v["src"] if v else None) for k, v in refs.items()}})

    # archive games that have started
    cutoff = t_run - dt.timedelta(hours=cfg["archive_after_hours"])
    for eid in list(history["events"]):
        h = history["events"][eid]
        if parse_iso(h["commence"]) < cutoff:
            pre = [s for s in h["snapshots"] if parse_iso(s["t"]) <= parse_iso(h["commence"])]
            rec = {"id": eid, **{k: h[k] for k in ("sport", "home", "away", "commence")},
                   "open": pre[0] if pre else None, "close": pre[-1] if pre else None,
                   "plays_seen": h["plays_seen"]}
            arch = root / "data" / "archive" / f'{h["commence"][:7]}.jsonl'
            arch.parent.mkdir(parents=True, exist_ok=True)
            with arch.open("a") as f:
                f.write(json.dumps(rec) + "\n")
            del history["events"][eid]

    # results: lock in today's plays, grade finished games
    log_path = root / "data" / "picks_log.json"
    log = results.load(log_path)
    if not log.get("backfilled"):
        results.backfill(log, history["events"], root / "data" / "archive", None)  # everything since launch
        log["backfilled"] = True
    results.log_plays(log, model_picks, all_plays, iso(t_run))
    try:
        if args.sample:
            import random as _r
            results.grade(log, None, t_run, meta, sample_scores=lambda sport: [
                {"id": e["event_id"], "completed": True, "home_team": e["home"], "away_team": e["away"],
                 "scores": [{"name": e["home"], "score": str(_r.randint(55, 90))},
                            {"name": e["away"], "score": str(_r.randint(55, 90))}]}
                for e in log["entries"].values()])
        else:
            results.grade(log, api_key, t_run, meta)
    except Exception as e:
        print(f"[results] grading failed: {e}")
    results.save(log_path, log)

    if args.sample:
        meta["sample_runs"] = run_index + 1
    meta["last_run"] = iso(t_run)
    save_json(hist_path, history)

    rank = {"strong": 0, "rec": 1}
    all_plays.sort(key=lambda p: (rank.get(p.get("tier"), 2), -p["ev"]))
    games.sort(key=lambda g: g["commence"])
    moves = []
    for g in games:
        for key, m in g["move"].items():
            if m["move"] != 0 or m["flags"]:
                moves.append({"id": g["id"], "sport": g["sport"], "commence": g["commence"],
                              "home": g["home"], "away": g["away"], "market": key, **m})
    moves.sort(key=lambda m: (-len(m["flags"]), -abs(m["move"])))
    save_json(out_path, {
        "updated": iso(t_run), "sample": args.sample,
        "credits_remaining": meta.get("credits_remaining"),
        "settings": {"min_ev": cfg["min_ev"], "sharp_book": BOOK_NAMES.get(cfg["sharp_book"]),
                     "markets": cfg["markets"]},
        "plays": all_plays, "moves": moves, "games": games,
        "model_picks": sorted(model_picks, key=lambda q: ({"strong": 0, "model": 1}.get(q["tier"], 2),
                                                          q["market"] != "spreads", -q["edge_pts"])),
        "results": results.summary(log, t_run),
        "model_info": ({**{k: model.m.get(k) for k in ("asof", "games_played", "hca", "backtest")},
                        "min_games": cfg["model"].get("min_team_games", 0),
                        "typical_games": int(statistics.median([t["games"] for t in model.m["teams"].values()]))}
                       if model else None),
    })
    if unmatched:
        print(f"[model] no rating match for: {sorted(unmatched)}")
    print(f"Done: {len(games)} games, {len(all_plays)} +EV plays, {len(moves)} line moves -> {out_path}")


if __name__ == "__main__":
    main()
