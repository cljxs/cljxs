# Ace — what is being measured, written down before the next bet

Frozen **2026-10-01**. Nothing below changes before **2026-12-01** except a bug
fix, and a bug fix is logged here with its date. Changing a rule restarts the
clock, because results under two rule sets cannot be added together.

Why this exists: an outside review (2026-10-01) pointed out that without
targets fixed in advance, every later look at the numbers is a fishing trip,
and a positive result could never be trusted.

## The question

Can a small language model, reading free ESPN data, forecast US sports
games better than the betting market — and if so, does that turn into bets
that beat the closing price? Profit is not the scorecard: at flat stakes, a
genuine +2% edge still shows a loss about 4 times in 10 after 500 bets.

## Gate 1 — blind forecasts (the main one)

* **What:** before seeing any price, Ace gives every game on `data/blind.json`
  (all games in the next 30 hours) the home team's chance of winning.
  Recorded in `state/blind.jsonl`. The latest estimate before the start counts.
* **Scored by:** Brier score (mean squared error; lower is better) against the
  final result, compared on the same games with DraftKings' no-vig chance at
  the moment he estimated. Also reported: DraftKings' close, Pinnacle's close.
  `python3 scripts/ace-clv.py`.
* **Sample:** 300 finished games. Not read as a verdict before that.
* **Pass:** Ace's Brier score is lower than DraftKings' at the moment he estimated.
* **Fail:** it is equal or higher. Then the conclusion is that Ace is a data
  tool, not a forecaster, and Gate 2's results are luck either way.

## Gate 2 — bets and closing-line value

* **What:** bets placed under the rules in `_ace-agents-header.md`: cited fresh
  news, price not yet moved 2 points on it, estimate pulled 60% toward the
  market and still 2 points (or 3% of it) clear, sides between 30% and 75%,
  flat $100.
* **Scored by:** closing-line value — the price taken, valued at the closing
  no-vig chance — against Pinnacle's close where there is one (the headline),
  DraftKings' otherwise (reported alongside). Also: the share of bets that beat
  the close.
* **Sample:** 150 bets for a first read; 200 for a verdict.
* **Edge shown:** average CLV of +1.5 points or more and at least 55% of bets
  beating the close, over 200+ bets, with the same sign in both halves of the
  sample.
* **No edge:** average CLV between −1 and +1 point after 150 bets.

## The comparison

Five bettors with no AI in them (`scripts/ace-baselines.py`) bet the same
candidate games at flat $100: random, the favourite, fresh news (the AI-free
version of Ace's rule), Pinnacle-above-DraftKings, and steam. The AI's
contribution is Ace's CLV minus the best of these on the same games. If the
fresh-news bettor matches Ace, the model adds nothing.

## Stops

* **Fault stop:** settled losses reach 30% of the starting bankroll → betting
  pauses and the code and ledger are checked for a fault. Not a verdict on skill.
* **Evidence stop:** 60 settled bets with an average CLV at or below zero →
  betting stops and the owner decides.

## Fixed numbers

| | |
|---|---|
| Stake | $100 flat (1% of $10,000) |
| Max open bets | 4, one per game |
| Shrink toward the market | 60% (the estimate keeps 40% of its distance) |
| Bar | +2 points, or 3% of the market's chance if larger, and EV > 0 |
| Bet range | market chance 30–75% |
| Fresh news | 24 hours or less; price moved less than 2 points toward the side since |
| Betting window | games starting within 12 hours |
| Blind window | games starting within 30 hours |
| Close | last price before the start, re-read every 5 minutes in the last 2 hours |

## Change log

* 2026-10-01 — frozen.
* 2026-10-02 — before any bet under these rules: fresh news widened from 6 to
  24 hours (owner's decision). On a real board 1 of 8 bettable games had
  news inside six hours, so the six-hour rule allowed almost no bets. The
  price-not-moved check is unchanged and is what keeps priced news out.
