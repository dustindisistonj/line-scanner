#!/usr/bin/env python3
"""
Three-venue edge scanner: Hard Rock (FL) vs Kalshi vs Polymarket.

WHAT IT LOOKS FOR
  1. Arbitrage  - opposite sides at two venues costing < 100% of the payout
  2. Value      - an exchange price cheaper than the de-vigged consensus fair price
  3. Best price - which venue is cheapest for any side you already want

WHY THIS AND NOT A PREDICTION MODEL
  Backtesting 8,325 games showed closing lines are unbeatable with public data.
  Price differences between venues are not a forecast -- they are arithmetic.

DATA SOURCES
  The Odds API   - needs a free key (500 credits/month). Covers Hard Rock Bet FL.
  Kalshi         - public read endpoints, no key.
  Polymarket     - public Gamma API, no key.
"""
import os, sys, json, time, difflib, argparse
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    sys.exit("pip install requests")

ODDS_KEY = os.environ.get("ODDS_API_KEY", "").strip()
SPORT = "americanfootball_ncaaf"

# ---------------------------------------------------------------- fee models
def am_to_prob(odds):
    """American odds -> implied probability (vig included)."""
    return (-odds) / ((-odds) + 100) if odds < 0 else 100 / (odds + 100)

def am_profit(odds):
    return 100 / (-odds) if odds < 0 else odds / 100

def kalshi_cost(price_cents, taker=True):
    """All-in cost per $1 contract. Taker fee = 0.07*p*(1-p), maker usually 0."""
    p = price_cents / 100
    return p + (0.07 * p * (1 - p) if taker else 0.0)

def poly_cost(price, taker=True, fee_rate=0.018):
    """Polymarket taker fees ran 0.75%-1.80% by category; makers free.
    fee_rate is on the position, so use the conservative end by default."""
    return price * (1 + fee_rate) if taker else price

def devig(p1, p2):
    """Two-sided de-vig -> fair probability for side 1."""
    return p1 / (p1 + p2) if (p1 + p2) > 0 else None

def kelly(fair, cost):
    """Fraction of bankroll, for a contract costing `cost` that pays 1."""
    b = (1 - cost) / cost
    q = 1 - fair
    return max(0.0, (fair * b - q) / b)

# ---------------------------------------------------------------- fetchers
def fetch_odds_api(markets="h2h,spreads,totals", books=None, regions="us,us2"):
    """Consensus board + Hard Rock. Costs credits = markets x regions."""
    if not ODDS_KEY:
        print("  ! ODDS_API_KEY not set - skipping sportsbook side")
        return []
    url = f"https://api.the-odds-api.com/v4/sports/{SPORT}/odds"
    params = dict(apiKey=ODDS_KEY, regions=regions, markets=markets,
                  oddsFormat="american", dateFormat="iso")
    if books:
        params["bookmakers"] = books
    r = requests.get(url, params=params, timeout=30)
    if r.status_code != 200:
        print(f"  ! odds api {r.status_code}: {r.text[:200]}")
        return []
    print(f"  credits used {r.headers.get('x-requests-used','?')}, "
          f"remaining {r.headers.get('x-requests-remaining','?')}")
    return r.json()

def fetch_kalshi(series_hint="NCAAF"):
    """Public markets endpoint. Series tickers change; filter broadly."""
    out, cursor = [], None
    base = "https://api.elections.kalshi.com/trade-api/v2/markets"
    for _ in range(12):
        p = {"status": "open", "limit": 1000}
        if cursor:
            p["cursor"] = cursor
        try:
            r = requests.get(base, params=p, timeout=30)
            if r.status_code != 200:
                print(f"  ! kalshi {r.status_code}"); break
            d = r.json()
        except Exception as e:
            print(f"  ! kalshi {e}"); break
        out += [m for m in d.get("markets", [])
                if series_hint in (m.get("ticker", "") + m.get("event_ticker", "")).upper()]
        cursor = d.get("cursor")
        if not cursor:
            break
    return out

def fetch_polymarket(query="college football"):
    """Gamma API, public. Returns open markets matching the query."""
    try:
        r = requests.get("https://gamma-api.polymarket.com/markets",
                         params={"closed": "false", "limit": 500, "order": "volume24hr",
                                 "ascending": "false"}, timeout=30)
        if r.status_code != 200:
            print(f"  ! polymarket {r.status_code}"); return []
        ms = r.json()
    except Exception as e:
        print(f"  ! polymarket {e}"); return []
    terms = ("ncaa", "college football", "cfb")
    return [m for m in ms if any(t in (m.get("question", "") + m.get("slug", "")).lower()
                                 for t in terms)]

# ---------------------------------------------------------------- matching
QUALIFIERS = {"state", "tech", "a&m", "am", "southern", "northern", "eastern",
              "western", "central", "international", "atlantic", "christian"}
# schools where a wrong match is easy and expensive
CONFLICTS = {
    "fl": {"oh", "redhawks"}, "oh": {"hurricanes"},
    "miami": set(), "ohio": set(),
}

def toks(s):
    s = (s or "").lower()
    s = "".join(ch if ch.isalnum() or ch.isspace() or ch == "&" else " " for ch in s)
    stop = {"the", "at", "vs", "v", "win", "wins", "winner", "will", "to",
            "game", "beat", "beats", "over", "college", "football", "ncaa"}
    return [t for t in s.split() if t not in stop]

def contiguous(hay, needle):
    """Is the token list `needle` present contiguously inside `hay`?"""
    n = len(needle)
    return any(hay[i:i+n] == needle for i in range(len(hay) - n + 1))

def match_team(name, candidates, cutoff=0.72):
    """Match a sportsbook team name to an exchange market title.

    Works on the longest prefix of the team name that appears in the title
    (sportsbook names lead with the school), then rejects the classic traps:
    Georgia matching 'Georgia State', Miami (FL) matching Miami (OH).
    """
    q = toks(name)
    if not q:
        return (None, 0.0)
    q_marks = {t for t in q if t in ("fl", "oh")}
    best, score = None, 0.0

    for c in candidates:
        ct = toks(c)
        if not ct:
            continue
        hit, used = False, 0
        for L in range(len(q), 0, -1):          # longest prefix first
            phrase = q[:L]
            if phrase and contiguous(ct, phrase):
                hit, used = True, L
                break
        if not hit:
            continue

        school = q[:used]
        # reject if the title qualifies the school differently (Georgia vs Georgia State)
        bad = False
        for i in range(len(ct) - len(school) + 1):
            if ct[i:i+len(school)] == school:
                nxt = ct[i+len(school)] if i+len(school) < len(ct) else None
                if nxt in QUALIFIERS and nxt not in q:
                    bad = True
                else:
                    bad = False
                    break
        if bad:
            continue
        # state-marker conflicts (Miami FL vs Miami OH)
        conflict = False
        for m in q_marks:
            if any(w in ct for w in CONFLICTS.get(m, set())):
                conflict = True
        if "fl" in q_marks and ("oh" in ct or "redhawks" in ct):
            conflict = True
        if conflict:
            continue

        s = 0.60 + 0.40 * (used / len(q))       # longer prefix = more confident
        if s > score:
            best, score = c, s
    return (best, round(score, 3)) if score >= cutoff else (None, round(score, 3))

# ---------------------------------------------------------------- analysis
def consensus_fair(event, market_key="h2h"):
    """De-vigged consensus across all books = the best fair-price estimate."""
    home, away = event["home_team"], event["away_team"]
    hs, as_ = [], []
    for bk in event.get("bookmakers", []):
        for mk in bk.get("markets", []):
            if mk["key"] != market_key:
                continue
            o = {x["name"]: x["price"] for x in mk["outcomes"]}
            if home in o and away in o:
                hs.append(am_to_prob(o[home])); as_.append(am_to_prob(o[away]))
    if not hs:
        return None, None, 0
    ph, pa = sum(hs)/len(hs), sum(as_)/len(as_)
    return devig(ph, pa), devig(pa, ph), len(hs)

def hardrock_price(event, team, market_key="h2h", book_keys=("hardrockbet",)):
    for bk in event.get("bookmakers", []):
        if bk["key"] not in book_keys:
            continue
        for mk in bk.get("markets", []):
            if mk["key"] != market_key:
                continue
            for x in mk["outcomes"]:
                if x["name"] == team:
                    return x["price"]
    return None

def find_arbs(events, kal_titles, poly_titles, taker=True, bankroll=500,
              book_keys=("hardrockbet",)):
    """Hard Rock on one side, an exchange on the other. If the two together
    cost less than the payout, the profit is locked whatever happens."""
    out = []
    for ev in events:
        home, away = ev["home_team"], ev["away_team"]
        for team, other in ((home, away), (away, home)):
            hr = hardrock_price(ev, team, book_keys=book_keys)
            if not hr:
                continue
            dec = 1 + am_profit(hr)
            legs = []
            # Kalshi: buying NO on `team` is backing the opponent
            kt, ks = match_team(team, list(kal_titles))
            if kt:
                na = kal_titles[kt].get("no_ask")
                if na:
                    legs.append(("Kalshi", f"NO on {kt[:40]} @ {na}c",
                                 kalshi_cost(na, taker), ks))
            # Polymarket: second outcome price is the opposing side
            pt, ps = match_team(team, list(poly_titles))
            if pt:
                try:
                    pr = json.loads(poly_titles[pt].get("outcomePrices", "[]"))
                    if len(pr) > 1:
                        legs.append(("Polymarket", f"NO on {pt[:40]} @ {float(pr[1]):.2f}",
                                     poly_cost(float(pr[1]), taker), ps))
                except Exception:
                    pass
            for venue, desc, cost, conf in legs:
                total = 1 / dec + cost
                if total < 1:
                    payout = bankroll / total
                    out.append(dict(game=f"{away} @ {home}",
                                    leg_a=f"Hard Rock {team} {hr:+d}",
                                    leg_b=f"{venue}: {desc}",
                                    profit_pct=round((1 - total) / total * 100, 2),
                                    stake_hr=round(payout / dec, 2),
                                    stake_ex=round(payout * cost, 2),
                                    returns=round(payout, 2),
                                    match_conf=conf))
    out.sort(key=lambda r: -r["profit_pct"])
    return out


def scan(min_edge=0.02, bankroll=1000, taker=True, book_keys=("hardrockbet",)):
    print("fetching sportsbook board...");  events = fetch_odds_api()
    print(f"  {len(events)} games")
    print("fetching kalshi...");            kal = fetch_kalshi()
    print(f"  {len(kal)} markets")
    print("fetching polymarket...");        poly = fetch_polymarket()
    print(f"  {len(poly)} markets")

    kal_titles = {m.get("title") or m.get("yes_sub_title") or m.get("ticker"): m for m in kal}
    poly_titles = {m.get("question"): m for m in poly}
    rows = []

    for ev in events:
        home, away = ev["home_team"], ev["away_team"]
        fair_h, fair_a, nbooks = consensus_fair(ev)
        if fair_h is None or nbooks < 3:
            continue
        for team, fair in ((home, fair_h), (away, fair_a)):
            hr = hardrock_price(ev, team, book_keys=book_keys)
            hr_be = am_to_prob(hr) if hr else None

            # exchange prices for the same team
            offers = []
            kt, ks = match_team(team, list(kal_titles))
            if kt:
                m = kal_titles[kt]
                ask = m.get("yes_ask")
                if ask:
                    offers.append(("Kalshi", kalshi_cost(ask, taker), f"{ask}c", ks))
            pt, ps = match_team(team, list(poly_titles))
            if pt:
                m = poly_titles[pt]
                try:
                    pr = json.loads(m.get("outcomePrices", "[]"))
                    if pr:
                        px = float(pr[0])
                        offers.append(("Polymarket", poly_cost(px, taker), f"{px:.2f}", ps))
                except Exception:
                    pass
            if hr_be:
                offers.append(("Hard Rock", hr_be, str(hr), 1.0))
            if not offers:
                continue

            venue, cost, shown, conf = min(offers, key=lambda x: x[1])
            edge = fair - cost
            if edge >= min_edge:
                rows.append(dict(game=f"{away} @ {home}", side=team, venue=venue,
                                 price=shown, cost=round(cost, 4),
                                 fair=round(fair, 4), edge=round(edge, 4),
                                 stake=round(kelly(fair, cost) * 0.25 * bankroll, 2),
                                 books=nbooks, match_conf=round(conf, 2)))
    rows.sort(key=lambda r: -r["edge"])
    arbs = find_arbs(events, kal_titles, poly_titles, taker, bankroll, book_keys)
    return rows, arbs

def show(rows, arbs=None):
    if arbs:
        print(f"\n*** {len(arbs)} ARBITRAGE OPPORTUNITIES ***")
        for a in arbs[:10]:
            print(f"  {a['game']}  ->  +{a['profit_pct']}% locked")
            print(f"    A: {a['leg_a']}   stake ${a['stake_hr']}")
            print(f"    B: {a['leg_b']}   stake ${a['stake_ex']}")
            print(f"    returns ${a['returns']} either way (match confidence {a['match_conf']})")

    if not rows:
        print("\nNo opportunities above threshold. That is the normal result.")
        return
    print(f"\n{'GAME':<34}{'SIDE':<22}{'VENUE':<12}{'PRICE':>8}{'FAIR':>8}{'EDGE':>8}{'STAKE':>9}")
    for r in rows:
        print(f"{r['game'][:33]:<34}{r['side'][:21]:<22}{r['venue']:<12}"
              f"{r['price']:>8}{r['fair']*100:>7.1f}%{r['edge']*100:>7.1f}%{r['stake']:>9.2f}")
    print("\nConfirm every price at the venue before trading. Fuzzy team matching "
          "can pair the wrong markets; check match_conf in the JSON output.")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-edge", type=float, default=2.0, help="percent")
    ap.add_argument("--bankroll", type=float, default=1000)
    ap.add_argument("--maker", action="store_true", help="assume limit orders (no fee)")
    ap.add_argument("--books", default="hardrockbet")
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    rows, arbs = scan(a.min_edge/100, a.bankroll, not a.maker, tuple(a.books.split(",")))
    show(rows, arbs)
    if a.json:
        json.dump({"value": rows, "arbs": arbs}, open(a.json, "w"), indent=1)
        print(f"\nwrote {a.json}")
