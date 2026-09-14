
## ⚠️ FINISHING A CYCLE — this overrides the step order above

On 2026-09-14 you ran a cycle and wrote a report, but `MEMORY.md` still read
"No cycles run yet". The memory log is how you avoid repeating yourself; an
empty one means every cycle starts blind.

**Write state FIRST.** As soon as you know what changes, write
`state/bankroll.json` — bankroll, open bets, settled bets, an incremented
`cycle_count` and `last_cycle_utc` — **before** you write anything else.

The order is now:

1. grade settled bets
2. study the context files
3. pass, or take the one bet that clears the bar
4. **write `state/bankroll.json`** ← before the report, not after
5. write the report
6. append one line to `MEMORY.md`

**Name the report by the slot you are actually in**, using ET: the 09:00 wake
is `reports/YYYY-MM-DD-morning.md`, 15:00 is `-afternoon.md`, 23:30 is
`-night.md`. Check the clock rather than guessing.

**End every cycle by stating these three lines:**

```
STATE:  <the path you wrote>  cycle_count=<n>
REPORT: <the path you wrote>
MEMORY: <the exact line you appended>
```

If you cannot write all three truthfully, the cycle is not finished — go back
and do the missing one. Name the files you actually wrote. Do not guess a path.

A cycle where you passed on everything still writes all three. "Nothing to do"
is a result, not a reason to skip the paperwork.
