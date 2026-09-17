# Line scanner

Watches Hard Rock Bet (FL), Kalshi and Polymarket for the same outcome priced
differently, and messages you when the gap is worth acting on.

It does not predict games. A walk-forward backtest over 8,325 games showed
closing lines can't be beaten with public data — every edge left is in
execution: arbitrage, and paying the cheaper of two prices.

## Setup (about 15 minutes)

**1. Create the repo.** Upload these files to a new GitHub repo. Private is fine
(2,000 free Actions minutes/month; this schedule uses ~356).

**2. Get an Odds API key.** Free at <https://the-odds-api.com> — 500 credits
per month. The schedule below uses 297, leaving headroom for manual runs.

**3. Set up alerts.** Telegram is the simplest:
   - Message `@BotFather` on Telegram, send `/newbot`, copy the token
   - Message your new bot once, then open
     `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the `chat.id`

   A Slack or Discord webhook works too — set `WEBHOOK_URL` instead.

**4. Add repo secrets.** Settings → Secrets and variables → Actions:

   | Secret | Required | What it is |
   |---|---|---|
   | `ODDS_API_KEY` | yes | your Odds API key |
   | `TG_TOKEN` | for Telegram | bot token from BotFather |
   | `TG_CHAT` | for Telegram | your chat id |
   | `WEBHOOK_URL` | optional | Slack/Discord webhook instead of Telegram |

**5. Test it.** Actions tab → "Line scanner" → Run workflow. Check the log for
your credit balance and whether the `hardrockbet` key appears. Most runs will
report nothing above threshold — that is the normal result, not a failure.

## When it runs

Game windows only, every 30 minutes on Saturday and Sunday and hourly on
Thursday, Friday and Monday nights. An always-on 15-minute schedule would burn
the entire monthly credit budget in four days.

GitHub's scheduled runs are queued, not guaranteed — during busy periods they
can be delayed 5 to 15 minutes. Fine for pre-game pricing, not fast enough for
live in-game moves.

## Tuning

Edit the `env:` block in `.github/workflows/scan.yml`:

| Setting | Default | Meaning |
|---|---|---|
| `ARB_MIN_PCT` | 0.75 | minimum locked profit to alert |
| `VALUE_MIN_PCT` | 3.0 | minimum edge over consensus fair price, in probability points |
| `MIN_MATCH_CONF` | 0.85 | reject uncertain team matches between venues |
| `REALERT_HOURS` | 6 | don't repeat the same alert within this window |
| `SPORTS` | `americanfootball_ncaaf` | add `americanfootball_nfl`, `basketball_nba`, etc. |

Adding a sport multiplies credit use — two sports is 594 of 500 credits, over
the limit. Either drop a scan window or upgrade the plan before adding one.

## Before you trade anything it finds

- Confirm both prices at both venues. Prices move while you place the first leg.
- Check order book depth. The displayed exchange price may cover only a few
  contracts, and a partial fill on one leg leaves you with a naked position.
- Check `match_conf`. Below 0.85 it isn't sent, but even 0.9 deserves a glance
  that both venues really mean the same game.
