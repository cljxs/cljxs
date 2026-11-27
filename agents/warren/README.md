# Warren - the stock analyst

Fills in the owner's Before-You-Buy Stock Checklist for any US-listed stock.
The owner gives the ticker, the amount, the months and the goal; Warren does
the rest, the same six steps as the worked Kinder Morgan example:

1. Know the business          Opus (checked by code: sector, real rivals, no numbers)
2. Growing and profitable?    SEC filings, last 3 years + last 12 months
3. Cash and dividend safety   FCF, coverage, payout, yield, dividend history
4. Debt and what it pays for  leverage, interest coverage, the year's cash line by line
5. Fair price vs. its sector  forward P/E, PEG, EV/EBITDA vs. two rivals; chart check
6. Risks and exit plan        three sell rules (Opus) and the holdings list

Then the checklist's decision rule: Skip / Wait / Buy.

    state/checks/<TICKER>/<stamp>.json   every check, newest last
    state/holdings.json                  what the owner owns (the page edits it)
    state/runs.json                      today's Opus questions (RUNS_PER_DAY)
    state/rechecks.json                  what the after-earnings re-check changed
    state/cache/                         SEC files, kept for hours, not committed

Code: scripts/warren.py. Page: /warren (the Deck), Warren's house in the village.

## Setting up on the droplet (once)

The SEC refuses any request without a contact email in it. Set yours once:

    echo you@example.com > agents/warren/state/sec-contact

Then check every data site from this machine (free, no model):

    python3 scripts/warren.py sources
