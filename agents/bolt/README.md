# Bolt — crypto momentum, on paper

A $10,000 paper account trading ten big coins, run entirely by
`scripts/bolt.py` on `bolt-cycle.timer`. **No model is woken**: every rule is
a formula, so the script is the trader. It costs nothing and cannot make up a
number. `NO_MODEL` in this folder tells preflight and deploy.sh so.

## What runs when

Every 10 minutes, every day:
- one CoinGecko call for BTC, ETH, SOL, BNB, XRP, ADA, AVAX, LINK, DOT, LTC;
  the 20- and 50-hour averages come from the 7 days of hourly prices in it
- the exits, at that tick's price, less a 0.1% fee: take profit +15%, stop
  -4% (raised to the price paid after +8%), dead money (24h within 2%), and
  the BTC dump switch (BTC -5% on the day: every coin but BTC is sold)

Every 4 hours (00/04/08/12/16/20 UTC): at most one new coin, the best
scoring 7+ on the 10-point rubric, sized at 15% of the account while keeping
5% in cash. No limit on how many he holds - one of each coin, as far as the
cash goes. Down 15%: no buys for 24 hours, then he
starts again on his own and counts from there.

## Files

    data/market.json        this tick's prices and averages
    state/portfolio.json    the book (checked every tick: cash replayed from the trades)
    state/equity.jsonl      the account, every tick, for the screen's chart
    reports/YYYY-MM-DD.md   each buy check, and every sale between them
    MEMORY.md               one line a buy check

## Commands

    scripts/deploy.sh bolt              install the timer and run one tick
    python3 scripts/bolt.py show        the book in words
    python3 scripts/bolt.py check       does it balance?
    systemctl stop bolt-cycle.timer     pause him
    journalctl -u bolt-cycle -n 20      the last ticks

His screen is /bolt on the Command Deck, and his house in the village.
