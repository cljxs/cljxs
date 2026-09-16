#!/usr/bin/env python3
"""
last-run.py <agent> — what did the agent actually DO on its last wake?

`journalctl -n 40` shows the tail of the openclaw JSON: model, stop reason,
how many tool calls, how many failed. That is enough to know something went
wrong and not enough to know what. The part you need - which tools ran, with
what arguments, what the failure said, and what the agent claimed it did - is
hundreds of lines further up.

    last-run.py ace
    last-run.py belfort

Standard library only.
"""

import json
import subprocess
import sys

MAX_LINES = 5000


def fetch(unit):
    try:
        out = subprocess.run(
            ["journalctl", "-u", unit, "--no-pager", "-o", "cat", "-n", str(MAX_LINES)],
            capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        print("journalctl not found - is this the droplet?", file=sys.stderr)
        raise SystemExit(2)
    return out.stdout or ""


def json_blocks(raw):
    """Every balanced {...} in the stream, biggest-first preference later.

    The journal interleaves the agent's pretty-printed JSON with plain log
    lines, so a naive json.loads on the whole buffer never works."""
    out, depth, start = [], 0, None
    for i, ch in enumerate(raw):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                chunk = raw[start:i + 1]
                if len(chunk) > 200:
                    try:
                        out.append(json.loads(chunk))
                    except Exception:
                        pass
                start = None
    return out


def dig(node, want, out, path=""):
    """Every dict anywhere below `node` carrying any key in `want`."""
    if isinstance(node, dict):
        if any(k in node for k in want):
            out.append((path, node))
        for k, v in node.items():
            dig(v, want, out, f"{path}.{k}")
    elif isinstance(node, list):
        for n, v in enumerate(node):
            dig(v, want, out, f"{path}[{n}]")


def main():
    if len(sys.argv) < 2:
        print("usage: last-run.py <agent>    e.g. last-run.py ace", file=sys.stderr)
        return 2
    agent = sys.argv[1]
    unit = f"{agent}-cycle.service"
    raw = fetch(unit)
    if not raw.strip():
        print(f"no journal entries for {unit}")
        return 0

    blocks = json_blocks(raw)
    if not blocks:
        print(f"no complete JSON block in the journal for {unit}. Raw tail:\n")
        print("\n".join(raw.splitlines()[-40:]))
        return 0
    # The LAST agent block, not the biggest. Picking the biggest showed the
    # previous night's cycle - it listed more games, so its JSON was longer -
    # while the run being investigated sat further down, unread.
    agent_blocks = [b for b in blocks if "toolSummary" in b or "completion" in b]
    d = (agent_blocks or blocks)[-1]

    print(f"=== {agent}: last cycle " + "=" * 40)
    comp, ts = d.get("completion") or {}, d.get("toolSummary") or {}
    print(f"model        {d.get('model')}   result={d.get('result')}")
    print(f"stopped      {comp.get('stopReason')} / {comp.get('finishReason')}")
    print(f"tool calls   {ts.get('calls')}   failures={ts.get('failures')}   "
          f"tools={', '.join(ts.get('tools') or []) or 'none'}")

    said, seen = [], set()
    holders = []
    dig(d, ("text", "content", "message"), holders)
    for _, node in holders:
        for k in ("text", "content", "message"):
            v = node.get(k)
            if isinstance(v, str) and len(v.strip()) > 30 and v not in seen:
                seen.add(v)
                said.append(v.strip())
    if said:
        print("\n--- what it said " + "-" * 39)
        for t in said[-4:]:
            print(t[:1500] + ("\n  ...[truncated]" if len(t) > 1500 else ""))
            print()

    calls = []
    dig(d, ("tool", "toolName", "name"), calls)
    print("--- tool calls " + "-" * 41)
    shown = 0
    for _, node in calls:
        name = node.get("tool") or node.get("toolName") or node.get("name")
        if not isinstance(name, str):
            continue
        args = node.get("input") or node.get("args") or node.get("arguments") or {}
        err = node.get("error") or node.get("failure") or (
            node.get("result") if node.get("isError") else None)
        print(f"  {name}" + ("   <-- FAILED" if err else ""))
        if args:
            print(f"      {json.dumps(args)[:220]}")
        if err:
            print(f"      error: {json.dumps(err)[:500]}")
        shown += 1
    if not shown:
        print("  (no per-call detail in this JSON)")
        print("  top-level keys: " + ", ".join(sorted(d.keys())))

    errs, printed, lines = [], set(), []
    dig(d, ("error", "errors", "failure", "stderr"), errs)
    for path, node in errs:
        for k in ("error", "errors", "failure", "stderr"):
            v = node.get(k)
            if v in (None, "", [], {}, False):
                continue
            s = json.dumps(v)[:400]
            if s not in printed:
                printed.add(s)
                lines.append(f"  {path}.{k}: {s}")
    if lines:
        print("\n--- errors " + "-" * 45)
        print("\n".join(lines[:15]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
