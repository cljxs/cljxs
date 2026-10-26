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

* **What:** bets placed under the rules in `_ace-agents-header.md`: estimate
  pulled 60% toward the market and still 2 points (or 3% of it) clear, sides
  between 30% and 75%, flat $100 - placed by code from the blind estimates
  since 2026-10-12 (see the change log; before that, cited fresh news was
  also required).
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
| Fresh news | optional since 2026-10-12; when cited: 24 hours or less, price moved less than 2 points toward the side since |
| Betting window | games starting within 12 hours |
| Blind window | games starting within 30 hours |
| Close | last price before the start, re-read every 5 minutes in the last 2 hours |

## Change log

* 2026-10-01 — frozen.
* 2026-10-02 — before any bet under these rules: fresh news widened from 6 to
  24 hours (owner's decision). On a real board 1 of 8 bettable games had
  news inside six hours, so the six-hour rule allowed almost no bets. The
  price-not-moved check is unchanged and is what keeps priced news out.
* 2026-10-03 — schedule, not a rule: the betting wake moved from 15:00 to
  11:30 ET (owner's decision), so noon ET college kickoffs and Sunday's 13:00
  NFL games are inside the 12-hour betting window at some wake. Every number
  in the table above is unchanged.
* 2026-10-12 — a rule change, the owner's decision: **news is no longer
  required for a bet.** Under the cited-news rule there was not one bet in a
  month, so Gate 2 had nothing to measure. Bets are now placed by code from
  Ace's blind estimates (made before he sees a price) wherever one clears the
  bar at the current price; he may still bet by hand on the same bar, and
  news he cites is still checked. The shrink, bar, range, stake, cap and both
  stops are unchanged. Gate 2 now measures the blind estimates' edge over the
  close; the fresh-news baseline stays in the comparison, so it still shows
  whether news alone would have done as well.
* 2026-10-21 — a bug fix, not a rule change: NBA (and baseball) games are read
  from ESPN's scoreboard by Eastern date, today and tomorrow. The undated board
  Ace was reading still showed Tuesday's finished games at 09:41 ET on
  Wednesday, so opening week's NBA games never reached the blind sheet or the
  bets - the 2026-10-20 night sheet held one college game. Gate 1 says every
  game in the next 30 hours; this makes the sheet match it. Every number in
  the table above is unchanged.
* 2026-10-26 — not a rule change: Ace Props (`scripts/ace-props.py`), NBA
  player points milestones picked by plain code, is a separate book on the
  same bankroll (owner's decision). Its bets carry `book: "props"` and are kept
  out of everything measured here: the 4-bet cap, Gate 1, Gate 2, closing-line
  value, the evidence stop, the no-AI comparison and the fault stop. It has its
  own cap (2 a day) and its own stop ($1,000 of settled losses). Every number in
  the table above is unchanged.
