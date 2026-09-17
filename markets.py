"""
Market comparison across h2h, spreads and totals.

THE RULE THAT MAKES THIS CORRECT
  Two prices are only comparable at the SAME line. Hard Rock -3.5 and a
  consensus of -3 are different bets, not a 20-cent edge. Everything below
  groups by the exact point value first, then compares prices within it.

WHAT IT COMPARES
  1. Hard Rock vs the de-vigged consensus of every other book, per line.
  2. Kalshi / Polymarket vs that same consensus, when the exchange contract
     resolves on the identical number.
"""
import json
from collections import defaultdict

from edge_scanner import (am_to_prob, am_profit, kalshi_cost, poly_cost,
                          poly_outcomes, devig, kelly, match_team)

MARKETS = ("h2h", "spreads", "totals")


def book_lines(event, market_key):
    """{point: {outcome_name: [prob, prob, ...]}} across all books.

    point is None for moneylines, the spread for spreads, the total for totals.
    Outcome names for totals are 'Over'/'Under'; for spreads, team names.
    """
    out = defaultdict(lambda: defaultdict(list))
    for bk in event.get("bookmakers", []):
        for mk in bk.get("markets", []):
            if mk["key"] != market_key:
                continue
            for o in mk.get("outcomes", []):
                pt = o.get("point")
                # normalise: a spread is stored under the FAVOURITE's number so
                # both sides of the same bet land in the same bucket
                key = None if pt is None else abs(float(pt))
                out[key][(o["name"], pt)].append(am_to_prob(o["price"]))
    return out


def fair_prices(event, market_key, exclude_book="hardrockbet", min_books=3):
    """De-vigged fair probability per (point, outcome), from books other than
    the one we are trying to beat. Including Hard Rock in its own benchmark
    would quietly shrink every edge we are looking for."""
    per_book = defaultdict(dict)
    for bk in event.get("bookmakers", []):
        if bk["key"] == exclude_book:
            continue
        for mk in bk.get("markets", []):
            if mk["key"] != market_key:
                continue
            outs = {(o["name"], o.get("point")): am_to_prob(o["price"])
                    for o in mk.get("outcomes", [])}
            # pair the two sides that belong together
            names = list(outs)
            if len(names) != 2:
                continue
            (n1, p1), (n2, p2) = names
            key = None if p1 is None else abs(float(p1))
            per_book[key][bk["key"]] = ((n1, p1, devig(outs[(n1, p1)], outs[(n2, p2)])),
                                        (n2, p2, devig(outs[(n2, p2)], outs[(n1, p1)])))
    fair = {}
    for key, books in per_book.items():
        if len(books) < min_books:
            continue
        acc = defaultdict(list)
        for pair in books.values():
            for name, pt, f in pair:
                if f is not None:
                    acc[(name, pt)].append(f)
        for k, vals in acc.items():
            fair[(key, k)] = (sum(vals) / len(vals), len(vals))
    return fair


def hardrock_offers(event, market_key, book_key="hardrockbet"):
    """{(point_key, (name, point)): american_odds} for the one book we bet at."""
    out = {}
    for bk in event.get("bookmakers", []):
        if bk["key"] != book_key:
            continue
        for mk in bk.get("markets", []):
            if mk["key"] != market_key:
                continue
            for o in mk.get("outcomes", []):
                pt = o.get("point")
                key = None if pt is None else abs(float(pt))
                out[(key, (o["name"], pt))] = o["price"]
    return out


# ---------------------------------------------------------------- exchanges
def kalshi_number(m):
    """Pull the line a Kalshi contract settles on, or None if unclear.

    Field names vary by series, so try the documented ones and give up rather
    than guess -- a wrong number silently compares two different bets.
    """
    for f in ("floor_strike", "cap_strike", "strike"):
        v = m.get(f)
        if isinstance(v, (int, float)):
            return float(v)
    sub = (m.get("yes_sub_title") or m.get("subtitle") or "")
    tok = "".join(c if (c.isdigit() or c in ".-") else " " for c in sub).split()
    for t in tok:
        try:
            v = float(t)
            if 0.5 <= abs(v) <= 150:
                return v
        except ValueError:
            continue
    return None


def kalshi_side(m):
    """'over', 'under', or None."""
    txt = ((m.get("yes_sub_title") or "") + " " + (m.get("title") or "")).lower()
    if "over" in txt or "more than" in txt or "above" in txt:
        return "over"
    if "under" in txt or "fewer" in txt or "below" in txt:
        return "under"
    return None


def diagnose_kalshi(kal, n=6):
    print(f"  -- kalshi sample ({len(kal)} markets) --")
    for m in kal[:n]:
        print(f"     ticker={m.get('ticker')} | title={str(m.get('title'))[:46]}")
        print(f"       sub={str(m.get('yes_sub_title'))[:40]} "
              f"strike={m.get('floor_strike') or m.get('cap_strike') or m.get('strike')} "
              f"yes_ask={m.get('yes_ask')} parsed_number={kalshi_number(m)} "
              f"side={kalshi_side(m)}")


def scan_all(events, kal, poly, taker=True, bankroll=1000, min_edge=0.02,
             book_key="hardrockbet", diagnose=False):
    if diagnose and kal:
        diagnose_kalshi(kal)

    rows = []
    for ev in events:
        home, away = ev.get("home_team"), ev.get("away_team")
        game = f"{away} @ {home}"

        for mkt in MARKETS:
            fair = fair_prices(ev, mkt, exclude_book=book_key)
            if not fair:
                continue
            hr = hardrock_offers(ev, mkt, book_key)

            # --- Hard Rock vs the rest of the market, at the same line ---
            for (pkey, (name, pt)), odds in hr.items():
                f = fair.get((pkey, (name, pt)))
                if not f:
                    continue
                fair_p, nbooks = f
                cost = am_to_prob(odds)
                edge = fair_p - cost
                if edge >= min_edge:
                    rows.append(dict(game=game, market=mkt,
                                     side=f"{name}{'' if pt is None else f' {pt:+g}'}",
                                     venue="Hard Rock", price=f"{odds:+d}",
                                     cost=round(cost, 4), fair=round(fair_p, 4),
                                     edge=round(edge, 4), books=nbooks,
                                     stake=round(kelly(fair_p, cost) * 0.25 * bankroll, 2),
                                     match_conf=1.0))

            # --- Kalshi totals, only where the number matches exactly ---
            if mkt == "totals":
                for m in kal:
                    num, side = kalshi_number(m), kalshi_side(m)
                    ask = m.get("yes_ask")
                    if num is None or side is None or not ask:
                        continue
                    title = (m.get("title") or "") + " " + (m.get("yes_sub_title") or "")
                    if not (match_team(home, [title])[0] and match_team(away, [title])[0]):
                        continue
                    want = "Over" if side == "over" else "Under"
                    f = fair.get((abs(num), (want, num)))
                    if not f:
                        continue
                    fair_p, nbooks = f
                    cost = kalshi_cost(ask, taker)
                    edge = fair_p - cost
                    if edge >= min_edge:
                        rows.append(dict(game=game, market="totals",
                                         side=f"{want} {num:g}", venue="Kalshi",
                                         price=f"{ask}c", cost=round(cost, 4),
                                         fair=round(fair_p, 4), edge=round(edge, 4),
                                         books=nbooks,
                                         stake=round(kelly(fair_p, cost) * 0.25 * bankroll, 2),
                                         match_conf=0.9))

            # --- Polymarket moneylines (named outcomes only) ---
            if mkt == "h2h":
                for m in poly:
                    outs = poly_outcomes(m)
                    if not outs:
                        continue
                    q = m.get("question", "")
                    if not (match_team(home, [q])[0] and match_team(away, [q])[0]):
                        continue
                    for team in (home, away):
                        pick, conf = match_team(team, list(outs))
                        if not pick:
                            continue
                        f = fair.get((None, (team, None)))
                        if not f:
                            continue
                        fair_p, nbooks = f
                        cost = poly_cost(outs[pick], taker)
                        edge = fair_p - cost
                        if edge >= min_edge:
                            rows.append(dict(game=game, market="h2h", side=team,
                                             venue="Polymarket",
                                             price=f"{outs[pick]:.2f}",
                                             cost=round(cost, 4), fair=round(fair_p, 4),
                                             edge=round(edge, 4), books=nbooks,
                                             stake=round(kelly(fair_p, cost) * 0.25 * bankroll, 2),
                                             match_conf=conf))
    rows.sort(key=lambda r: -r["edge"])
    return rows
