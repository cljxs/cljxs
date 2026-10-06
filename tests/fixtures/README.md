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
| `espn-nfl-odds.json` | ESPN's odds listing for one game (LAC @ BUF): which sportsbook, and the link to its player props | fetched from ESPN's public core API on 2026-09-24; each item cut to provider, details and the propBets link - the `links` block of ticket and sportsbook URLs was removed for the same reason as the scoreboard's `tickets` |
| `espn-nfl-propbets.json` | that sportsbook's player props for the game, as ESPN serves them: line and opening line, no price, each prop sent twice (over and under) | page 1 of 2 fetched on 2026-09-24, trimmed to three players' full-game props plus the look-alikes the parser must skip - first-half totals, milestones, touchdown scorer |
| `emily-ask-user-timeout.log` | the stdout tail task-dispatcher logged for task #30: Emily's only tool call was `ask_user`, and the run was cut off at its 600-second limit (`paused`, `toolUse`) | transcribed from a screenshot of the droplet's `journalctl -u task-dispatcher` on 2026-09-28 - the dispatcher keeps only the last 1500 characters, and the first lines of that (a tool schema hash) were left off |
| `clipper-words-librivox.json` | faster-whisper `base.en` word timestamps for a 5-minute public-domain LibriVox reading ("Peach Blossom Shangri-La", archive.org `short_stories14_librivox`) | written by `clipper/transcribe.py`'s own provider on 2026-09-28, so it is exactly what Clipper stores - including that faster-whisper hands back numpy floats, which is why the provider now casts |
| `clipper-ebur128.log` | ffmpeg 6.1 `ebur128` per-frame log, 4 s of the same reading | captured 2026-09-28; it writes `M:-120.7` and `M: -16.0` depending on the number's width, which the parser must accept both of |
| `openclaw-agent-json-2026.9.log` | openclaw 2026.9.2's `--json` run as journald logs it: the run wrapped in `runId`/`status`/`summary`/`result`, with the model, usage, cost and turns in `result.meta.agentMeta` | transcribed from screenshots of `journalctl -u paul-cycle` and `last-run.py paul` after Paul's first dry cycle on 2026-10-05 (MiMo-V2.6-Pro). Numbers and nesting are real; the business name and email are replaced with `Example Bakery` / `owner@example.com`, `systemPromptReport` is cut to a few entries, and `status`/`summary` were not visible, so their values are placeholders |
| `dns-google-mx-nxdomain.json` | dns.google's DNS-over-HTTPS answer for an MX lookup on a domain that does not exist (`Status: 3`) | fetched 2026-10-06 for milkboxbakery.com, the address on Paul's first pitch; `paul.py mail_verdict` reads it as "would bounce" |
| `dns-google-mx-null.json` | the same lookup for example.com: a null MX (`0 .`, RFC 7505 - accepts no mail) | fetched 2026-10-06 |
| `dns-google-mx-gmail.json` | the same lookup for gmail.com: real mail servers | fetched 2026-10-06 |
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
