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

# ---- thresholds: what is actually worth a notification -----------------
ARB_MIN_PCT      = float(os.environ.get("ARB_MIN_PCT", "0.75"))   # locked profit %
VALUE_MIN_PCT    = float(os.environ.get("VALUE_MIN_PCT", "3.0"))  # prob. points
MIN_MATCH_CONF   = float(os.environ.get("MIN_MATCH_CONF", "0.85"))
BANKROLL         = float(os.environ.get("BANKROLL", "1000"))
BOOK             = os.environ.get("BOOK_KEY", "hardrockbet")
CREDIT_FLOOR     = int(os.environ.get("CREDIT_FLOOR", "40"))
REALERT_HOURS    = float(os.environ.get("REALERT_HOURS", "6"))
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
    return (f"*ARB {a['profit_pct']}%* — {a['game']}\n"
            f"  A: {a['leg_a']}  →  stake ${a['stake_hr']}\n"
            f"  B: {a['leg_b']}  →  stake ${a['stake_ex']}\n"
            f"  returns ${a['returns']} either way (match {a['match_conf']})")


def fmt_val(v):
    return (f"*VALUE +{v['edge']*100:.1f} pts* — {v['game']}\n"
            f"  {v['side']} at {v['venue']} {v['price']}\n"
            f"  fair {v['fair']*100:.1f}% vs cost {v['cost']*100:.1f}%, "
            f"stake ${v['stake']} (match {v['match_conf']})")


def main():
    if not es.ODDS_KEY:
        sys.exit("ODDS_API_KEY not set")

    left = credits_left()
    print(f"credits remaining: {left}")
    if left is not None and left < CREDIT_FLOOR:
        print(f"below floor of {CREDIT_FLOOR} — skipping to protect the month's budget")
        return

    state = load_state()
    now = datetime.now(timezone.utc).timestamp()
    fresh = []

    for sport in SPORTS:
        es.SPORT = sport
        print(f"\n=== {sport} ===")
        try:
            rows, arbs = es.scan(VALUE_MIN_PCT / 100, BANKROLL, True, (BOOK,))
        except Exception as e:
            print(f"scan failed for {sport}: {e}")
            continue

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
            key = f"val|{sport}|{v['game']}|{v['side']}|{v['venue']}"
            if now - state.get(key, 0) < REALERT_HOURS * 3600:
                continue
            state[key] = now
            fresh.append(fmt_val(v))

    save_state(state)

    if fresh:
        head = f"🏈 {len(fresh)} opportunit{'y' if len(fresh)==1 else 'ies'}\n\n"
        tail = "\n\n_Verify both prices before trading. Exchange size may be thinner than shown._"
        notify(head + "\n\n".join(fresh[:8]) + tail)
    else:
        print("\nnothing above threshold — normal result, no alert sent")


if __name__ == "__main__":
    main()
