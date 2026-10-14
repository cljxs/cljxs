#!/usr/bin/env python3
"""
model-trial.py — how each model actually did, per agent, from the journal.

    model-trial.py report                 every model-waking agent
    model-trial.py report ace belfort     just these
    model-trial.py report --days 30 --json

WHY. Trying a cheaper or stronger model on an agent is one command
(scripts/set-agent-model.sh). Knowing whether it was BETTER is the hard part,
and the answer is in the runs, not the price list: a model that takes 68
turns where another takes 12 costs more however cheap its tokens are, and one
that stops halfway saved nothing (Ace on gpt-4o-mini, Emily on every Gemini
tried). So this reads every wake OpenClaw recorded in the journal and puts
the models side by side on what they did: runs, how many ended "ok", tool
failures, turns, minutes, and dollars - OpenRouter's own billed figure.

"cached" is how much of what the model read came from the cache. Most of
a run is re-reading what was already sent, and the cache prices that at a
tenth of the input price or less - so a model with a cached price that
shows ~0% here is being billed full price for every re-read, and costs
several times what its price list suggests.

Read-only and free: no model is woken. It reads what OpenClaw printed;
whether the work was RIGHT is still each agent's verifier's call (preflight
and health-check), so a model that finishes "ok" and writes nothing is
caught there, not here.

Uses last-run.py's journal reader - the one place that knows the shape
OpenClaw prints a run in.

Standard library only.
"""

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
AGENTS = ("ace", "belfort", "paul", "scout")
# The first line each <agent>-verify.py prints is its verdict. Most say
# "PASS -" or "FAILED -" up front, but not all: "cannot read ... FAILED",
# or Ace's missing bankroll, which never says FAILED on that line. So the
# line is a pass when it says PASS, and anything else is not. A run that exits before
# its verifier (a provider error) has no line, and gets no verdict.
VERDICT_RE = re.compile(r"^[a-z]+-verify: (.*)$", re.M)


def verdict(line):
    words = set(re.findall(r"[A-Z]+", line))
    return "PASS" if "PASS" in words else "CRASHED" if "CRASHED" in words else "FAILED"


def _last_run():
    spec = importlib.util.spec_from_file_location("last_run_trial", SCRIPTS / "last-run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def journal(unit, days):
    try:
        r = subprocess.run(["journalctl", "-u", unit, "--no-pager", "-o", "cat",
                            "--since", f"{int(days)} days ago"],
                           capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        raise SystemExit("journalctl not found - run this on the droplet")
    return r.stdout or ""


def runs(raw):
    """Every agent run in a journal, oldest first, as flat dicts, each with
    the verdict its verifier printed after it (or None)."""
    out, start = [], 0
    for m in VERDICT_RE.finditer(raw):
        chunk = _runs(raw[start:m.end()])
        if chunk:
            chunk[-1]["verdict"] = verdict(m.group(1))
        out += chunk
        start = m.end()
    return out + _runs(raw[start:])


def _runs(raw):
    lr = _last_run()
    out = []
    for block in lr.json_blocks(raw):
        d = lr.run_view(block)
        if not isinstance(d, dict) or not d.get("model"):
            continue
        if not ("assistantTurns" in d or "toolSummary" in d or "usage" in d):
            continue
        ts = d.get("toolSummary") or {}
        usage = d.get("usage") or {}
        cost = d.get("costUsd")
        if cost is None:
            cost = (usage.get("cost") or {}).get("total")
        out.append({"model": d.get("model"), "result": d.get("result") or d.get("status"),
                    "turns": d.get("assistantTurns"), "cost": cost,
                    "minutes": round(d["durationMs"] / 60000, 1) if isinstance(d.get("durationMs"), (int, float)) else None,
                    "calls": ts.get("calls"), "failures": ts.get("failures"),
                    "cached": cached_share(usage),
                    "session": d.get("sessionId"), "verdict": None})
    return out


def cached_share(usage):
    """The fraction of what was read that came from the cache, or None when
    the run reported no token counts."""
    fresh, cached = usage.get("input"), usage.get("cacheRead") or 0
    if not isinstance(fresh, (int, float)) or fresh + cached <= 0:
        return None
    return round(cached / (fresh + cached), 3)


def _avg(values):
    vals = [v for v in values if isinstance(v, (int, float))]
    return round(sum(vals) / len(vals), 4) if vals else None


def by_model(rows):
    """{model: {runs, ok, checked, passed, failures, turns, minutes, cost,
    cost_total}} - averages per run, over the runs that reported each number."""
    out = {}
    for r in rows:
        m = out.setdefault(r["model"], {"runs": 0, "ok": 0, "checked": 0, "passed": 0, "tool_failures": 0,
                                        "_t": [], "_m": [], "_c": [], "_k": []})
        m["runs"] += 1
        m["ok"] += 1 if r["result"] == "ok" else 0
        m["checked"] += 1 if r.get("verdict") else 0
        m["passed"] += 1 if r.get("verdict") == "PASS" else 0
        m["tool_failures"] += r["failures"] or 0
        m["_t"].append(r["turns"])
        m["_m"].append(r["minutes"])
        m["_c"].append(r["cost"])
        m["_k"].append(r.get("cached"))
    for m in out.values():
        m["turns"], m["minutes"], m["cost"] = _avg(m.pop("_t")), _avg(m.pop("_m")), _avg(m["_c"])
        m["cached"] = _avg(m.pop("_k"))
        m["cost_total"] = round(sum(c for c in m.pop("_c") if isinstance(c, (int, float))), 4)
    return out


def money(v):
    return "-" if v is None else f"${v:.3f}"


def pct(v):
    return "-" if v is None else f"{v * 100:.0f}%"


def cmd_report(a):
    agents = a.agents or list(AGENTS)
    result = {}
    for agent in agents:
        result[agent] = by_model(runs(journal(f"{agent}-cycle.service", a.days)))
    if a.json:
        print(json.dumps(result, indent=1))
        return 0
    print(f"Runs in the last {a.days} days, by model (OpenRouter's billed cost):\n")
    for agent, models in result.items():
        if not models:
            print(f"  {agent:<9} no runs in the journal")
            continue
        for model, m in sorted(models.items(), key=lambda kv: -kv[1]["runs"]):
            checks = f"{m['passed']}/{m['checked']}" if m["checked"] else "-"
            print(f"  {agent:<9} {model:<36} {m['runs']:>3} runs  {m['ok']:>3} ok  {checks:>5} passed checks  "
                  f"{m['turns'] if m['turns'] is not None else '-':>5} turns  "
                  f"{m['minutes'] if m['minutes'] is not None else '-':>5} min  "
                  f"{money(m['cost']):>7}/run  {pct(m['cached']):>4} cached  {m['tool_failures']} tool failures")
    print("\nA model is only better if it also passes its agent's checks. Where that column"
          " shows '-', look with:  python3 scripts/health-check.py")
    print("'cached' near 0% means caching is not working for that model: every re-read is"
          " billed at the full input price.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="every model's runs, side by side")
    r.add_argument("agents", nargs="*")
    r.add_argument("--days", type=int, default=30)
    r.add_argument("--json", action="store_true")
    r.set_defaults(fn=cmd_report)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
