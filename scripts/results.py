"""
Results tracking: locks in every lean, model pick and recommended play the first
time the site shows it, then grades it once the game is final.

Log: data/picks_log.json (kept forever; a season is a few thousand small rows).
Scores: The Odds API /scores endpoint (2 credits per sport per call), only called
when a logged play's game should be over and hasn't been graded yet.
"""
import datetime as dt
import json
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

SCORES_URL = "https://api.the-odds-api.com/v4/sports/{sport}/scores/"
CT = ZoneInfo("America/Chicago")
SPORT_KEYS = {"CFB": "americanfootball_ncaaf", "CBB": "basketball_ncaab"}

CATEGORIES = {
    "model_market": "Model + Market",
    "model_pick": "Model picks",
    "lean_spread": "Spread leans",
    "lean_total": "Total leans",
    "recommended": "Recommended (+EV)",
}


def parse_iso(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def ct_date(iso_time):
    return parse_iso(iso_time).astimezone(CT).date().isoformat()


def load(path):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"entries": {}}


def save(path, log):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(log, separators=(",", ":")))


def category_of_model(q):
    if q["tier"] == "lean":
        return "lean_total" if q["market"] == "totals" else "lean_spread"
    return "model_market" if q["tier"] == "strong" else "model_pick"


def add(log, key, row):
    """First time we see a play, lock it in. Later runs never overwrite it."""
    if key not in log["entries"]:
        log["entries"][key] = {**row, "status": "pending"}


def log_plays(log, model_picks, market_plays, t_run):
    for q in model_picks:
        cat = category_of_model(q)
        key = f'{q["event_id"]}|{q["market"]}|{q["side"]}|{cat}'
        add(log, key, {"event_id": q["event_id"], "sport": q["sport"], "home": q["home"], "away": q["away"],
                       "commence": q["commence"], "date": ct_date(q["commence"]), "category": cat,
                       "market": q["market"], "side": q["side"], "point": q["point"], "odds": q["odds"],
                       "book": q["book"], "edge": q["edge_pts"], "units": q.get("units") or 0,
                       "logged": t_run})
    for p in market_plays:
        if not p.get("tier"):
            continue
        key = f'{p["event_id"]}|{p["market"]}|{p["side"]}|recommended'
        add(log, key, {"event_id": p["event_id"], "sport": p["sport"], "home": p["home"], "away": p["away"],
                       "commence": p["commence"], "date": ct_date(p["commence"]), "category": "recommended",
                       "market": p["market"], "side": p["side"], "point": p["point"], "odds": p["odds"],
                       "book": p["book"], "edge": round(p["ev"] * 100, 1), "units": p.get("units") or 0,
                       "logged": t_run})


def backfill(log, history_events, archive_dir, day):
    """Recover today's plays from line history (for the first run after this feature shipped)."""
    events = list(history_events.items())
    for f in Path(archive_dir).glob("*.jsonl"):
        for line in f.read_text().splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append((r["id"], r))
    for eid, h in events:
        if day is not None and ct_date(h["commence"]) != day:
            continue
        for k, v in h.get("plays_seen", {}).items():
            parts = k.split("|")
            if parts[0] == "model" and len(parts) == 2:
                side = parts[1]
                market = "totals" if side in ("Over", "Under") else "spreads"
                cat = "lean_total" if market == "totals" else "lean_spread"
            elif parts[0] in ("spreads", "totals") and v.get("tier"):
                market, side, cat = parts[0], parts[1], "recommended"
            else:
                continue
            key = f"{eid}|{market}|{side}|{cat}"
            add(log, key, {"event_id": eid, "sport": h["sport"], "home": h["home"], "away": h["away"],
                           "commence": h["commence"], "date": ct_date(h["commence"]), "category": cat,
                           "market": market,
                           "side": side, "point": v.get("point"), "odds": v.get("odds"), "book": v.get("book"),
                           "edge": v.get("edge") if cat != "recommended" else round((v.get("ev") or 0) * 100, 1),
                           "units": 0, "logged": v.get("t"), "backfilled": True})


def fetch_scores(sport_key, api_key):
    params = {"apiKey": api_key, "daysFrom": 3, "dateFormat": "iso"}
    url = SCORES_URL.format(sport=sport_key) + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "edge-site/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
        remaining = r.headers.get("x-requests-remaining")
    return data, (int(float(remaining)) if remaining else None)


def grade_one(e, home_score, away_score):
    if e["point"] is None:
        return None
    if e["market"] == "totals":
        diff = (home_score + away_score - e["point"]) * (1 if e["side"] == "Over" else -1)
    else:
        margin = home_score - away_score if e["side"] == e["home"] else away_score - home_score
        diff = margin + e["point"]
    return "win" if diff > 0 else "loss" if diff < 0 else "push"


def grade(log, api_key, now, meta, after_hours=2.5, sample_scores=None):
    """Grade pending plays whose games should be finished. Returns credits used."""
    due = [e for e in log["entries"].values()
           if e["status"] == "pending" and parse_iso(e["commence"]) < now - dt.timedelta(hours=after_hours)]
    sports = sorted({e["sport"] for e in due})
    for sport in sports:
        try:
            if sample_scores is not None:
                data, remaining = sample_scores(sport), None
            else:
                data, remaining = fetch_scores(SPORT_KEYS[sport], api_key)
                if remaining is not None:
                    meta["credits_remaining"] = remaining
        except Exception as ex:
            print(f"[results] scores error for {sport}: {ex}")
            continue
        finals = {}
        for g in data:
            if not g.get("completed") or not g.get("scores"):
                continue
            sc = {s["name"]: float(s["score"]) for s in g["scores"] if s.get("score") is not None}
            if g["home_team"] in sc and g["away_team"] in sc:
                finals[g["id"]] = (sc[g["home_team"]], sc[g["away_team"]])
        n = 0
        for e in due:
            if e["sport"] == sport and e["event_id"] in finals:
                hs, as_ = finals[e["event_id"]]
                res = grade_one(e, hs, as_)
                if res:
                    e.update(status=res, home_score=hs, away_score=as_)
                    n += 1
        print(f"[results] {sport}: graded {n} plays")
        # games far past start with no final score (postponed/cancelled) are voided after 2 days
        for e in due:
            if e["sport"] == sport and e["status"] == "pending" and \
                    parse_iso(e["commence"]) < now - dt.timedelta(days=2):
                e["status"] = "void"


def profit(e):
    o = e["odds"] or -110
    if e["status"] == "win":
        return o / 100 if o > 0 else 100 / -o
    if e["status"] == "loss":
        return -1.0
    return 0.0


def record(rows):
    w = sum(r["status"] == "win" for r in rows)
    l = sum(r["status"] == "loss" for r in rows)
    p = sum(r["status"] == "push" for r in rows)
    pend = sum(r["status"] == "pending" for r in rows)
    u = round(sum(profit(r) for r in rows if r["status"] in ("win", "loss", "push")), 2)
    return {"w": w, "l": l, "p": p, "pending": pend, "units": u,
            "pct": round(100 * w / (w + l), 1) if w + l else None}


def summary(log, now):
    today = now.astimezone(CT).date()
    days = {"today": today.isoformat(), "yesterday": (today - dt.timedelta(days=1)).isoformat()}
    rows = [e for e in log["entries"].values() if e["status"] != "void"]
    out = {"dates": days, "categories": CATEGORIES, "periods": {}}
    for sport in ("CFB", "CBB"):
        out["periods"][sport] = {}
        for period, filt in (("today", lambda e: e["date"] == days["today"]),
                             ("yesterday", lambda e: e["date"] == days["yesterday"]),
                             ("season", lambda e: True)):
            sel = [e for e in rows if e["sport"] == sport and filt(e)]
            out["periods"][sport][period] = {cat: record([e for e in sel if e["category"] == cat])
                                             for cat in CATEGORIES}
    recent = [e for e in rows if e["date"] in days.values()]
    out["entries"] = sorted(recent, key=lambda e: (e["date"], e["commence"]), reverse=True)
    out["first_date"] = min((e["date"] for e in rows), default=None)
    return out
