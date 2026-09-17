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
| `schema-dump-401.log` | a tool-schema dump containing `401` as a VALUE | format captured from a real `last-run.py` dump; the `401` line is the one that caused the false positive |

`scripts/capture-fixtures.py` refreshes these from the live droplet and
redacts anything secret-shaped. Run it there, commit what it writes, and the
tests keep testing reality instead of my memory of it.

Still missing, because no raw sample has been captured yet:

* `openclaw config get agents.entries.<name>.model.primary` - preflight parses
  it, and the slug bug lived exactly there. The tests cover the pattern
  against known-good slugs, which is weaker than covering it against the real
  line.
