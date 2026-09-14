
## ⚠️ FINISHING A CYCLE — this overrides the step order above

On 2026-09-14 you wrote a report and a memory line but never updated
`state/portfolio.json`. It still read `cycle_count: 3` from three days earlier.
Nothing was lost only because you made no trade that day. Had a stop-loss
fired, the close would have existed in the report and nowhere else, and the
next cycle would have marked to market against a position you had already sold.

**Write state FIRST.** As soon as you know what changes, write
`state/portfolio.json` — cash, positions, trades, an incremented `cycle_count`
and `last_cycle_utc` — **before** you write anything else.

The order is now:

1. mark to market
2. exit checks, close what triggered
3. at most one new entry
4. **write `state/portfolio.json`** ← before the report, not after
5. write the report
6. append one line to `MEMORY.md`

**Name the report by the slot you are actually in.** The 09:35 ET wake is
`reports/YYYY-MM-DD-open.md`. The 15:55 ET wake is `-close.md`. Check the
clock rather than guessing — a 09:35 run was filed as `-close` once already.

**End every cycle by stating these three lines:**

```
STATE:  <the path you wrote>  cycle_count=<n>
REPORT: <the path you wrote>
MEMORY: <the exact line you appended>
```

If you cannot write all three truthfully, the cycle is not finished — go back
and do the missing one. Name the files you actually wrote. Do not guess a path.
