# Timmy — fund analyst

Reads four stocks three times a trading day and writes a signal report on
each. **Timmy does not trade.** No portfolio, no cash, no orders. It reads
data and forms opinions; you decide what to do with them.

    Fetcher (free, every 15 min)  ->  data/*.json  ->  Timmy (costs fuel, 3x/day)  ->  reports/

## The watchlist

`state/watchlist.json`. Four tickers to start: HIMS, ASTS, UBER, IREN. Edit
the file — or just ask Claude — and both halves pick it up on the next run.
No code change, no restart.

## The anti-hallucination rule

`scripts/timmy-fetch.py` is plain Python with no AI in it. It pulls daily
candles from Yahoo, computes SMA20, SMA50, RSI14, MACD and the 1d/5d/30d
changes locally, pulls per-ticker headlines, and writes JSON into `data/`.

Timmy only reads those files. **If a number is not in a data file, Timmy is
not allowed to cite it.** The indicator math lives in `scripts/indicators.py`
and is bit-identical to Belfort's, so the two agents never disagree about what
UBER's RSI is.

Headlines are **titles only** — the article bodies are never fetched. Timmy is
told to treat a headline as something that was published, not something it has
verified.

## The staleness gate

`timmy-fetch.py --status` prints a verdict, and Timmy's first action every
cycle is to run it and obey:

    DATA AGE: 12 minutes (fetched 2026-09-14 17:37:54 UTC)
    VERDICT: PROCEED. Data is fresh enough to analyse.

If the data is older than 24 hours it prints `SKIP THIS CYCLE` instead, and
Timmy writes no analysis — just a memory line recording the skip. Plain code
makes that call, not the model: comparing timestamps is exactly the sort of
thing a language model gets quietly wrong.

This is also why the fetcher runs every six hours overnight and at weekends.
The market is shut and the prices do not change — but without those runs the
data file would be 65 hours old by Monday's first cycle, Timmy would correctly
refuse to run, and it would look broken when it was working perfectly.

## The verifier

`scripts/timmy-verify.py` runs after every cycle and fails the systemd unit if
the deliverables are not on disk: four reports for today, each long enough to
be real, each with a `**Lean: BUY|HOLD|SELL**` line, plus a new line in
`MEMORY.md`.

This exists because systemd only sees an exit code, and an agent that prints a
report into the chat instead of writing it exits 0. Belfort and Scout both
shipped that failure and both were logged as successes. Timmy fails loudly.

## Checking on it

    systemctl list-timers 'timmy-*'                  # when it next runs
    journalctl -u timmy-cycle -n 40 --no-pager       # what the last cycle did
    journalctl -u timmy-fetch -n 20 --no-pager       # is the data landing
    python3 scripts/timmy-fetch.py --status          # how fresh is the data
    ls -t agents/timmy/reports | head                # the latest reports
    cat agents/timmy/MEMORY.md                       # every cycle, one line each

## Pausing it

    systemctl disable --now timmy-cycle.timer        # stop the thinking (and the cost)
    systemctl disable --now timmy-fetch.timer        # stop the data collection too

Stopping `timmy-cycle.timer` alone is usually what you want: the fetcher is
free, and leaving it running means the data is current whenever you switch
Timmy back on. Re-enable with `systemctl enable --now timmy-cycle.timer`.

## Files

    agents/timmy/
      AGENTS.md              what Timmy reads at the start of every run
      MEMORY.md              append-only, one line per cycle, kept under 2KB
      state/watchlist.json   the four tickers — edit freely
      data/                  fetcher-owned, Timmy only reads (gitignored)
      reports/               one file per ticker per day (gitignored)

    scripts/
      timmy-fetch.py         the fetcher, and the --status staleness gate
      timmy-verify.py        the post-cycle check
      indicators.py          shared SMA / RSI / MACD math

## Changing it

Cadence, tickers, report length, what it looks at — all of it is yours. Ask
and it changes. The only thing worth knowing: cost scales with how often the
*cycle* runs, not the fetcher. The fetcher is free. Three cycles a day is the
starting point, not a rule.
