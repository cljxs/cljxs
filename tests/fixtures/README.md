# Fixtures

Real captured output, not invented samples. Every parser bug in this repo was
a format assumption - `home_ml` for `moneyline_home`, `\b401\b` against a
schema dump, a two-segment slug pattern against a three-segment slug - so a
fixture is only worth having if it is what the tool actually printed.

Provenance of each file, and how to refresh it:

| file | what it is | how it got here |
|---|---|---|
| `config-unset.txt` | `openclaw config get` on an unset path | captured from the droplet |
| `config-timeout-300.txt` | the same path once set | captured from the droplet |
| `systemd-show.txt` | `systemctl show` timestamp format | captured from the droplet |
| `ace-timeout.log` | the wake that timed out before any response | captured from the droplet |
| `espn-nfl-roster-gb.json` | ESPN's NFL team roster, with each athlete's `injuries` history | fetched from ESPN's public roster endpoint for Green Bay on 2026-09-24, trimmed to eight athletes; two in the active `offense` group are designated Out, which is the bug it guards |
| `espn-nfl-scoreboard-week.json` | ESPN's NFL scoreboard for the current week (week 3, 2026) | fetched from ESPN's public scoreboard endpoint on 2026-09-24, trimmed to four games including the neutral-site `BAL VS DAL`; the `tickets` blocks were removed - ticket-seller links that check-secrets rightly flags as token-shaped, and that nothing here reads |
| `schema-dump-401.log` | a tool-schema dump containing `401` as a VALUE | format captured from a real `last-run.py` dump; the `401` line is the one that caused the false positive |

`scripts/capture-fixtures.py` refreshes these from the live droplet and
redacts anything secret-shaped. Run it there, commit what it writes, and the
tests keep testing reality instead of my memory of it.

**A test must never restate a value that lives in a fixture.** Refreshing is
what these files are for, so a hardcoded copy of one of their values is a
time bomb with a date on it. `SystemdTimestamps.test_the_real_format` carried
`"2026-09-16 18:55:37"` as a string beside the fixture that said the same
thing; the fixture got refreshed on the droplet, the test failed with a diff
of two timestamps, and nothing in that diff said the FIXTURE had moved rather
than the parser. Assert the round trip - parse the captured line, print it
back, compare it to the line - and put fixed values only in tests that also
hold their own input.

Still missing, because no raw sample has been captured yet:

* `openclaw config get agents.entries.<name>.model.primary` - preflight parses
  it, and the slug bug lived exactly there. The tests cover the pattern
  against known-good slugs, which is weaker than covering it against the real
  line.
