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
* 2026-10-26 — a bug fix, not a rule change: a close counts only if it was
  seen in the last 60 minutes before the start, and after the decision it
  grades (`ace-clv.py closing`). Pinnacle is fetched on a budget of 14 calls a
  day. When the close fetches ran out, its 2:30pm snapshot was carried forward
  and read as Pinnacle's close: hours before kick-off, and for a bet placed
  after 2:30, a price from before the bet. A bet with no close that counts is
  listed by `ace-clv.py` and left out of the averages and the evidence stop;
  the headline falls back to DraftKings' close where that one counts. The same
  rule applies to the no-AI bettors and to the Brier scores' closes. Every
  number in the table above is unchanged.
* 2026-10-26 — schedule, not a rule (owner's decision): on college football
  Saturdays Pinnacle's daily decision snapshot is taken at 10:30 CT (11:30
  ET), the betting wake, instead of 2:30pm ET (`ace-sharp.py EARLY_DECISION`).
  Since the wake moved to 11:30 ET on 2026-10-03 those bets were measured
  against DraftKings, because Pinnacle's price came three hours later. The
  wake now runs the fetch first and waits for it, so the price is in place
  when he bets. Still one decision snapshot a sport a day; every number in
  the table above is unchanged.
* 2026-10-29 — not a rule change: a sixth no-AI bettor, `underdog`, joins the
  comparison (owner's decision, after `scripts/ace-study.py` showed Ace's
  blind chances squeezed toward 50-50, so his rule bets mostly underdogs). It
  takes the side DraftKings gives 38–48% on game day from 10:00 ET, flat $100.
  It was chosen after looking at this data, so for the 2026-12-01 read-out the
  AI's contribution is still measured against the five above; `underdog` is
  reported beside them from its first bet, on games after today only. Every
  number in the table above is unchanged.
* 2026-10-29 — a bug fix, not a rule change (owner's decision): a blind
  estimate names its team - `ace-judge.py blind "1:BUF:55"`, either team - and
  the script records the home team's chance from it. `1:55` was read as the
  home team's chance, and some went in the wrong way round: on 2026-10-04 all
  four NFL games on the sheet (15% for Baltimore at home, the market 85%), and
  a few NBA games. None became a bet - all were outside the 30–75% range - but
  a swap on a close game would look like an ordinary disagreement. A team not
  in that game is now refused and nothing is recorded. Gate 1 keeps every
  estimate already made; `scripts/ace-study.py` names the far-apart games.
  Every number in the table above is unchanged.
