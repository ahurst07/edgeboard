# Hurst Strategy: CFB + CBB +EV plays and line movement

A free, public site that:
- **Finds +EV plays.** It takes the vig off Pinnacle's line (the sharpest book) to get a fair price, then flags any sportsbook paying more than that. If Pinnacle has no line, it uses a consensus of books instead.
- **Highlights recommended plays.** Spreads and totals that pass a stricter filter: at least +2.5% EV against Pinnacle's no-vig line, a normal price (−150 to +150), and Pinnacle's line not moving against the play. Each one shows a bet size and the reasons it was picked. **STRONG PLAY** = +4% or more, or Pinnacle's line has moved toward the play while the book hasn't caught up (a "stale number"). Bet sizes use quarter-Kelly: 1 unit = 1% of bankroll, 2 units max.
- **Model picks (college basketball).** `scripts/ratings.py` builds our own team ratings every run from free public box scores (sportsdataverse hoopR data): opponent-adjusted offense, defense and pace, plus home court, weighted toward recent games and anchored to last season early on. `scripts/model_plays.py` projects each game and flags spreads where the best line beats our number by 2-6 points. Tested on 1,190 games of 2025-26 against DraftKings closing lines: average miss 8.8 pts vs. 8.65 for the market; 287-242 (54.3%) ATS with 2+ pts of edge. Strongest when the market-based edge agrees ("MODEL + MARKET").
- **Tracks line movement.** It saves the sharp line on every update and shows the move from the first line it captured ("open") to now. Moves get labeled BIG MOVE, STEAM (a quick move since the last update) and KEY NUMBER (crossing 3 or 7 in football).
- **Line shop.** It shows every book's price for every game, with the best number highlighted.

It runs entirely on free services. GitHub Actions pulls odds four times a day, saves the history in the repo, and publishes the page on GitHub Pages. Your domain then points at that page.

```
config.json                     <- settings (books, thresholds, markets)
scripts/update.py               <- pulls odds, finds edges, tracks moves
site/index.html                 <- the web page
.github/workflows/update.yml    <- the schedule (4x/day) + publishing
data/history.json               <- line history (created automatically)
data/archive/YYYY-MM.jsonl      <- finished games: open, close, plays flagged
```

---

## Setup (about 20 minutes, no coding)

### 1. Get a free odds API key
Sign up at **the-odds-api.com** and pick the free plan (500 credits/month). Copy your API key.

### 2. Create a GitHub repository
1. Make a free account at **github.com** if you don't have one.
2. Click **New repository**. Name it something like `edgeboard` and set it to **Public** (Pages is free for public repos). Then click **Create**.
3. Click **uploading an existing file** and drag in `README.md`, `config.json`, and the `scripts` and `site` folders. Commit.
4. The workflow file sits in a hidden folder, so add it by hand: **Add file → Create new file**. Type the name `.github/workflows/update.yml`, paste in the contents of that file, and commit.

### 3. Add your API key as a secret
In the repo, go to **Settings → Secrets and variables → Actions → New repository secret**.
- Name: `ODDS_API_KEY`
- Value: your key

The key stays private. It never appears on the website.

### 4. Turn on GitHub Pages
Go to **Settings → Pages → Build and deployment → Source** and choose **GitHub Actions**.

### 5. Run it once
Go to **Actions → "Update odds and publish site" → Run workflow**. After a minute or two the site will be live at `https://YOUR-USERNAME.github.io/edgeboard/`. From then on it updates automatically at 8am, 12pm, 4pm and 8pm Central.

### 6. Point your domain at it
Your Google Domains domain is now managed in **Squarespace Domains** (account.squarespace.com/domains).

**Option A: use a subdomain (keeps whatever is on your main site).** Example: `picks.yourdomain.com`
- Squarespace Domains → your domain → **DNS → Custom records** → add **CNAME**, host `picks`, data `YOUR-USERNAME.github.io`
- GitHub → **Settings → Pages → Custom domain** → enter `picks.yourdomain.com` → Save → check **Enforce HTTPS** once it's available.

**Option B: use the main domain.** Since the current site is unused, it can be replaced.
- In Squarespace DNS, delete the old records that point to Google. If you built the site with Google Sites, look for a `www` CNAME to `ghs.googlehosted.com` or similar.
- Add four **A** records with host `@`, pointing to: `185.199.108.153`, `185.199.109.153`, `185.199.110.153`, `185.199.111.153`
- Add a **CNAME** with host `www`, data `YOUR-USERNAME.github.io`
- GitHub → Settings → Pages → Custom domain → `yourdomain.com` → Save → **Enforce HTTPS**.

DNS changes can take up to a day to spread.

---

## How the free plan budget works

The Odds API charges **1 credit per market per sport per call** when you request 10 or fewer bookmakers. Calls that come back empty (sport out of season) are free.

| Setup | Credits per update | 4 updates/day for a month |
|---|---|---|
| CFB + CBB, spreads + totals (default) | 4 | ~480 |
| CBB only (after bowl season), spreads + totals | 2 | ~240 |

When credits run low, the script stops before it hits zero (`min_credits_reserve`). After bowl season you have room to add more update times to the workflow file.

## Settings you might change (`config.json`)

| Setting | What it does |
|---|---|
| `min_ev` | Minimum edge to show a play (0.015 = 1.5%) |
| `recommend.min_ev` / `strong_ev` | Edge needed for RECOMMENDED (2.5%) and STRONG (4%) |
| `recommend.against_move_block` | Skip a play if Pinnacle's line moved this many points against it (1.0) |
| `recommend.with_move_boost` | Points of movement toward a play that make it STRONG (0.5) |
| `recommend.kelly_fraction` / `max_units` | Bet sizing: 0.25 = quarter-Kelly, capped at 2 units |
| `markets` | `spreads` and `totals` (`h2h` adds moneylines but goes over the free budget) |
| `bookmakers` | Up to 10 books (more than 10 doubles the cost). Free-plan keys only, so no Caesars or Fanatics |
| `spread_move_alert` / `total_move_alert` | How many points counts as a BIG MOVE, per sport |
| `max_off_number` | How far a book's number can sit from Pinnacle's and still be priced (≈ estimate) |
| `days_ahead` | How far ahead to pull games |

To rename the site, edit the `HURST<span>STRATEGY</span>` text and the `<title>` in `site/index.html`.

## Honest limitations
- **"Open" is the first line we captured**, not the book's true opener. On the free plan we snapshot four times a day. True openers need a paid historical plan.
- **Pinnacle is the benchmark.** The Odds API notes Pinnacle's odds come from its public website and can lag slightly, so a "play" can be stale by the time you see it. Always check the live price.
- **Off-number plays (marked ≈)** use a simple rule of thumb: each spread point is about 3% win probability and each total point about 2%. Football spreads that cross 3 or 7 are skipped instead of guessed.
- **Four updates a day** will miss fast moves. A paid plan (about $30/mo) supports hourly updates.
- The page is informational only and includes 21+ and 1-800-GAMBLER notices. Keep them.

## Testing without an API key
```
python scripts/update.py --sample     # run a few times to see line movement
cd site && python -m http.server       # then open http://localhost:8000
```
Sample data is fake and labeled SAMPLE DATA on the page. Delete `data/` and `site/data/` before going live.

## Good next steps
1. **Results + CLV tracking.** `data/archive/` already saves every play flagged along with each game's closing line. Add scores to grade plays and show a public record. Beating the closing line is the best sign the edges are real.
2. **Your own ratings model.** Build team ratings from free box-score data and show "model vs. market" next to the +EV board.
3. **Alerts.** Send a GroupMe or email message when a BIG MOVE or a +3% play appears.
