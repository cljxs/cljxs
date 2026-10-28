# Handoff: Ace, Belfort and Bolt (and their Coach)

Written 2026-10-21 by the Claude session that built most of this repo, for a
new chat that will look after the three paper-money agents: **Ace** (sports
bets), **Belfort** (stocks) and **Bolt** (crypto), plus the **Trading
Coach** that journals Belfort's and Bolt's trades. Read it top to bottom once;
the rest of the repo will make sense after.

- This repository, on the branch everything lives on:
  https://github.com/cljxs/cljxs/tree/claude/ai-agent-ecosystem-setup-0280zz
- This note as raw text (easiest for another AI to read):
  https://raw.githubusercontent.com/cljxs/cljxs/claude/ai-agent-ecosystem-setup-0280zz/HANDOFF-ACE-BELFORT-BOLT.md
- Read before changing anything: [CLAUDE.md](https://github.com/cljxs/cljxs/blob/claude/ai-agent-ecosystem-setup-0280zz/CLAUDE.md)
  (how to change things without breaking them) and
  [ROUTING.md](https://github.com/cljxs/cljxs/blob/claude/ai-agent-ecosystem-setup-0280zz/ROUTING.md)
  (which files, anchors and tests each agent touches - start there instead of
  reading the repo). In a Claude Code session on this repo both load by
  themselves.

The repository is **public**. Nothing secret is in it and nothing secret may
ever be put in it: no API keys, tokens, passwords, IP addresses, email
addresses, or live data from the agents (books, bets, reports are gitignored).

**All money here is paper.** No brokerage, exchange or sportsbook is
connected. Nothing in this repo can place a real order or wager, and nothing
should be added that could.

---

## 1. Who the owner is, and how to work with them

- **Not a programmer.** Explain in plain words; say what a timer, a commit
  or a "stop" does. Short answers with the outcome first.
- **Works from an iPad**: Termius (terminal) to reach the server, Safari for
  the web pages.
  - **Every command you give must be ONE line**, and give it in a code block
    to paste: the terminal flattens multi-line pastes, and the iPad keyboard
    turns `--` into a long dash when typed (they paste commands rather than
    type them).
  - Pages must work on a tablet and a phone.
- **They like to choose.** For anything sizeable, give 2-4 options with a
  recommendation. They often bring a prompt or idea from another AI (or a
  friend's bot, "Trev") and want your honest view of it, not agreement.
- **Money matters.** Say what something costs before building it, build caps in.
- After each piece of work they expect: tested, committed and pushed to the
  branch above, then the single-line commands to run on the server.
- **Say "verified" (you ran it and read the output) or "believed" (it should
  work)** - every bad surprise here came from believed presented as verified.
- When they decline something ("No not right now"), do not raise it again
  unprompted.

---

## 2. The ecosystem, briefly

One Ubuntu server (a DigitalOcean droplet), repo cloned at `/root/ecosystem`.
Each agent wakes on a **systemd timer**, does its work by **calling Python
scripts** in `scripts/`, and **a verifier script judges the cycle by what is
on disk**, not by what the agent says.

Other agents (not yours, but they share the server, the budget and the web
app): Peter (NFL/NBA props), Scout and Emily (Etsy shop), Paul (local-business
websites), Clip (video clips), Stan, the Agent Room, Fury and the GM. ROUTING.md
has a row for each.

**Which engine wakes a model** is `tasks/engines.json`, read only by
`scripts/wake.py`. Ace and Belfort run on **Haiku on the owner's Claude plan**
(`claude -p`, max 40 turns); if the plan refuses a wake before it starts it
falls back to OpenClaw on OpenRouter. Bolt's news book asks the plan one
question at a time (Haiku, 1 turn, no fallback). The Coach asks Sonnet one
question a week (no fallback). `python3 scripts/wake.py engines` shows what
runs where; `python3 scripts/wake.py check` tests the plan's login.

**The Command Deck** is the web app: `mission-control-api/` (Node/Express,
port 3001, reachable from the iPad over Tailscale). The JavaScript holds no
rules - each route runs the Python script that owns the answer and returns its
JSON. Pages and their village houses:

| Page | Module | Page file | Agent |
|---|---|---|---|
| `/ace` | `mission-control-api/ace.js` | `public/ace.html` | Ace |
| `/markets` | `mission-control-api/belfort.js` | `public/markets.html` | Belfort |
| `/bolt` | `mission-control-api/bolt.js` | `public/bolt.html` | Bolt (both books) |
| `/coach` | `mission-control-api/coach.js` | `public/coach.html` | Trading Coach |

`/village` is a pixel-art map; each agent's house opens its page in a frame
(`mission-control-api/public/village.html`: `const NAV`, `const FRAMES`).
A changed `public/*.html` needs only a reload; a changed `.js` module needs
`python3 scripts/restart-deck.py` (never `systemctl restart` - it kills jobs
the Deck started).

**Free, always-safe checks** (no AI woken, no money):

    python3 scripts/preflight.py      could each agent run? (files, units, model, credit)
    python3 scripts/health-check.py   did it run? (systemd vs disk vs Deck)
    python3 scripts/budget.py         today's OpenRouter spend vs the cap
    python3 scripts/last-run.py <agent>   what the agent actually did on its last wake

**Money for AI:** OpenRouter's shared daily budget is $1.50 (`tasks/limits.json`);
the agents on the Claude plan spend none of it unless they fall back. Cost
scales with **turns**, not tokens - a loop is the most expensive bug there is.

---

## 3. Ace - paper sports bets (NBA, NFL, college football)

$10,000 paper bankroll, flat **$100** bets, max 4 open. **Code keeps the
money** (`scripts/ace-book.py`); Ace never writes a number into the bankroll.

**The scientific frame - do not break it.** `agents/ace/PREREGISTRATION.md`,
frozen **2026-10-01 until 2026-12-01**: what is measured and the pass/fail
lines, written before the results so they cannot be fished. **Changing a
betting rule before 2026-12-01 restarts the clock** (results under two rule
sets cannot be added). Only bug fixes, each logged in that file with its date.
If the owner asks for a rule change, tell them this first and let them choose.

- **Gate 1 - blind forecasts:** before seeing any price, Ace gives every game
  on `data/blind.json` the home team's chance. Brier score vs DraftKings'
  no-vig chance, after 300 finished games. Fail = Ace is a data tool, not a
  forecaster.
- **Gate 2 - closing-line value (CLV):** did the price taken beat the close,
  Pinnacle's especially. 150 bets for a first read, 200 for a verdict.
- **Five no-AI bettors** (`scripts/ace-baselines.py`) bet the same games by
  fixed rules; if the mechanical fresh-news bettor matches Ace, the AI adds
  nothing.
- **Stops:** fault stop at -30% of the start; evidence stop after 60 bets with
  average CLV at or below zero. Both refuse new bets; the owner decides.

**How bets are made since 2026-10-12:** the *math rule* bets from the blind
estimates - the estimate is pulled 60% toward the market, must still clear it
by 2 points (or 3%) with positive expected value, sides priced 30-75% only.
`ace-judge.py blind` places them the moment the last estimate is in. News is
optional. (Under the old cited-news rule there was not one bet in a month.)
A **focus day** (`scripts/ace-focus.py`) points Ace at one sport and a short slate.

| Timer (Eastern) | Runs | Costs |
|---|---|---|
| `ace-fetch` every 30 min 09:00-23:30 | `scripts/ace-fetch.py`: ESPN data, grades finished bets, runs the no-AI bettors, writes the blind sheet | nothing |
| `ace-close` every 5 min 10:00-23:55 | `ace-fetch.py --close`: re-prices games starting within 2 h (the closing line) | nothing |
| `ace-cycle` 11:30 and 23:30 | `scripts/ace-cycle.sh` -> wake.py -> Ace, then `scripts/ace-verify.py` | 2 plan wakes a day |

**Ace Props (2026-10-26, owner, from a TikTok - option A):** `scripts/ace-props.py`
bets NBA player points milestones (DraftKings' 15+/20+/25+ ladder, free through
ESPN) by plain code: opponent bottom 10 at the player's position (G/F/C, last
40 games), his hit rate against those defences shrunk 60% toward the price,
10+ games, -400 to +400, +2 pts. At most 2 a day, $100, page `/ace-props` (a
button on Ace's page, and in the village menu). Its bets go into Ace's
`bets.jsonl` as `book: "props"`: his bankroll, none of his measures (cap, fault
stop, CLV, baselines, coach) - `ace-book.py is_props`. Own stop at -$1,000.
Logged in the preregistration as not a rule change. `ace-props.py show`.

Scripts: `ace-judge.py` (blind/list/bet/pass/rest/verdict/mark - Ace's only
way to record judgements), `ace-book.py` (the money, from `state/bets.jsonl`),
`ace-clv.py` (CLV and Brier; `closing` decides what counts as a close: the last 60 minutes before the start and after the decision it grades - logged as a bug fix 2026-10-26), `ace-sharp.py` (Pinnacle prices from The Odds
API; key `ODDS_API_KEY` via `scripts/set-credential.py ace ODDS_API_KEY`;
`ace-sharp.py budget` shows credits; the daily decision snapshot is 2:30pm ET, but 10:30 CT on college football Saturdays - `EARLY_DECISION`, owner 2026-10-26 - and `ace-cycle.sh` runs the fetch and waits for it before every wake), `ace-baselines.py`. Data: ESPN's public
API through `site.web.api.espn.com` (the usual `site.api.espn.com` host is
blocked from datacentre IPs). Rules for the model:
`agents/ace/_ace-agents-header.md`. Tests: `tests/test_ace_board.py`, and
`tests/test_parsers.py` classes starting `Ace`.

---

## 4. Belfort - paper swing trader (stocks)

$10,000 paper portfolio, a watchlist of 79 names in 14 groups (chips, software,
space, quantum...). The model decides *what* and *why*; **every change to
the book goes through `scripts/belfort-trade.py`**, which does all the money.
This exists because a hand-kept book once read **-41%** when the real loss
was under 4% (sales never credited to cash).

| Timer (Eastern, Mon-Fri) | Runs | Costs |
|---|---|---|
| `belfort-fetch` every 10 min 09:00-16:50 | `scripts/belfort-fetch.py`: quotes, indicators, news, earnings, candidates | nothing |
| `belfort-exits` every 10 min 09:45-15:55 (since 2026-10-22) | `belfort-trade.py exits --apply`: the sell rules, by code | nothing |
| `belfort-cycle` 09:35, 12:00 (since 2026-10-22) and 15:55 | `scripts/belfort-cycle.sh`: exits, then wake.py -> Belfort, then `scripts/belfort-verify.py` | 3 plan wakes a trading day |
| `belfort-review` 1st of the month 08:05 | `scripts/belfort-learn.py review --write`: the monthly review of his record | nothing |

**Exits (code, before every wake and every 10 minutes):** each position's own stop
(2x its average daily move under the price paid, 5-15%; older ones -10%);
take half at +20% and let the rest run; once +8%, the stop trails the best
close by 8%, never below cost (owner, 2026-10-22, from the replay: the trail
was 10% or the stop's own distance, and half was sold at +25%); dead money (within 2% after 8 trading days and behind QQQ)
sold. Only a **broken thesis** is the model's own exit.

**Entries (model, at most one a wake):** trend (price > SMA20 > SMA50), RSI
40-65 with MACD positive, a **catalyst cited by headline id** from that
name's news under 7 days old, written as `TYPE: fact`, and a score of 7+/10.
Code refuses: over 25% in one name, under 5% cash, over 40% in a cluster,
more than 8 names, buying in an unfavourable market regime (QQQ below its falling 50-day
average - QQQ alone since 2026-10-28; `BREADTH` in belfort-trade.py, and
`belfort-learn.py blocked` measures each breadth on his passes), a name reporting earnings within 5 trading days, or more than a
1% portfolio risk at the stop (`belfort-trade.py size SYMBOL` gives the most
allowed). He also writes a score and reason for every candidate he passed on
(`state/calls.txt` -> `belfort-trade.py calls`) - the owner reads those on
the Markets page. **A no-AI "shadow" book** trades the same candidates by the
mechanical rules beside him (`belfort-trade.py shadow`); it has done worse
than Belfort, which is what prompted Bolt's news book. `belfort-learn.py`
gives him his record (by catalyst type and score) as evidence, never as a rule
change - rules change only when the owner changes them.
`python3 scripts/belfort-learn.py replay` (2026-10-22) walks every position he has
held through real daily prices under the sell rules now and before 2026-10-22
(`--trail N` tries any trail); it changes nothing.

**Reddit chatter (owner, 2026-10-26)**: `belfort-cycle.sh` puts a REDDIT block
in his wake message - `scripts/reddit_search.py belfort`, the names he holds
and weighs, read through web search once a day (Reddit's own API is closing).
Background only: a buy still cites a news.json headline, checked by code. Ace
gets none until after his 2026-12-01 read-out (his inputs are frozen).
**OFF since 2026-10-26**: Claude's web search cannot reach reddit.com at all, so
`reddit_search.ENABLED` is False and nothing is asked until another search is chosen.

**Three ideas from TradingAgents (owner approved 2026-10-26)**, all for
grading, none changes a rule:
* **Beyond SPY.** Every buy, sale and call keeps SPY's price (`spy_now`), and
  `closed_trades` gives each trade `spy_pct` and `vs_spy` (`spy_move`; older
  trades use SPY's closes on the two days). His wake record and the monthly
  review show it.
* **The case against.** After his wake, `belfort-cycle.sh` runs
  `belfort-trade.py bear`: each buy of that wake is put to his own model on
  the plan as one question (worry 1-5, a risk, why), using only what was known
  when he bought. It's kept on the BUY trade, and he never sees it. Unanswered
  questions are written down, not retried. `bear --test` asks about his latest
  buy and writes nothing.
* **Point in time.** `pass_outcomes` measures only on final closes
  (`et_time.final_bar_day`): the 10-day marks were being read at the 09:35
  wake from today's half-formed bar. A call with no saved price is based on
  the last close before it. Rows measured before the fix are measured again
  (`MEASURE_VERSION`). The Coach's "early exit" reads the close on the day of
  the sale, not the price whenever the journal ran.
The Coach shows all three: vs SPY and the case against on each journal row,
"Against the market", "Case against" patterns and "Names he passed on" (got
away / dodged) in the daily analysis. The Sunday question gets `against_spy`
and `passed_on` too.

Rules for the model: `agents/belfort/_belfort-agents-header.md`. Tests:
`tests/test_parsers.py` classes starting `Belfort` and `TheMarketsPage`.

---

## 5. Bolt - paper crypto, plain code (two books)

**No model trades Bolt** (`agents/bolt/NO_MODEL`): every rule is a formula in
`scripts/bolt.py`, run by `bolt-cycle` **every 10 minutes**, free.

- Ten coins (BTC, ETH, SOL, BNB, XRP, ADA, AVAX, LINK, DOT, LTC), one
  CoinGecko call a tick; 20- and 50-hour averages from 7 days of hourly prices.
- **Buys** every 1.5 hours (00:00, 01:30 ... UTC): at most one coin, the best
  scoring 7+ on a 10-point rubric, 15% of the account, keeping 5% cash; no buy
  of a coin up 20%+ on the day.
- **Exits, every tick (swing exits since 2026-10-19):** stop -8%; trailing stop
  8% under the best price since buying (only rises, no fixed target); 14-day
  time limit; BTC down 5% on the day sells every other coin. Fills at the
  tick's price with a 0.1% fee each way.
- Down 15%: no buys for 24 hours, then he restarts counting by himself.
- **Bolt + News, a second $10,000 book** (2026-10-16): the same rules and exits,
  but before a buy the coin's last 48 hours of headlines (CoinDesk,
  Cointelegraph, The Block, Decrypt) go to Haiku on the Claude plan as one
  question; code buys only on a 7+ that cites one of those headlines, with no
  bad news. At most **5 questions an Eastern day** (`NEWS_WAKES_PER_DAY`).
  Exits never wait for a model. The two books side by side say whether news
  helps crypto.

Commands: `python3 scripts/bolt.py show` (the book in words), `check` (does it
balance?), `board` (the screen's JSON). Pause: `systemctl stop bolt-cycle.timer`.
Tests: `tests/test_parsers.py` classes starting `Bolt`.

The owner tested a "7-day buy filter" for Bolt (2026-10-19) and said **"No
not right now"** - do not re-propose it unprompted.

---

## 6. The Trading Coach (Belfort and Bolt)

`scripts/coach.py`, page `/coach`. From the owner's own prompt (2026-10-19):
journal every trade, analyse daily **without changing any rule**, and every
Sunday propose STOP / START / CONTINUE / updated rules.

- **The journal and the daily analysis are code** (`coach-daily`, 00:20 ET):
  every trade read from the agents' own books, and the mistakes a script can
  see (a loss past the stop, a buy under the bar, chasing, an early exit, a
  gain given back), each with its number. Belfort is judged against his widest
  stop (15%).
- **The weekly review** (`coach-weekly`, Sunday 20:10 ET) asks Sonnet on the
  plan one question with the week's measured numbers and the current rules.
  Fewer than 5 closed trades in the week: no proposals (noise). The answers
  are **proposals only**, saved for the owner; nothing edits a rule. The
  first real Sunday review is **2026-10-25**.
- **Ace too (2026-10-22, owner's option A):** his settled bets are journaled
  from `ace-book.py`, each with its edge (`ace-judge.py bar`) and closing-line
  value (`ace-clv.py headline_clv`). The daily mistakes for him are "beaten by
  the close" and "bet under the bar" - never a plain loss. Until
  `ace-judge.py FROZEN_UNTIL` (2026-12-01) the Sunday review only comments on
  him, under its own heading, and proposes nothing; after it he is reviewed
  like the others. `coach.py backfill --book ace --since YYYY-MM-DD` journals
  his past days and keeps every other book's saved rows.
- **Three coaches since 2026-10-23** (owner: "all separate from each other"):
  Belfort, Bolt (both books together - they are one comparison) and Ace each
  keep their own files in `coach/<name>/journal` and `coach/<name>/weekly`,
  and each Sunday question sees only its own agent's numbers and rules
  (`COACHES`, `rules_for`). The one combined journal was split once by
  `coach.py split` (it also runs by itself); the old files are in
  `coach/combined-before-split/`. The Coach page has a Belfort / Bolt / Ace switch.
- Commands: `python3 scripts/coach.py journal`, `daily`, `weekly --dry` (each takes `--coach`), `backfill`, `split`, `show --json`.
  Tests: `tests/test_coach.py`.

---

## 7. The rules this ecosystem paid for (apply them here)

1. **A model will claim work it did not do.** Every cycle is judged by a
   verifier reading the disk (`ace-verify.py`, `belfort-verify.py`,
   `scripts/signoff.py`). Never trust a report's prose.
2. **Anything a script can compute exactly belongs in a script.** Prices,
   P&L, stakes, sizes, CLV, stops - code. Bolt has no model at all for this reason.
3. **One fact, one place.** `belfort-verify.py` imports `book_gap` from
   `belfort-trade.py` rather than restating it. Import, never copy.
4. **Times are Eastern, stored as UTC.** `scripts/et_time.py` owns the clock and
   the wake slots, and `_meta.json`'s `report_name` is the report filename -
   agents filed reports under the wrong date twice by reading the UTC clock.
5. **Exits are code, not the model's discretion** - Belfort's sell rules were
   only followed once they moved into code.
6. **Tests use real captured data** (`tests/fixtures/`: ESPN payloads,
   `_meta.json`, CoinGecko replies). Every test names the bug it guards and was
   checked by putting the bug back. Run before a push:
   `python3 -B -m unittest tests.test_parsers -b` (fast) and
   `python3 scripts/check-secrets.py`; for money-touching changes, the full
   `python3 -B -m unittest discover -s tests -b` too (about 40 minutes - run it
   in the background).
7. **Changing an agent's instructions:** edit `agents/<name>/_<name>-agents-header.md`
   (AGENTS.md is generated), then on the server `scripts/deploy.sh <name>`.
8. **Credentials never leave the server.** `agents/<name>/state/credentials.env`,
   mode 600, entered by the owner with `scripts/set-credential.py`. Never ask
   for a key in chat; never print one.
9. **Never `pkill -f` a pattern** on the server - it has killed the session
   running it. Stop processes by PID.
10. **Every commit ends with the session's attribution lines; no model names in
    commits, code or pull requests. Pull requests only when the owner asks.**

---

## 8. Open threads (check with the owner; some may already be done)

- **Coach deploy:** `scripts/deploy.sh coach` on the server, and the first
  journal backfill, were given to the owner on 2026-10-20 - confirm the
  `coach-daily` and `coach-weekly` timers exist (`systemctl list-timers | grep coach`).
- **Bolt's swing exits** (2026-10-19) take effect on `git pull` - `bolt.py show`
  shows the trailing stop on each position when they are live.
- **First Coach weekly review: Sunday 2026-10-25.** Read it with the owner; it
  proposes, the owner decides, a rule changes only by a commit.
- **Ace's preregistration runs to 2026-12-01.** Plan the read-out of Gate 1
  (300 games) and Gate 2 (150-200 bets) with `python3 scripts/ace-clv.py` and
  `python3 scripts/ace-baselines.py report` when the samples are there.
- A model-trial comparison exists (`scripts/model-trial.py`) if the owner asks
  whether a different model would do better; cost scales with turns.

---

## 9. Links

- Repo (branch): https://github.com/cljxs/cljxs/tree/claude/ai-agent-ecosystem-setup-0280zz
- CLAUDE.md: https://github.com/cljxs/cljxs/blob/claude/ai-agent-ecosystem-setup-0280zz/CLAUDE.md
- ROUTING.md: https://github.com/cljxs/cljxs/blob/claude/ai-agent-ecosystem-setup-0280zz/ROUTING.md
- Ace's preregistration: https://github.com/cljxs/cljxs/blob/claude/ai-agent-ecosystem-setup-0280zz/agents/ace/PREREGISTRATION.md
- The agents' rules: `agents/ace/_ace-agents-header.md`, `agents/belfort/_belfort-agents-header.md`, `agents/bolt/README.md`
- ESPN's public API (unofficial): `site.web.api.espn.com`; The Odds API: https://the-odds-api.com
- CoinGecko API: https://docs.coingecko.com
- Claude Code on the web: https://code.claude.com/docs/en/claude-code-on-the-web
