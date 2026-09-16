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
SIGNS = [
    (r"billing error|insufficient balance|run out of credits|quota exceeded|"
     r"insufficient_quota|payment required|\b402\b",
     "OUT OF CREDIT",
     "The provider refused the request because the account has no balance.\n"
     "  Top up:  https://openrouter.ai/credits\n"
     "  Nothing is wrong with this agent and nothing needs fixing in it. No\n"
     "  model was asked anything, so the cycle wrote nothing for want of an\n"
     "  answer, not for want of trying. Every other paid agent is failing the\n"
     "  same way right now; Fury is not, because it runs no model."),
    (r"invalid api key|incorrect api key|unauthorized|authentication.{0,20}fail|\b401\b",
     "KEY REJECTED",
     "The provider rejected the API key.\n"
     "  Check it:  python3 scripts/check-credentials.py\n"
     "  Re-set it: python3 scripts/set-credential.py OPENROUTER_API_KEY"),
    (r"rate.?limit|too many requests|\b429\b",
     "RATE LIMITED",
     "The provider is rate limiting this key. The next scheduled wake will\n"
     "  probably succeed; nothing to fix here."),
    (r"model not found|no endpoints found|unknown model|no allowed providers",
     "MODEL UNAVAILABLE",
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

    low = text.lower()
    for pattern, title, advice in SIGNS:
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
        print(f"  {title} - this is the provider, not the agent")
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
