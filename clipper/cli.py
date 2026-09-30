"""python3 -m clipper <command>

    source add NAME --rights KIND --evidence "..." [--url U] [--attribution "..."] [--channel UC...]
    source channel NAME --channel UC...   link a source to the creator's YouTube channel
    source rules NAME [--tags "#a #b"] [--credit "Clip from @creator"]   what every post must carry
    source rules NAME --hunt "Topic: term, term; Topic2: term"   moments a campaign asks for
    source rules NAME --spell "Right: wrong, wrong; Right2: wrong"   names Whisper mishears
    source rules NAME --watermark FILE|URL [--cut-out-white] [--watermark-top PX]
                                a campaign's watermark, burned into every clip
    source list
    ingest SOURCE FILE_OR_HTTPS_URL [--title "..."] [--move] [--remote]
    run [--video ID]            transcribe, find moments, render - whatever is pending
    retry VIDEO_ID              put a failed video back where it failed
    refind VIDEO_ID             choose its moments again (a new --hunt); only before any clip exists
    forget VIDEO_ID [--including-posted]   remove a video and all its clips (a test run)

  A campaign's own clip list (its moments, hooks and captions):
    source rules NAME --cutlist NOTION_URL      import it
    ingest SOURCE LINK --remote --episode YT_ID  the file is that published episode
    cuts VIDEO_ID [N] [--episode YT_ID]         queue the campaign's first batch, or the next N
    status                      videos, stages, errors, today's spend
    clips [VIDEO_ID]            finished clips with score and reason

  Approving and posting:
    copy CLIP [--redo]          the title, caption and tags a clip will post with
    edit CLIP [--title T] [--caption C] [--tags "a b c"]
    approve CLIP                approve and post it (YouTube now, TikTok by hand until approved)
    reject CLIP --why "..."
    publish                     post anything approved and still waiting
    posted CLIP tiktok [--url U]   you posted it by hand
    youtube-login               sign Clip in to your YouTube channel (a code for the iPad)

  Twitch and Dropbox (a stream you may clip, clips you download yourself):
    source rules NAME --twitch LOGIN            the channel the watcher follows
    source rules NAME --dropbox-folder LINK     a shared folder: every video put in it is picked up
    twitch-login                sign Clip in to your Twitch account (a code for the iPad)
    watch                       while a watched channel is live, clip where its chat explodes
    moments                     the most-clipped moments of recent past broadcasts, to clip yourself
    pickup [--run]              ingest new videos from the Dropbox folders (and render them)

  Spotter, the research assistant:
    spot                        research now: trending creators, their clipped moments
    trends                      what the last research found
    remake TREND_ID             cut Clip's own version of a trending moment from permitted footage
"""

import argparse
import json
import re
import sys

from clipper import (campaign, config, db, dropbox, ingest, pipeline, postcopy, publish, report,
                     rights, score, transcribe, trends, twitch, youtube)


def open_all():
    cfg = config.load()
    paths = config.paths()
    return cfg, paths, db.connect(paths["db"])


def cmd_source(a):
    cfg, paths, conn = open_all()
    if a.action == "add":
        row = ingest.add_source(conn, cfg, a.name, a.rights, a.evidence or "", a.url, a.attribution,
                                a.channel)
        print(f"source {row['name']} added ({row['rights']}: {rights.RIGHTS[row['rights']]})")
        return 0
    if a.action == "rules":
        if a.twitch is not None or a.dropbox_folder is not None:
            set_pickup(conn, a.name, a.twitch, a.dropbox_folder)
        if a.cutlist:
            for ep, (section, n, first) in campaign.import_list(conn, a.name, a.cutlist).items():
                print(f"campaign list for YouTube {ep} ({section}): {n} moments, {first} to start with")
            print("link a video to its episode with --episode on ingest, or `clipper cuts VIDEO --episode ID`")
        if a.spell is not None:
            rules = transcribe.parse_spellings(a.spell)
            with conn:
                n = conn.execute("UPDATE sources SET spellings = ? WHERE name = ?",
                                 (json.dumps(rules) if rules else None, a.name)).rowcount
            if not n:
                raise ValueError(f"no source named {a.name!r}")
            for wrong, right in rules:
                print(f"captions say {right!r} where Whisper wrote {' '.join(wrong)!r}")
        if a.hunt is not None:
            hunt = score.parse_hunt(a.hunt)
            with conn:
                n = conn.execute("UPDATE sources SET hunt = ? WHERE name = ?",
                                 (json.dumps(hunt) if hunt else None, a.name)).rowcount
            if not n:
                raise ValueError(f"no source named {a.name!r}")
            for t in hunt:
                print(f"hunting: {t['name']} ({', '.join(t['terms'])})")
            if hunt:
                print("a video already searched keeps its picks - `refind VIDEO` searches it again")
        # A new watermark and its position are checked together: checking the
        # position first, against the watermark it replaces, refused the
        # droplet's first real run (the old one was the flattened white box).
        if a.watermark:
            ingest.set_watermark(conn, paths, cfg, a.name, a.watermark, a.cut_out_white,
                                 a.watermark_top)
        elif a.watermark_top is not None:
            ingest.set_watermark_top(conn, cfg, a.name, a.watermark_top)
        s = conn.execute("SELECT * FROM sources WHERE name = ?", (a.name,)).fetchone()
        if s and s["watermark"] and (a.watermark or a.watermark_top is not None):
            from clipper import render
            w, h, x, y = render.check_watermark(s["watermark"], render.source_cfg(cfg, s))
            print(f"watermark kept: {w}x{h}, centred at {x},{y} on every clip from {a.name}, "
                  f"start to finish")
        with conn:
            n = conn.execute("UPDATE sources SET post_tags = COALESCE(?, post_tags), "
                             "credit = COALESCE(?, credit) WHERE name = ?",
                             (" ".join("#" + t for t in postcopy.tags_of(a.tags)) if a.tags is not None else None,
                              a.credit, a.name)).rowcount
        if not n:
            raise ValueError(f"no source named {a.name!r}")
        s = conn.execute("SELECT * FROM sources WHERE name = ?", (a.name,)).fetchone()
        print(f"every post from {a.name} carries: tags {s['post_tags'] or '(none)'}; "
              f"credit {s['credit'] or '(none)'}")
        return 0
    if a.action == "channel":
        ingest.set_channel(conn, a.name, a.channel)
        print(f"source {a.name} linked to channel {a.channel} - Spotter now treats that creator "
              f"as one Clip may cut")
        return 0
    for s in conn.execute("SELECT * FROM sources ORDER BY id"):
        flag = ("" if s["active"] else "  [inactive]") + ("  [watermark]" if s["watermark"] else "")
        print(f"{s['id']:>3}  {s['name']:<24} {s['rights']:<14} {s['evidence'][:60]}{flag}")
    return 0


TWITCH_LOGIN = re.compile(r"(?:https?://)?(?:www\.|m\.)?(?:twitch\.tv/)?@?([A-Za-z0-9_]{3,25})/?")


def set_pickup(conn, name, twitch_login, folder):
    """A source's Twitch channel and Dropbox folder; "" clears either."""
    if twitch_login:
        m = TWITCH_LOGIN.fullmatch(twitch_login.strip())
        if not m:
            raise ValueError(f"{twitch_login!r} is not a Twitch channel - the name in "
                             f"twitch.tv/<name>, e.g. plaqueboymax")
        twitch_login = m.group(1).lower()
    if folder and not (folder.startswith("https://") and ingest.is_dropbox_folder(folder)):
        raise ValueError("--dropbox-folder wants a Dropbox folder's share link "
                         "(https://www.dropbox.com/scl/fo/...)")
    with conn:
        for col, value in (("twitch", twitch_login), ("dropbox_folder", folder)):
            if value is not None:
                n = conn.execute(f"UPDATE sources SET {col} = ? WHERE name = ?",
                                 (value or None, name)).rowcount
                if not n:
                    raise ValueError(f"no source named {name!r}")
    if twitch_login:
        print(f"the watcher follows twitch.tv/{twitch_login} for {name}")
    elif twitch_login == "":
        print(f"the watcher no longer follows a channel for {name}")
    if folder:
        print(f"every new video in that Dropbox folder becomes a {name} video "
              f"(python3 -m clipper pickup, or its timer)")
    elif folder == "":
        print(f"no Dropbox folder is picked up for {name} any more")


def cmd_twitch_login(a):
    twitch.login()
    return 0


def cmd_watch(a):
    cfg, paths, conn = open_all()
    srcs = twitch.watched(conn)
    if not srcs:
        print("no source has a Twitch channel - source rules NAME --twitch LOGIN")
        return 0
    api = twitch.Api()
    for src in srcs:
        r = twitch.watch(conn, cfg, src, api=api)
        if not r["live"]:
            print(f"{src['twitch']} is not live")
        else:
            print(f"{src['twitch']} went offline: {r['clips']} clip(s) asked for. "
                  f"Past moments: python3 -m clipper moments")
    return 0


def cmd_moments(a):
    cfg, paths, conn = open_all()
    if not a.cached:
        for ch, n in twitch.moments(conn, cfg).items():
            print(f"{ch}: {n} moment(s) viewers clipped in the last {cfg['twitch_moments_hours']} h")
    for m in report.twitch_state(conn, cfg)["moments"]:
        print(f"  {m['views']:>8,} views  {m['channel']}: {(m['title'] or '')[:60]}\n"
              f"           {m['link']}")
    return 0


def cmd_pickup(a):
    cfg, paths, conn = open_all()
    r = dropbox.pickup(conn, paths, cfg)
    print(f"Dropbox: {len(r['new'])} new video(s)"
          + (f" ({', '.join(map(str, r['new']))})" if r["new"] else "")
          + f", {r['skipped']} already picked up, {r['failed']} could not be (see clipper status)")
    if a.run and r["new"]:
        try:
            pipeline.run(conn, paths, cfg)
        except pipeline.Busy:
            print("a run is already going - it will get to them")
            return 0
        for vid in r["new"]:
            v = conn.execute("SELECT stage, error FROM videos WHERE id = ?", (vid,)).fetchone()
            print(f"video {vid}: {v['stage']}" + (f" - {v['error']}" if v["error"] else ""))
    return 0


def cmd_ingest(a):
    cfg, paths, conn = open_all()
    row, new = ingest.ingest(conn, paths, a.source, a.origin, a.title, a.move, a.remote)
    if a.episode:
        row = set_episode(conn, row, a.episode)
    if new:
        where = " - left in place, read over the network" if a.remote else ""
        print(f"video {row['id']} ingested: {row['title']} ({row['duration'] / 60:.1f} min){where}. "
              f"Next: clipper/.venv/bin/python -m clipper run")
    else:
        print(f"already have it: video {row['id']} ({row['title']}, stage {row['stage']}) - "
              f"the same file is never processed twice")
    return 0


def set_episode(conn, video, episode):
    """Link a video to the published YouTube episode it is (an id or a link)."""
    m = campaign.YT.search(episode)
    ep = m.group(1) if m else episode.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", ep):
        raise ValueError(f"{episode!r} is not a YouTube video id or link")
    with conn:
        conn.execute("UPDATE videos SET episode = ? WHERE id = ?", (ep, video["id"]))
    video = conn.execute("SELECT * FROM videos WHERE id = ?", (video["id"],)).fetchone()
    n = len(campaign.entries(conn, video))
    print(f"video {video['id']} is YouTube episode {ep}"
          + (f" - the campaign lists {n} moments for it" if n else " - no campaign list for it yet"))
    return video


def cmd_cuts(a):
    cfg, paths, conn = open_all()
    v = conn.execute("SELECT * FROM videos WHERE id = ?", (a.video,)).fetchone()
    if not v:
        raise ValueError(f"no video {a.video}")
    if a.episode:
        v = set_episode(conn, v, a.episode)
    if not campaign.entries(conn, v):
        raise ValueError(f"video {a.video} has no campaign list - import one with "
                         f"`source rules NAME --cutlist URL` and link the episode with --episode")
    if v["stage"] in ("ingested",) or (v["stage"] == "failed" and (v["error"] or "").startswith("ingested")):
        raise ValueError(f"video {a.video} is not transcribed yet - `run` first")
    todo = campaign.queue(conn, paths, cfg, v, n=a.n, first_batch=a.n is None)
    if not todo and a.n is None:
        todo = campaign.queue(conn, paths, cfg, v, n=cfg["max_clips_per_video"])
    if not todo:
        print(f"every moment on the campaign's list for video {a.video} has been cut")
        return 0
    for r in todo:
        print(f"  #{r['key']:<4} {pipeline.clock(r['start'])}-{pipeline.clock(r['end'])}  {r['hook'][:70]}")
    print(f"queued {len(todo)}. Next: nice -n 19 clipper/.venv/bin/python -m clipper run --video {a.video}")
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


def cmd_refind(a):
    cfg, paths, conn = open_all()
    v = pipeline.refind(conn, a.video)
    print(f"video {v['id']} back at transcribed: its moments will be chosen again (the "
          f"transcript is kept). Run: clipper/.venv/bin/python -m clipper run --video {v['id']}")
    return 0


def cmd_forget(a):
    cfg, paths, conn = open_all()
    title, n, posted = pipeline.forget(conn, paths, a.video, a.including_posted)
    print(f"forgot video {a.video} ({title}): {n} clip(s) and their files are gone"
          + (f"; still online, take down by hand if wanted: {', '.join(posted)}" if posted else ""))
    return 0


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


def cmd_spot(a):
    cfg, paths, conn = open_all()
    r = trends.run(conn, paths, cfg)
    print(f"Spotter: {r['creators']} trending creators over "
          f"{int(cfg['spot_min_subscribers']):,} subscribers, {r['clips']} trending clips of them, "
          f"{r['matched']} found in footage Clip may cut. {r['units']} YouTube units used.")
    for n in r["note"]:
        print(f"  note: {n}")
    print("See them: python3 -m clipper trends   (or Clip's studio in the village)")
    return 0


def cmd_trends(a):
    cfg, paths, conn = open_all()
    snap = report.snapshot(conn, paths, cfg)["spotter"]
    last = snap["last_run"]
    if not last:
        print("Spotter has not run yet: python3 -m clipper spot")
        return 0
    print(f"Last research: {last['ts']} UTC - {last['units']} units\n\nTRENDING CREATORS")
    for c in snap["creators"]:
        ok = "permission" if c["permitted"] else "no permission yet"
        print(f"  {c['title'][:28]:<28} {c['subscribers'] or 0:>12,} subs  "
              f"{c['heat']:>10,.0f} views/h  {ok}")
    print("\nTRENDING CLIPS OF THEM (other people's - never reposted)")
    for t in snap["trending"]:
        state = t["state_words"]
        if t["state"] == "matched":
            state = (f"IN YOUR FOOTAGE (video {t['match_video_id']} at "
                     f"{pipeline.clock(t['match_start'])}) -> python3 -m clipper remake {t['id']}")
        print(f"  #{t['id']:<4} {t['views_per_hour'] or 0:>9,.0f}/h  {t['creator'] or '?'}: "
              f"{t['title'][:60]}\n         {state}")
    return 0


def show_copy(conn, clip_id):
    row = conn.execute("SELECT * FROM post_copy WHERE clip_id = ?", (clip_id,)).fetchone()
    clip = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
    src = publish._source(conn, clip)
    words = postcopy.compose(row, src)
    print(f"clip {clip_id} ({row['generator']})\n  TITLE    {words['youtube']['title']}\n"
          f"  YOUTUBE  {words['youtube']['description']}\n  TIKTOK   {words['tiktok']}")


def cmd_copy(a):
    cfg, paths, conn = open_all()
    publish._clip(conn, a.clip)
    postcopy.write(conn, cfg, a.clip, redo=a.redo)
    show_copy(conn, a.clip)
    return 0


def cmd_edit(a):
    cfg, paths, conn = open_all()
    postcopy.write(conn, cfg, a.clip)
    postcopy.edit(conn, a.clip, a.title, a.caption, a.tags)
    show_copy(conn, a.clip)
    return 0


def print_results(results):
    for platform, status, detail in results:
        print(f"  {platform}: {status} - {detail}")


def cmd_approve(a):
    cfg, paths, conn = open_all()
    results = publish.approve(conn, cfg, a.clip, a.note)
    print(f"clip {a.clip} approved.")
    print_results(results)
    return 0


def cmd_reject(a):
    cfg, paths, conn = open_all()
    publish.reject(conn, a.clip, a.why)
    print(f"clip {a.clip} rejected - reason kept for learning.")
    return 0


def cmd_publish(a):
    cfg, paths, conn = open_all()
    results = publish.publish(conn, cfg)
    print_results(results) if results else print("nothing waiting to post")
    return 0


def cmd_posted(a):
    cfg, paths, conn = open_all()
    publish.mark_posted(conn, a.clip, a.platform, a.url)
    print(f"clip {a.clip} marked posted on {a.platform}.")
    return 0


def cmd_login(a):
    youtube.login()
    return 0


def cmd_deck(a):
    cfg, paths, conn = open_all()
    print(json.dumps(report.snapshot(conn, paths, cfg)))
    return 0


def cmd_remake(a):
    cfg, paths, conn = open_all()
    clip = trends.remake(conn, paths, cfg, a.trend)
    print(f"remade: clip {clip['id']} ({pipeline.clock(clip['start'])}-{pipeline.clock(clip['end'])}, "
          f"score {clip['score']:.0f}) -> {clip['path']}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="clipper", description="Permitted long videos in, captioned shorts out.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("source")
    s.add_argument("action", choices=["add", "channel", "rules", "list"])
    s.add_argument("name", nargs="?")
    s.add_argument("--rights", choices=sorted(rights.RIGHTS))
    s.add_argument("--evidence")
    s.add_argument("--url")
    s.add_argument("--attribution")
    s.add_argument("--channel", help="the creator's YouTube channel id (UC...)")
    s.add_argument("--tags", help="tags every post from this source must carry")
    s.add_argument("--credit", help="a line every post from this source must carry")
    s.add_argument("--watermark", help="a campaign's watermark PNG (file or https link), "
                                       "overlaid unchanged on every clip from this source")
    s.add_argument("--cut-out-white", action="store_true",
                   help="the watermark is drawn on a white canvas: make the canvas see-through")
    s.add_argument("--cutlist", help="a campaign's Notion page with its clip list (times, hooks, captions)")
    s.add_argument("--hunt", help="the moments the campaign wants, in its order: "
                                  "\"Knicks: knicks, chant; Pat Beverley: beverley, beverly\"")
    s.add_argument("--spell", help="names Whisper mishears: \"Trae Young: try young; Knicks: nicks\"")
    s.add_argument("--watermark-top", type=int,
                   help="how far down the 1920-high frame the watermark's top edge sits")
    s.add_argument("--twitch", help="the Twitch channel the watcher follows (\"\" to stop)")
    s.add_argument("--dropbox-folder", help="a Dropbox folder's share link: every video in it is "
                                            "picked up for this source (\"\" to stop)")
    s.set_defaults(fn=cmd_source)

    i = sub.add_parser("ingest")
    i.add_argument("source")
    i.add_argument("origin")
    i.add_argument("--title")
    i.add_argument("--move", action="store_true", help="move the file instead of copying it")
    i.add_argument("--episode", help="the published YouTube episode this file is (id or link), for a campaign list")
    i.add_argument("--remote", action="store_true",
                   help="leave an https video where it is; clips read only their own seconds")
    i.set_defaults(fn=cmd_ingest)

    r = sub.add_parser("run")
    r.add_argument("--video", type=int)
    r.set_defaults(fn=cmd_run)

    t = sub.add_parser("retry")
    t.add_argument("video", type=int)
    t.set_defaults(fn=cmd_retry)
    t = sub.add_parser("cuts", help="cut the next moments on the campaign's own list")
    t.add_argument("video", type=int)
    t.add_argument("n", type=int, nargs="?", help="how many (default: the campaign's first batch)")
    t.add_argument("--episode", help="the YouTube episode this video is (id or link)")
    t.set_defaults(fn=cmd_cuts)
    t = sub.add_parser("forget", help="remove a video and every clip made from it (a test run)")
    t.add_argument("video", type=int)
    t.add_argument("--including-posted", action="store_true",
                   help="even if some of its clips were posted (they stay online)")
    t.set_defaults(fn=cmd_forget)
    t = sub.add_parser("refind", help="choose a video's moments again, e.g. after --hunt")
    t.add_argument("video", type=int)
    t.set_defaults(fn=cmd_refind)

    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("spot").set_defaults(fn=cmd_spot)
    sub.add_parser("trends").set_defaults(fn=cmd_trends)
    sub.add_parser("deck", help="JSON for the Command Deck").set_defaults(fn=cmd_deck)
    x = sub.add_parser("copy")
    x.add_argument("clip", type=int)
    x.add_argument("--redo", action="store_true")
    x.set_defaults(fn=cmd_copy)
    x = sub.add_parser("edit")
    x.add_argument("clip", type=int)
    x.add_argument("--title")
    x.add_argument("--caption")
    x.add_argument("--tags")
    x.set_defaults(fn=cmd_edit)
    x = sub.add_parser("approve")
    x.add_argument("clip", type=int)
    x.add_argument("--note")
    x.set_defaults(fn=cmd_approve)
    x = sub.add_parser("reject")
    x.add_argument("clip", type=int)
    x.add_argument("--why", required=True)
    x.set_defaults(fn=cmd_reject)
    sub.add_parser("publish").set_defaults(fn=cmd_publish)
    x = sub.add_parser("posted")
    x.add_argument("clip", type=int)
    x.add_argument("platform", choices=["tiktok", "youtube"])
    x.add_argument("--url")
    x.set_defaults(fn=cmd_posted)
    sub.add_parser("youtube-login").set_defaults(fn=cmd_login)
    sub.add_parser("twitch-login").set_defaults(fn=cmd_twitch_login)
    sub.add_parser("watch", help="clip live streams where chat explodes, until they end").set_defaults(fn=cmd_watch)
    x = sub.add_parser("moments", help="recent past-broadcast moments viewers clipped")
    x.add_argument("--cached", action="store_true", help="the last list, without asking Twitch")
    x.set_defaults(fn=cmd_moments)
    x = sub.add_parser("pickup", help="ingest new videos from the sources' Dropbox folders")
    x.add_argument("--run", action="store_true", help="and render them")
    x.set_defaults(fn=cmd_pickup)
    m = sub.add_parser("remake")
    m.add_argument("trend", type=int)
    m.set_defaults(fn=cmd_remake)
    c = sub.add_parser("clips")
    c.add_argument("video", type=int, nargs="?")
    c.set_defaults(fn=cmd_clips)

    a = p.parse_args(argv)
    if a.cmd == "source" and a.action == "add" and not (a.name and a.rights):
        p.error("source add needs NAME and --rights (and --evidence)")
    if a.cmd == "source" and a.action == "rules" and not a.name:
        p.error("source rules needs NAME")
    if a.cmd == "source" and a.action == "channel" and not (a.name and a.channel):
        p.error("source channel needs NAME and --channel UC...")
    try:
        return a.fn(a)
    except (ValueError, OSError, RuntimeError) as exc:     # OSError: missing files, and network errors (URLError)
        print(f"clipper: {exc}", file=sys.stderr)
        return 2
