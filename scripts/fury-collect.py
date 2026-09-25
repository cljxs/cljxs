#!/usr/bin/env python3
"""
fury-collect.py — gathers the state of the whole ecosystem into one JSON file.

Plain code. NO AI. Fury READS this file. If a fact about the system is not in
here, Fury does not state it. Same anti-hallucination split the other agents
use, pointed at the ecosystem itself instead of at a market.

Standard library only.
"""

import importlib.util
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
AGENTS = ROOT / "agents"
OUT = AGENTS / "fury" / "data" / "system.json"
API = os.environ.get("MISSION_CONTROL_API", "http://127.0.0.1:3001")

# scout-ideas.py owns what "pending" means for an idea.
_scout_spec = importlib.util.spec_from_file_location(
    "scout_ideas", Path(__file__).resolve().parent / "scout-ideas.py")
scout = importlib.util.module_from_spec(_scout_spec)
_scout_spec.loader.exec_module(scout)


def sh(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def read_json(p):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return None


def mins_since_iso(s):
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s).replace(" ", "T").replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - t).total_seconds() // 60))
    except Exception:
        return None


def newest_report(d):
    try:
        files = sorted(((f, f.stat().st_mtime) for f in Path(d).glob("*.md")),
                       key=lambda x: -x[1])
        if not files:
            return None
        f, m = files[0]
        body = f.read_text(errors="replace")
        line = next((l.strip() for l in body.splitlines()
                     if len(l.strip()) > 25 and not l.startswith("#")), "")
        return {"name": f.name,
                "written_utc": datetime.fromtimestamp(m, timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "age_min": int((datetime.now(timezone.utc).timestamp() - m) // 60),
                "first_line": line[:300],
                "count_total": len(files)}
    except Exception:
        return None


def last_memory_line(p):
    try:
        lines = [l.strip() for l in Path(p).read_text().splitlines()
                 if l.strip().startswith("-") and "no cycles" not in l.lower()
                 and "no runs yet" not in l.lower()]
        return lines[-1][:250] if lines else None
    except Exception:
        return None


def state_summary(d):
    """Pick out whatever money/counter fields an agent happens to use."""
    for name in ("portfolio.json", "bankroll.json", "ideas.json"):
        j = read_json(Path(d) / "state" / name)
        if j is None:
            continue
        if name == "ideas.json":
            ideas = j.get("ideas", []) if isinstance(j, dict) else []
            return {"file": name,
                    "ideas_total": len(ideas),
                    "ideas_pending": sum(1 for i in ideas if scout.is_pending(i)),
                    "ideas_approved": sum(1 for i in ideas if i.get("status") == "approved"),
                    "ideas_rejected": sum(1 for i in ideas if i.get("status") == "rejected")}
        out = {"file": name, "cycle_count": j.get("cycle_count"),
               "last_cycle_utc": j.get("last_cycle_utc"),
               "last_cycle_age_min": mins_since_iso(j.get("last_cycle_utc"))}
        # market_value is the book's own total, written by belfort-trade.py
        # `mark` at the end of every cycle - carried so the briefing can read
        # it rather than redo the sum.
        for k in ("cash", "bankroll", "starting_cash", "starting_bankroll",
                  "market_value"):
            if k in j:
                out[k] = j[k]
        if isinstance(j.get("positions"), list):
            out["open_positions"] = len(j["positions"])
            out["positions"] = [{"symbol": p.get("symbol"),
                                 "shares": p.get("shares"),
                                 "cost_basis": p.get("cost_basis") or p.get("entry_price"),
                                 "last_price": p.get("last_price"),
                                 "market_value": p.get("market_value")}
                                for p in j["positions"]]
        if isinstance(j.get("open_bets"), list):
            out["open_bets"] = len(j["open_bets"])
        if isinstance(j.get("settled_bets"), list):
            out["settled_bets"] = len(j["settled_bets"])
        return out
    return None


def units_for(agent):
    return [u for u in (f"{agent}-cycle.service", f"{agent}-fetch.service") if u]


# ---------------------------------------------------------------- briefing
#
# Fury the agent was asked five times, in five wordings, to write this file.
# Every run made one tool call, composed a good briefing into its reply, and
# saved nothing - including the run where the file was already on disk with
# the headings in place and it only had to fill them in.
#
# Everything the briefing needs is already in this script's own output. The
# model was only ever turning known facts into sentences, so this does that
# in plain code: correct every morning, never silently missing, and free.


def money(v):
    if v is None:
        return None
    return f"{'-' if v < 0 else ''}${abs(v):,.2f}"


def pick(d, *names):
    """State files spell the same idea differently - cash/bankroll/balance,
    positions/open_bets. Take the first one that is actually there."""
    for n in names:
        if isinstance(d, dict) and d.get(n) is not None:
            return d[n]
    return None


def agent_money(state):
    """Returns (value, pnl, pnl_pct) or (None, None, None)."""
    if not isinstance(state, dict):
        return None, None, None
    cash = pick(state, "cash", "bankroll", "balance")
    start = pick(state, "starting_cash", "starting_bankroll", "starting_balance")
    positions = pick(state, "positions", "open_bets") or []
    if cash is None:
        return None, None, None
    # THE BOOK'S OWN TOTAL FIRST. The briefing printed "Belfort $1,593.29
    # (-84.07%)" beside a Deck showing +0.95%: state_summary copied each
    # position as symbol, shares and cost_basis, and this looked for a price
    # under "price", "last" or "entry" - none of which it had been given - so
    # every open position counted as nothing and the value was the cash.
    # Belfort's `mark` writes the whole book's market value every cycle; the
    # code that owns the book has already done this sum.
    book = pick(state, "market_value")
    if isinstance(book, (int, float)) and not isinstance(book, bool):
        value = float(book)
    else:
        held = 0.0
        for p in positions if isinstance(positions, list) else []:
            if not isinstance(p, dict):
                continue
            mv = pick(p, "market_value")
            if isinstance(mv, (int, float)):
                held += float(mv)
                continue
            px = pick(p, "last_price", "price", "last", "cost_basis", "entry")
            sh = pick(p, "shares", "qty", "stake")
            if px is not None and sh is not None:
                held += float(px) * float(sh)
        value = float(cash) + held
    if not start:
        return value, None, None
    start = float(start)
    return value, value - start, (value / start - 1) * 100


def store_lines():
    """The store report's lines, or [] when it cannot be read. A briefing with
    no store section is better than no briefing."""
    try:
        spec = importlib.util.spec_from_file_location(
            "store_report", Path(__file__).resolve().parent / "store-report.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.lines(mod.latest_summary())
    except Exception as exc:
        return [f"store report unavailable: {type(exc).__name__}"]


def build_briefing(report, date_str):
    agents = report.get("agents") or {}
    needs, happened, monies = [], [], []

    for unit in report.get("failures") or []:
        needs.append(f"`{unit.get('unit', 'a service')}` failed"
                     + (f" (exit {unit['exit_code']})" if unit.get("exit_code") else ""))

    q = report.get("queue") or {}
    pending = q.get("pending") if isinstance(q, dict) else None
    if pending:
        needs.append(f"{pending} task{'s' if pending != 1 else ''} pending in the queue")
    if report.get("queue_error"):
        needs.append(f"the task queue could not be read ({report['queue_error'][:60]})")

    try:
        n = scout.pending_count()
        if n:
            needs.append(f"{n} Scout idea{'s' if n != 1 else ''} awaiting your review")
    except Exception:
        pass

    for name in sorted(agents):
        a = agents[name] or {}
        state, line = a.get("state"), a.get("last_memory_line")
        value, pnl, pct = agent_money(state)
        if value is not None:
            monies.append((name, value, pnl, pct))
        if line:
            happened.append(f"**{name.title()}** — {str(line).strip()[:160]}")
        elif isinstance(state, dict) and pick(state, "cycle_count") is not None:
            happened.append(f"**{name.title()}** — cycle {pick(state, 'cycle_count')}, no note written")

    if needs:
        opener = (f"**{len(needs)} thing{'s' if len(needs) != 1 else ''} need"
                  f"{'' if len(needs) != 1 else 's'} you today.**")
    else:
        opener = "**Nothing needs you today.** Everything that was scheduled ran."

    out = [f"# Daily briefing — {date_str}", "", opener, ""]

    out.append("## \u26a0\ufe0f Needs you")
    out.append("")
    out += [f"- {n}" for n in needs] if needs else ["- Nothing."]
    out.append("")

    out.append("## What happened")
    out.append("")
    out += [f"- {h}" for h in happened] if happened else ["- No agent recorded activity."]
    out.append("")

    if monies:
        out.append("## The money")
        out.append("")
        total = 0.0
        for name, value, pnl, pct in monies:
            total += value
            bit = f"- **{name.title()}** {money(value)}"
            if pct is not None:
                bit += f" ({'+' if pct >= 0 else ''}{pct:.2f}%)"
            out.append(bit)
        if len(monies) > 1:
            out.append(f"- **Combined** {money(total)}")
        out.append("")

    # THE STORE. Read through store-report.py's own summary, so the briefing
    # and `store-report.py show` print the same numbers from the same sums.
    store = store_lines()
    if store:
        out.append("## The store")
        out.append("")
        out += [f"- {line.strip()}" if not line.startswith("  ") else f"  - {line.strip()}"
                for line in store]
        out.append("")

    out.append(f"_Assembled from data/system.json at {report.get('generated_utc', '?')} UTC. "
               "Every figure here was read from a state file, not estimated._")
    return "\n".join(out) + "\n"


def write_briefing(reports_dir, date_str, report):
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{date_str}.md"
    path.write_text(build_briefing(report, date_str))
    return path


def log_line(agent_dir, date_str, report):
    """One line per briefing, appended - the same log the agents keep."""
    needs = len(report.get("failures") or [])
    q = report.get("queue") or {}
    if isinstance(q, dict) and q.get("pending"):
        needs += 1
    ran = sum(1 for a in (report.get("agents") or {}).values()
              if a and a.get("last_memory_line"))
    mem = agent_dir / "MEMORY.md"
    with mem.open("a") as f:
        f.write(f"{date_str} | briefing written | {ran} agent(s) active | "
                f"{needs} item(s) needing attention\n")
    return mem


def collect(now=None):
    """The whole system's state, read now. No file is written.

    Split from main() so the GM can build the same briefing live when it
    runs: it used to read the file written at 08:30, and a GM run at 11:00
    told the owner three Scout ideas were waiting that had been approved in
    between.
    """
    now = now or datetime.now(timezone.utc)

    agents = sorted(p.name for p in AGENTS.iterdir()
                    if p.is_dir() and not p.name.startswith(".")) if AGENTS.is_dir() else []

    report = {"generated_utc": now.strftime("%Y-%m-%d %H:%M:%S"),
              "generated_day": now.strftime("%Y-%m-%d"),
              "agents": {}, "services": {}, "queue": None, "failures": []}

    for a in agents:
        d = AGENTS / a
        report["agents"][a] = {
            "state": state_summary(d),
            "latest_report": newest_report(d / "reports"),
            "last_memory_line": last_memory_line(d / "MEMORY.md"),
            "data_asof": (read_json(d / "data" / "_meta.json") or {}).get("asof_utc"),
        }

    # systemd: what ran, what failed, what is next
    for a in agents:
        for unit in units_for(a):
            active = sh(f"systemctl is-active {unit} 2>/dev/null")
            if not active:
                continue
            failed = sh(f"systemctl is-failed {unit} 2>/dev/null")
            props = sh(f"systemctl show {unit} -p ExecMainStatus -p ExecMainExitTimestamp 2>/dev/null")
            info = dict(l.split("=", 1) for l in props.splitlines() if "=" in l)
            entry = {"state": active,
                     "failed": failed == "failed",
                     "last_exit_code": info.get("ExecMainStatus"),
                     "last_finished": info.get("ExecMainExitTimestamp") or None}
            report["services"][unit] = entry
            if entry["failed"] or (entry["last_exit_code"] not in (None, "", "0")):
                report["failures"].append(
                    {"unit": unit, "exit_code": entry["last_exit_code"],
                     "last_finished": entry["last_finished"]})

    timers = sh("systemctl list-timers '*-cycle.timer' '*-fetch.timer' --no-pager 2>/dev/null")
    report["timers_raw"] = timers.splitlines()[:14] if timers else []

    try:
        with urllib.request.urlopen(API + "/tasks/stats", timeout=10) as r:
            report["queue"] = json.loads(r.read())
        with urllib.request.urlopen(API + "/tasks?status=pending", timeout=10) as r:
            rows = json.loads(r.read())
            report["queue_pending"] = [{"id": t["id"], "assignee": t["assignee"],
                                        "type": t["type"], "created_at": t["created_at"]}
                                       for t in rows][:20]
    except Exception as exc:
        report["queue_error"] = str(exc)[:160]
    return report


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    report = collect()
    agents = report["agents"]
    OUT.write_text(json.dumps(report, indent=1) + "\n")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = write_briefing(OUT.parent.parent / "reports", today, report)
    mem = log_line(OUT.parent.parent, today, report)
    print(f"[fury-collect] briefing written: {path}")
    print(f"[fury-collect] logged to {mem}")

    print(f"[fury-collect] ok: {len(agents)} agents, "
          f"{len(report['services'])} services, {len(report['failures'])} failure(s) -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
