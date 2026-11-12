#!/usr/bin/env python3
"""
check-provider-error.py <logfile> — was that a provider problem, not the agent?

Ace's 15:00 cycle failed and the verifier reported four missing deliverables.
All four were true and none of them mattered: the model never ran, because the
OpenRouter account had no credit. Without this check the next hour goes into
reading instructions and rewriting scripts for an agent that was never asked a
question.

Exits 2 and prints a diagnosis if the run hit a billing, auth or quota error.
Exits 0 otherwise, including when it cannot read the log - a missing log is
not evidence of a provider fault.

Standard library only.
"""

import re
import sys
from pathlib import Path

# Ordered: the first match wins, so put the specific ones first.
# (pattern, title, subtitle, what to do about it)
#
# The HTTP codes MUST carry context. A bare \b401\b matched `"schemaChars":
# 401,` in a tool-schema dump and reported a perfectly good API key as
# rejected - which would have had someone re-entering a working credential.
# Numbers that size are everywhere in this log; only "status 401" is evidence.
SIGNS = [
    # The Claude plan (scripts/wake.py). First, because its refusals also say
    # 401, and the OpenRouter advice below would send someone to re-enter a
    # key that was never the problem. Words from a real refusal:
    # tests/fixtures/claude-cli-result-401.json.
    (r"oauth (?:access )?token|no claude_code_oauth_token|claude cli is not installed",
     "CLAUDE PLAN LOGIN REJECTED", "this is configuration, not the agent",
     "The Claude plan did not accept this server's login, so no model ran.\n"
     "  Make a new token:   ~/.local/bin/claude setup-token\n"
     "  Store it:           python3 scripts/set-credential.py plan CLAUDE_CODE_OAUTH_TOKEN\n"
     "  Check it works:     python3 scripts/wake.py check"),
    # Not yet seen for real - the wording is the CLI's as last published.
    # The first real one should become a fixture.
    (r"usage limit reached|(?:5-hour|weekly|session) limit reached|limit reached\W{1,4}resets",
     "CLAUDE PLAN LIMIT REACHED", "this is the plan's allowance, not the agent",
     "The Claude plan's usage limit is spent until it resets. Your own Claude use\n"
     "  draws on the same allowance. Agents set to fall back to OpenRouter do so\n"
     "  by themselves; this one did not. See:  python3 scripts/wake.py engines"),
    # First, because it is the most specific. The owner capped the key at $1 a
    # day on OpenRouter (2026-09-25); once that is spent every call answers
    # "403 Key limit exceeded (daily limit)" until the cap resets at midnight
    # UTC. Unmatched, the run fell through to the verifier and read as an
    # agent that forgot its deliverables.
    (r"key limit exceeded|budget limit exceeded|spending limit exceeded",
     "DAILY SPENDING CAP REACHED", "this is the budget, not the agent",
     "OpenRouter refused the request because the key's spending cap is spent.\n"
     "  It resets at midnight UTC - 7pm Central - and the next wake after that\n"
     "  will run. Today's spend so far:  python3 scripts/budget.py\n"
     "  Nothing is wrong with this agent. If this keeps happening before the\n"
     "  agents' last wakes of the day, the daily cap is too tight for the\n"
     "  schedule - raise it on the key, or run fewer things by hand."),
    (r"billing error|insufficient balance|run out of credits|quota exceeded|"
     r"insufficient_quota|payment required|"
     r"(?:status|code|http)\D{0,8}402\b",
     "OUT OF CREDIT", "this is the provider, not the agent",
     "The provider refused the request because the account has no balance.\n"
     "  Top up:  https://openrouter.ai/credits\n"
     "  Nothing is wrong with this agent and nothing needs fixing in it. No\n"
     "  model was asked anything, so the cycle wrote nothing for want of an\n"
     "  answer, not for want of trying. Every other paid agent is failing the\n"
     "  same way right now; Fury is not, because it runs no model."),
    (r"invalid api key|incorrect api key|no auth credentials|"
     r"unauthorized\D{0,40}(?:api|key|token|credential)|"
     r"(?:api|key|token|credential)\D{0,40}unauthorized|"
     r"authentication.{0,20}fail|"
     r"(?:status|code|http)\D{0,8}401\b",
     "KEY REJECTED", "this is configuration, not the agent",
     "The provider rejected the API key.\n"
     "  Check it:  python3 scripts/check-credentials.py\n"
     "  Re-set it: python3 scripts/set-credential.py OPENROUTER_API_KEY"),
    (r"rate.?limit|too many requests|"
     r"(?:status|code|http)\D{0,8}429\b",
     "RATE LIMITED", "this is the provider, not the agent",
     "The provider is rate limiting this key. The next scheduled wake will\n"
     "  probably succeed; nothing to fix here."),
    (r"request timed out before a response|incomplete_turn|timeoutphase",
     "RAN OUT OF TIME", "the cycle was cut off mid-flight",
     "The cycle was still working when the timeout cut it off, so whatever it\n"
     "  had not written yet is missing for that reason. Check the tool-call\n"
     "  count first:  scripts/last-run.py ace\n"
     "  High count with no failures means it is doing the work one call at a\n"
     "  time - batch it. Raising the timeout hides that rather than fixing it."),
    (r"model not found|no endpoints found|unknown model|no allowed providers",
     "MODEL UNAVAILABLE", "this is configuration, not the agent",
     "The configured model was refused by the provider. Check the slug:\n"
     "  openclaw config get agents | grep -A3 model"),
]


def main():
    if len(sys.argv) < 2:
        return 0
    try:
        text = Path(sys.argv[1]).read_text(errors="replace")
    except Exception:
        return 0

    # A wake the plan refused and wake.py then sent through OpenClaw: the
    # refusal is history, and only what OpenClaw did after it is this run.
    marker = "sending this wake through OpenClaw instead"
    if marker in text:
        text = text.split(marker, 1)[1]
    low = text.lower()
    for pattern, title, subtitle, advice in SIGNS:
        m = re.search(pattern, low)
        if not m:
            continue
        # Show the provider's own words - they are usually clearer than mine.
        line = ""
        for ln in text.splitlines():
            if re.search(pattern, ln.lower()):
                line = ln.strip()[:300]
                break
        print("=" * 62)
        print(f"  {title} - {subtitle}")
        print("=" * 62)
        if line:
            print(f"  {line}")
            print()
        print(f"  {advice}")
        print("=" * 62)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
