#!/usr/bin/env python3
"""
fury-collect.py — gathers the state of the whole ecosystem into one JSON file.

Plain code. NO AI. Fury READS this file. If a fact about the system is not in
here, Fury does not state it. Same anti-hallucination split the other agents
use, pointed at the ecosystem itself instead of at a market.

Standard library only.
"""

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
                    "ideas_pending": sum(1 for i in ideas if i.get("status", "pending") == "pending"),
                    "ideas_approved": sum(1 for i in ideas if i.get("status") == "approved"),
                    "ideas_rejected": sum(1 for i in ideas if i.get("status") == "rejected")}
        out = {"file": name, "cycle_count": j.get("cycle_count"),
               "last_cycle_utc": j.get("last_cycle_utc"),
               "last_cycle_age_min": mins_since_iso(j.get("last_cycle_utc"))}
        for k in ("cash", "bankroll", "starting_cash", "starting_bankroll"):
            if k in j:
                out[k] = j[k]
        if isinstance(j.get("positions"), list):
            out["open_positions"] = len(j["positions"])
            out["positions"] = [{"symbol": p.get("symbol"),
                                 "shares": p.get("shares"),
                                 "cost_basis": p.get("cost_basis") or p.get("entry_price")}
                                for p in j["positions"]]
        if isinstance(j.get("open_bets"), list):
            out["open_bets"] = len(j["open_bets"])
        if isinstance(j.get("settled_bets"), list):
            out["settled_bets"] = len(j["settled_bets"])
        return out
    return None


def units_for(agent):
    return [u for u in (f"{agent}-cycle.service", f"{agent}-fetch.service") if u]


# ---------------------------------------------------------------- skeleton

PLACEHOLDER = "FURY-HAS-NOT-WRITTEN-THIS-YET"

SKELETON = """# Daily briefing — {date}

<!-- {ph} -->
<!-- Fury: replace every line below with the real briefing, then delete the -->
<!-- comment above. Keep it under 250 words. Data is in data/system.json.   -->

## Is anything wrong?

_(not yet written)_

## ⚠️ Needs you

_(not yet written)_

## What happened

_(not yet written)_

## The money

_(not yet written)_
"""


def write_skeleton(reports_dir, date_str):
    """Leave today's briefing file on disk, pre-filled with headings.

    Fury kept composing a good briefing into its reply and saving nothing -
    three separate wordings of "write the file" did not change that. Creating
    a file from scratch is the step it skips; editing one that already exists
    is a different, easier task. So the collector creates it and Fury fills
    it in. The service fails the run while the placeholder is still there.

    Never clobbers a real briefing: if the file exists and no longer carries
    the placeholder, Fury has already written it and we leave it alone.
    """
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{date_str}.md"
    if path.exists() and PLACEHOLDER not in path.read_text():
        return path, False
    path.write_text(SKELETON.format(date=date_str, ph=PLACEHOLDER))
    return path, True


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)

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

    OUT.write_text(json.dumps(report, indent=1) + "\n")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path, created = write_skeleton(OUT.parent.parent / "reports", today)
    print(f"[fury-collect] briefing skeleton {'created' if created else 'left alone (already written)'}: {path}")

    print(f"[fury-collect] ok: {len(agents)} agents, "
          f"{len(report['services'])} services, {len(report['failures'])} failure(s) -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
