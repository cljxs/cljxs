"""python3 -m clipper <command>

    source add NAME --rights KIND --evidence "..." [--url U] [--attribution "..."]
    source list
    ingest SOURCE FILE_OR_HTTPS_URL [--title "..."] [--move]
    run [--video ID]            transcribe, find moments, render - whatever is pending
    retry VIDEO_ID              put a failed video back where it failed
    status                      videos, stages, errors, today's spend
    clips [VIDEO_ID]            finished clips with score and reason
"""

import argparse
import json
import sys

from clipper import config, db, ingest, pipeline, rights


def open_all():
    cfg = config.load()
    paths = config.paths()
    return cfg, paths, db.connect(paths["db"])


def cmd_source(a):
    cfg, paths, conn = open_all()
    if a.action == "add":
        row = ingest.add_source(conn, cfg, a.name, a.rights, a.evidence or "", a.url, a.attribution)
        print(f"source {row['name']} added ({row['rights']}: {rights.RIGHTS[row['rights']]})")
        return 0
    for s in conn.execute("SELECT * FROM sources ORDER BY id"):
        flag = "" if s["active"] else "  [inactive]"
        print(f"{s['id']:>3}  {s['name']:<24} {s['rights']:<14} {s['evidence'][:60]}{flag}")
    return 0


def cmd_ingest(a):
    cfg, paths, conn = open_all()
    row, new = ingest.ingest(conn, paths, a.source, a.origin, a.title, a.move)
    if new:
        print(f"video {row['id']} ingested: {row['title']} ({row['duration'] / 60:.1f} min). "
              f"Next: clipper/.venv/bin/python -m clipper run")
    else:
        print(f"already have it: video {row['id']} ({row['title']}, stage {row['stage']}) - "
              f"the same file is never processed twice")
    return 0


def cmd_run(a):
    cfg, paths, conn = open_all()
    try:
        results = pipeline.run(conn, paths, cfg, a.video)
    except pipeline.Busy as exc:
        print(exc, file=sys.stderr)
        return 3
    if not results:
        print("nothing pending")
        return 0
    bad = 0
    for vid, ok in results.items():
        v = conn.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
        n = conn.execute("SELECT COUNT(*) FROM clips WHERE video_id = ?", (vid,)).fetchone()[0]
        if ok:
            print(f"video {vid} done: {n} clip(s) in {paths['clips'] / str(vid)}")
        else:
            bad += 1
            print(f"video {vid} stopped at {v['stage']} (attempt {v['attempts']}): {v['error']}")
    return 1 if bad else 0


def cmd_retry(a):
    cfg, paths, conn = open_all()
    v = conn.execute("SELECT * FROM videos WHERE id = ?", (a.video,)).fetchone()
    if not v:
        print(f"no video {a.video}", file=sys.stderr)
        return 2
    stage = (v["error"] or "").split(":", 1)[0]
    if v["stage"] != "failed" or stage not in pipeline.STAGES:
        print(f"video {a.video} is at {v['stage']}, not failed - `run` picks it up as it is")
        return 0
    with conn:
        conn.execute("UPDATE videos SET stage = ?, attempts = 0 WHERE id = ?", (stage, a.video))
    print(f"video {a.video} back at {stage}. Run: python3 -m clipper run --video {a.video}")
    return 0


def cmd_status(a):
    cfg, paths, conn = open_all()
    rows = conn.execute("SELECT v.*, s.name AS source FROM videos v JOIN sources s "
                        "ON s.id = v.source_id ORDER BY v.id DESC LIMIT 20").fetchall()
    if not rows:
        print("no videos yet")
    for v in rows:
        n = conn.execute("SELECT COUNT(*) FROM clips WHERE video_id = ?", (v["id"],)).fetchone()[0]
        line = f"{v['id']:>3}  {v['stage']:<12} {n} clip(s)  {v['source']:<16} {v['title'][:50]}"
        if v["error"]:
            line += f"\n       error: {v['error'][:200]}"
        print(line)
    print(f"\nspent today: ${db.spent_today(conn):.4f} of ${cfg['daily_budget_usd']:.2f}")
    return 0


def cmd_clips(a):
    cfg, paths, conn = open_all()
    q, args = "SELECT * FROM clips ORDER BY video_id DESC, rank LIMIT 30", ()
    if a.video is not None:
        q, args = "SELECT * FROM clips WHERE video_id = ? ORDER BY rank", (a.video,)
    rows = conn.execute(q, args).fetchall()
    if not rows:
        print("no clips yet")
    for r in rows:
        m = json.loads(r["meta"])
        print(f"CLIP v{r['video_id']}#{r['rank']}  score {r['score']:.0f}/100  "
              f"{pipeline.clock(r['start'])}-{pipeline.clock(r['end'])}  {r['status']}\n"
              f"   why:  {'; '.join(m['reasons'])}\n   file: {r['path']}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="clipper", description="Permitted long videos in, captioned shorts out.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("source")
    s.add_argument("action", choices=["add", "list"])
    s.add_argument("name", nargs="?")
    s.add_argument("--rights", choices=sorted(rights.RIGHTS))
    s.add_argument("--evidence")
    s.add_argument("--url")
    s.add_argument("--attribution")
    s.set_defaults(fn=cmd_source)

    i = sub.add_parser("ingest")
    i.add_argument("source")
    i.add_argument("origin")
    i.add_argument("--title")
    i.add_argument("--move", action="store_true", help="move the file instead of copying it")
    i.set_defaults(fn=cmd_ingest)

    r = sub.add_parser("run")
    r.add_argument("--video", type=int)
    r.set_defaults(fn=cmd_run)

    t = sub.add_parser("retry")
    t.add_argument("video", type=int)
    t.set_defaults(fn=cmd_retry)

    sub.add_parser("status").set_defaults(fn=cmd_status)
    c = sub.add_parser("clips")
    c.add_argument("video", type=int, nargs="?")
    c.set_defaults(fn=cmd_clips)

    a = p.parse_args(argv)
    if a.cmd == "source" and a.action == "add" and not (a.name and a.rights):
        p.error("source add needs NAME and --rights (and --evidence)")
    try:
        return a.fn(a)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"clipper: {exc}", file=sys.stderr)
        return 2
