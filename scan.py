#!/usr/bin/env python3
"""
Scheduled runner. Scans, decides what is worth waking you up for, alerts once.

Design constraints this file exists to respect:
  - The Odds API free tier is 500 credits/month. Every call is counted and the
    run aborts if the remaining balance drops near zero, so a runaway schedule
    can never silently burn the month's budget.
  - Alerts must not repeat. The same arb sitting there for two hours should
    notify once, not twelve times.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

import edge_scanner as es
import markets as mk

# ---- thresholds: what is actually worth a notification -----------------
ARB_MIN_PCT      = float(os.environ.get("ARB_MIN_PCT", "0.75"))   # locked profit %
VALUE_MIN_PCT    = float(os.environ.get("VALUE_MIN_PCT", "3.0"))  # prob. points
MIN_MATCH_CONF   = float(os.environ.get("MIN_MATCH_CONF", "0.85"))
BANKROLL         = float(os.environ.get("BANKROLL", "1000"))
BOOK             = os.environ.get("BOOK_KEY", "hardrockbet")
CREDIT_FLOOR     = int(os.environ.get("CREDIT_FLOOR", "40"))
REALERT_HOURS    = float(os.environ.get("REALERT_HOURS", "6"))
LOOP_MINUTES     = float(os.environ.get("LOOP_MINUTES", "0"))    # 0 = single pass
INTERVAL_SEC     = int(os.environ.get("INTERVAL_SEC", "180"))
MARKETS_PARAM    = os.environ.get("MARKETS", "h2h,spreads,totals")
DIAGNOSE         = os.environ.get("DIAGNOSE", "") == "1"
SPORTS           = [s.strip() for s in
                    os.environ.get("SPORTS", "americanfootball_ncaaf").split(",") if s.strip()]

STATE = Path("state/seen.json")


def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(st):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=4)).timestamp()
    st = {k: v for k, v in st.items() if v > cutoff}
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1))
    return st


def credits_left():
    """One cheap probe so we never start a scan we cannot afford."""
    if not es.ODDS_KEY:
        return None
    try:
        r = requests.get("https://api.the-odds-api.com/v4/sports",
                         params={"apiKey": es.ODDS_KEY}, timeout=20)
        rem = r.headers.get("x-requests-remaining")
        return int(rem) if rem is not None else None
    except Exception:
        return None


def notify(text):
    """Telegram if configured, else Slack/Discord webhook, else stdout only."""
    sent = False
    tok, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if tok and chat:
        try:
            r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                              json={"chat_id": chat, "text": text,
                                    "parse_mode": "Markdown",
                                    "disable_web_page_preview": True}, timeout=20)
            sent = r.status_code == 200
            if not sent:
                print(f"telegram error {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"telegram failed: {e}")
    hook = os.environ.get("WEBHOOK_URL")
    if hook:
        try:
            requests.post(hook, json={"content": text, "text": text}, timeout=20)
            sent = True
        except Exception as e:
            print(f"webhook failed: {e}")
    if not sent:
        print("NO ALERT CHANNEL CONFIGURED - printing instead")
    print(text)


def fmt_arb(a):
    if a.get("suspect"):
        return (f"⚠️ *SUSPECT {a['profit_pct']}%* — {a['game']}\n"
                f"  A margin this large almost always means the two legs are the\n"
                f"  SAME side. Read both markets before risking anything.\n"
                f"  A: {a['leg_a']}\n  B: {a['leg_b']}")
    return (f"*ARB {a['profit_pct']}%* — {a['game']}\n"
            f"  A: {a['leg_a']}  →  stake ${a['stake_hr']}\n"
            f"  B: {a['leg_b']}  →  stake ${a['stake_ex']}\n"
            f"  returns ${a['returns']} either way (match {a['match_conf']})")


def fmt_val(v):
    return (f"*VALUE +{v['edge']*100:.1f} pts* — {v['game']}\n"
            f"  {v['side']} at {v['venue']} {v['price']}\n"
            f"  fair {v['fair']*100:.1f}% vs cost {v['cost']*100:.1f}%, "
            f"stake ${v['stake']} (match {v['match_conf']})")


def one_pass(state, diagnose=False):
    """A single scan across every configured sport. Returns new alert strings."""
    now = datetime.now(timezone.utc).timestamp()
    fresh = []
    for sport in SPORTS:
        es.SPORT = sport
        events = es.fetch_odds_api(markets=MARKETS_PARAM, books=None, regions="us")
        if not events:
            continue
        kal = es.fetch_kalshi(diagnose=diagnose)
        poly = es.fetch_polymarket()
        print(f"  {sport}: {len(events)} games, {len(kal)} kalshi, {len(poly)} poly")

        # min_edge=-1 returns everything; we filter for alerts below but log
        # the near-misses so we can see whether the threshold is set sanely.
        rows = mk.scan_all(events, kal, poly, taker=True, bankroll=BANKROLL,
                           min_edge=-1, book_key=BOOK, diagnose=diagnose)
        if rows:
            top = rows[:3]
            print("    best edges this pass: " + " | ".join(
                f"{r['side'][:22]} {r['venue'][:9]} {r['edge']*100:+.1f}" for r in top))
            by_venue = {}
            for r in rows:
                v = r["venue"]
                by_venue[v] = max(by_venue.get(v, -9), r["edge"] * 100)
            print("    best by venue: " + ", ".join(f"{k} {v:+.1f}" for k, v in by_venue.items()))
        else:
            print("    no comparable lines found at all (check line matching)")
        arbs = es.find_arbs(events, {(m.get("title") or m.get("ticker")): m for m in kal},
                            poly, True, BANKROLL, (BOOK,))

        for a in arbs:
            if a["profit_pct"] < ARB_MIN_PCT or a["match_conf"] < MIN_MATCH_CONF:
                continue
            key = f"arb|{sport}|{a['game']}|{a['leg_b'][:40]}"
            if now - state.get(key, 0) < REALERT_HOURS * 3600:
                continue
            state[key] = now
            fresh.append(fmt_arb(a))

        for v in rows:
            if v["edge"] * 100 < VALUE_MIN_PCT or v["match_conf"] < MIN_MATCH_CONF:
                continue
            key = f"val|{sport}|{v['game']}|{v['market']}|{v['side']}|{v['venue']}"
            if now - state.get(key, 0) < REALERT_HOURS * 3600:
                continue
            state[key] = now
            fresh.append(fmt_val(v))
    return fresh


def main():
    if not es.ODDS_KEY:
        sys.exit("ODDS_API_KEY not set")

    state = load_state()
    started = time.time()
    passes = 0

    while True:
        left = credits_left()
        if left is not None and left < CREDIT_FLOOR:
            print(f"credits {left} below floor {CREDIT_FLOOR} — stopping")
            break

        passes += 1
        print(f"\n--- pass {passes} (credits left {left}) ---")
        try:
            fresh = one_pass(state, diagnose=(DIAGNOSE and passes == 1))
        except Exception as e:
            print(f"pass failed: {e}")
            fresh = []

        if fresh:
            head = f"🏈 {len(fresh)} opportunit{'y' if len(fresh)==1 else 'ies'}\n\n"
            tail = "\n\n_Verify both prices before trading. Exchange size may be thinner than shown._"
            notify(head + "\n\n".join(fresh[:8]) + tail)
            save_state(state)
        else:
            print("  nothing above threshold")

        elapsed = (time.time() - started) / 60
        if LOOP_MINUTES <= 0 or elapsed + INTERVAL_SEC / 60 >= LOOP_MINUTES:
            break
        time.sleep(INTERVAL_SEC)

    save_state(state)
    print(f"\ndone: {passes} passes in {(time.time()-started)/60:.1f} min")


if __name__ == "__main__":
    main()
