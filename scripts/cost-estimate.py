#!/usr/bin/env python3
"""
cost-estimate.py — what a cycle costs, before you pay for it.

    python3 scripts/cost-estimate.py emily --turns 12
    python3 scripts/cost-estimate.py --prompt-tokens 8000 --turns 12
    python3 scripts/cost-estimate.py --schedule

Every number here is arithmetic, and this repo's rule is that a model asked to
do arithmetic will eventually claim it did. So the rates live in a table, the
prompt size is measured off disk, the wake count is read from the timers, and
the multiplication happens here where it can be checked.

WHY TURNS DOMINATE. Every turn re-sends the whole conversation, so a cycle's
input is not `prompt` - it is `prompt` once per turn, plus the transcript
built so far:

    input tokens = turns*prompt + growth * turns*(turns-1)/2

The second term is quadratic. A 68-turn cycle does not cost five times a
12-turn cycle; at these sizes it costs more like thirty. That is why fixing a
loop saves more than switching models, and the --turns number is the one worth
measuring properly.

RATES ARE ANTHROPIC FIRST-PARTY LIST, read 2026-09-18 - and checked the same
day against OpenRouter's own /api/v1/models, which is what these agents
actually buy through: it charges Anthropic list for the Opus and Sonnet rows,
no margin. That was a caveat here until it was checked; it is now a fact with
a date on it. Re-check it rather than trusting this line a year from now:

    python3 scripts/cost-estimate.py --rates opus

THE CACHED COLUMN IS STILL A BEST CASE. OpenRouter carries the price, but
whether OpenClaw sends cache_control on an agent wake is unverified, and
without it nothing caches. Read that column as the ceiling on what caching
could save, not as a discount you are getting.

TOKEN COUNTS ARE ESTIMATED FROM CHARACTERS. The only true count comes from the
provider's own usage field on a real call. This is a planning number, not a
bill.

Standard library only.
"""

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))

# Anthropic first-party list prices, dollars per million tokens, read from
# Anthropic's pricing on 2026-09-18. cache_read/cache_write are multipliers on
# the INPUT rate: a read is 0.1x base, a write is 1.25x at the default
# 5-minute TTL. cache_min is the shortest prefix that will cache at all -
# below it the marker is silently ignored.
PRICES = {
    "claude-opus-5":   {"in": 5.00, "out": 25.00, "cache_read": 0.1,
                        "cache_write": 1.25, "cache_min": 512},
    "claude-sonnet-5": {"in": 2.00, "out": 10.00, "cache_read": 0.1,
                        "cache_write": 1.25, "cache_min": 1024},
    "claude-haiku-4-5": {"in": 1.00, "out": 5.00, "cache_read": 0.1,
                         "cache_write": 1.25, "cache_min": 4096},
}

# Roughly four characters to a token for English prose. Good enough to choose
# a model, not good enough to predict an invoice - see the docstring.
CHARS_PER_TOKEN = 4

# What a turn adds to the transcript that the next turn must re-read: the
# model's own output plus whatever a tool handed back. Tool results are the
# bigger half in this ecosystem - an ESPN payload, a variants list, a config
# dump - which is why it defaults higher than the output estimate.
DEFAULT_OUTPUT_TOKENS = 400
DEFAULT_TOOL_RESULT_TOKENS = 600


def prompt_tokens_for(agent):
    """The real prompt this agent wakes with, measured off disk.

    AGENTS.md is generated and untracked, so on a checkout without a droplet
    this falls back to the header it is generated from and says so.
    """
    built = ROOT / "agents" / agent / "AGENTS.md"
    if built.is_file():
        return len(built.read_text()) // CHARS_PER_TOKEN, str(built.relative_to(ROOT))
    header = ROOT / "agents" / agent / f"_{agent}-agents-header.md"
    if header.is_file():
        return (len(header.read_text()) // CHARS_PER_TOKEN,
                f"{header.relative_to(ROOT)} (AGENTS.md not built here - "
                f"the real prompt is larger)")
    return None, None


def cycles_per_week():
    """How often each agent wakes a model, read from the timers.

    The schedule is a fact that lives in deploy/*.timer. Restating it here is
    how it would go stale the first time one changed.
    """
    out = {}
    for unit in sorted((ROOT / "deploy").glob("*-cycle.timer")):
        agent = unit.name[:-len("-cycle.timer")]
        service = unit.with_suffix(".service")
        # fury-cycle runs fury-collect.py and wakes no model at all; a timer
        # is not a bill unless something behind it calls one.
        wakes_model = False
        if service.is_file():
            # An ExecStart can run to several lines with trailing backslashes.
            # Matching only lines that START with ExecStart read timmy's
            # inline unit as waking no model, because the openclaw call was on
            # a continuation line. Join them before looking.
            joined = service.read_text().replace("\\\n", " ")
            exec_line = " ".join(l for l in joined.splitlines()
                                 if l.startswith("ExecStart"))
            script = re.search(r"(\S+\.sh)", exec_line)
            if script:
                sh = ROOT / "scripts" / Path(script.group(1)).name
                wakes_model = sh.is_file() and "openclaw" in sh.read_text()
            else:
                wakes_model = "openclaw" in exec_line
        if not wakes_model:
            out[agent] = 0
            continue

        per_week = 0
        for line in unit.read_text().splitlines():
            if not line.startswith("OnCalendar="):
                continue
            spec = line.split("=", 1)[1]
            days = 5 if "Mon..Fri" in spec else 7
            # "09..23:00/30" means every 30 minutes across a range of hours -
            # a fetch shape. Cycle timers are single times, counted as one.
            per_week += days
        out[agent] = per_week
    return out


def rate_rows(models, term):
    """[(id, in, out, cache_read, cache_write)] per million tokens, matching term.

    Kept out of main() so it can be run against a captured payload. The prices
    arrive as strings of dollars PER TOKEN, and some are null - gpt-4o-mini has
    no input_cache_write at all - so every field is converted defensively. A
    null is not a zero: a zero would read as "caching is free here".
    """
    def per_million(pricing, key):
        raw = (pricing or {}).get(key)
        if raw in (None, ""):
            return None
        try:
            return float(raw) * 1_000_000
        except (TypeError, ValueError):
            return None

    out = []
    for m in models:
        mid = str(m.get("id") or "")
        if term.lower() not in mid.lower():
            continue
        pricing = m.get("pricing") or {}
        in_r = per_million(pricing, "prompt")
        out_r = per_million(pricing, "completion")
        if in_r is None or out_r is None:
            continue  # a model with no headline price cannot be compared
        out.append((mid, in_r, out_r,
                    per_million(pricing, "input_cache_read"),
                    per_million(pricing, "input_cache_write")))
    return sorted(out)


def cycle_cost(model, prompt, turns, output, growth, cached):
    """(input tokens, output tokens, dollars, why-not) for one cycle.

    Turn i re-sends the prompt plus everything said so far, so the input is
    turns*prompt + growth*turns*(turns-1)/2 - quadratic in turns, which is the
    whole point.

    `cached` means "use caching where it pays", not "pay the cache premium
    regardless". Caching is a choice, and there are two cases where taking it
    costs more than leaving it off: a prompt under the model's minimum, where
    the marker is silently ignored, and a single-turn cycle, which pays the
    1.25x write and never reads it. Showing a cached price above the plain one
    would be reporting a saving that is a loss; showing the plain price with
    no explanation would hide why. So the reason comes back with the number.
    """
    rate = PRICES[model]
    transcript = growth * turns * (turns - 1) // 2
    out_tokens = output * turns
    in_tokens = prompt * turns + transcript
    plain = (in_tokens * rate["in"] + out_tokens * rate["out"]) / 1_000_000

    if not cached:
        return in_tokens, out_tokens, plain, ""

    if prompt < rate["cache_min"]:
        return in_tokens, out_tokens, plain, (
            f"prompt is under this model's {rate['cache_min']}-token minimum")

    billed_in = (prompt * rate["cache_write"]
                 + prompt * rate["cache_read"] * (turns - 1)
                 + transcript)
    dollars = (billed_in * rate["in"] + out_tokens * rate["out"]) / 1_000_000
    if dollars >= plain:
        return in_tokens, out_tokens, plain, "one turn writes a cache nothing reads"
    return in_tokens, out_tokens, dollars, ""


def main():
    ap = argparse.ArgumentParser(description="what a cycle costs")
    ap.add_argument("agent", nargs="?", help="measure this agent's real prompt")
    ap.add_argument("--prompt-tokens", type=int, dest="prompt",
                    help="instead of measuring an agent")
    ap.add_argument("--turns", type=int, default=12,
                    help="model turns in a cycle (default 12) - measure this")
    ap.add_argument("--output", type=int, default=DEFAULT_OUTPUT_TOKENS,
                    help=f"output tokens per turn (default {DEFAULT_OUTPUT_TOKENS})")
    ap.add_argument("--tool-result", type=int, default=DEFAULT_TOOL_RESULT_TOKENS,
                    dest="tool_result",
                    help=f"tokens a tool hands back per turn "
                         f"(default {DEFAULT_TOOL_RESULT_TOKENS})")
    ap.add_argument("--rates", metavar="TERM",
                    help="ask OpenRouter what it charges today for slugs "
                         "matching TERM, instead of trusting the table above")
    ap.add_argument("--schedule", action="store_true",
                    help="how often each agent wakes a model, from the timers")
    a = ap.parse_args()

    if a.rates:
        # The table has a date on it, and a dated number is a number that goes
        # stale. This is how you check it without pasting a fragile one-liner.
        import json
        import urllib.request
        try:
            with urllib.request.urlopen(
                    "https://openrouter.ai/api/v1/models", timeout=30) as r:
                models = json.load(r)["data"]
        except Exception as exc:
            print(f"could not reach OpenRouter: {exc}", file=sys.stderr)
            return 1
        rows = rate_rows(models, a.rates)
        if not rows:
            print(f"nothing in OpenRouter's catalogue matches {a.rates!r}",
                  file=sys.stderr)
            return 1
        print(f"OpenRouter slugs matching {a.rates!r} - this is what you would "
              f"pay, and\nthe id column is the slug for set-agent-model.sh "
              f"(prefixed openrouter/):\n")
        for mid, in_r, out_r, c_read, c_write in rows:
            cache = ""
            if c_read is not None and in_r:
                cache = f"   cache read {c_read / in_r:.2f}x"
                if c_write is not None:
                    cache += f", write {c_write / in_r:.2f}x"
                else:
                    cache += ", write not priced"
            print(f"  {mid:<40} in ${in_r:>7.2f}/M   out ${out_r:>7.2f}/M{cache}")
        return 0

    if a.schedule:
        weekly = cycles_per_week()
        print("Model-waking cycles per week, read from deploy/*-cycle.timer:\n")
        for agent, n in sorted(weekly.items()):
            note = "  (wakes no model)" if n == 0 else ""
            print(f"  {agent:<10} {n:>3}/week{note}")
        print(f"\n  {'total':<10} {sum(weekly.values()):>3}/week")
        print("\nEmily has no cycle timer - she is woken by the task dispatcher, "
              "so her count is however many tasks you approve.")
        print("This is the schedule in deploy/. What is actually armed on the "
              "droplet is\n`systemctl list-timers` - scripts/health-check.py "
              "owns that question.")
        return 0

    if a.prompt:
        prompt, source = a.prompt, "given on the command line"
    elif a.agent:
        prompt, source = prompt_tokens_for(a.agent)
        if prompt is None:
            print(f"no agent called {a.agent!r} in {ROOT/'agents'}", file=sys.stderr)
            return 1
    else:
        ap.error("name an agent, or give --prompt-tokens")

    growth = a.output + a.tool_result
    print(f"prompt      ~{prompt:,} tokens   ({source})")
    print(f"turns        {a.turns}")
    print(f"per turn     {a.output} out + {a.tool_result} tool result "
          f"= {growth} added to the transcript\n")

    width = max(len(m) for m in PRICES)
    print(f"  {'model':<{width}}  {'per cycle':>10}  {'cached':>10}   "
          f"{'in/out tokens':>18}")
    for model in PRICES:
        in_t, out_t, plain, _ = cycle_cost(model, prompt, a.turns, a.output,
                                           growth, cached=False)
        _, _, cheap, why = cycle_cost(model, prompt, a.turns, a.output,
                                      growth, cached=True)
        note = f"  ({why})" if why else ""
        print(f"  {model:<{width}}  ${plain:>9.4f}  ${cheap:>9.4f}   "
              f"{in_t:>8,} / {out_t:<7,}{note}")

    print(f"\n  Rates are Anthropic list, read 2026-09-18 and confirmed the same "
          f"day to be\n  what OpenRouter charges for these models - that is "
          f"where these agents buy.\n  Whether OpenClaw sends cache_control on "
          f"a wake is unverified, so read the\n  cached column as a ceiling, "
          f"not a discount. Token counts are estimated at\n  {CHARS_PER_TOKEN} "
          f"chars/token - a true count only comes from a provider's usage field.")
    print(f"\n  Halve the turns and you halve more than the cost: "
          f"{a.turns} turns carries "
          f"{growth * a.turns * (a.turns-1) // 2:,} tokens of transcript, "
          f"{(growth * (a.turns//2) * ((a.turns//2)-1) // 2):,} at {a.turns//2}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
