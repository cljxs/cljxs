"""One clip, rendered: cut, 9:16, captions burned in, loudness evened out.

The output is what both TikTok and Shorts recommend: 1080x1920, H.264,
yuv420p, 30 fps, AAC 48 kHz, moov atom at the front (+faststart) so it
starts playing before it has fully downloaded.
"""

from pathlib import Path

from clipper import captions, media

W, H = 1080, 1920

# Where a watermark may not go, in 1920-high pixels. The top 220 is the phone's
# status bar and TikTok's Following/For You tabs; from 900 across, TikTok's
# like/comment/share buttons; the caption band is worked out from the
# captions' own margin and largest font, so moving the captions moves this.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
TOP_CLEAR = 220
RIGHT_CLEAR = 900
CAPTION_TOP = H - captions.MARGIN_V - 2 * max(s["size"] for s in captions.STYLES.values())


def even(x):
    return int(x) // 2 * 2


def center_crop(src_w, src_h):
    """(w, h, x, y) of the largest 9:16 box in the middle of the frame.
    Worked out here from the probed size rather than as an ffmpeg expression:
    a number is testable, and min(iw,ih*9/16) needs comma-escaping that is
    easy to get wrong."""
    if src_w * 16 > src_h * 9:                   # wider than 9:16: trim the sides
        w, h = even(src_h * 9 / 16), even(src_h)
    else:                                        # taller: trim top and bottom
        w, h = even(src_w), even(src_w * 16 / 9)
    return w, h, (src_w - w) // 2, (src_h - h) // 2


def watermark_box(wm_w, wm_h, top, max_width):
    """(w, h, x, y) for a watermark: its own size, or scaled down evenly to
    max_width - never stretched - centred across, `top` pixels down. Raises
    rather than place it where a campaign says it may not go (a corner,
    under the captions, under the phone's or TikTok's own overlays): a clip
    with a covered watermark is a clip that is not paid for."""
    w, h = wm_w, wm_h
    if w > max_width:
        w, h = int(max_width), max(1, round(wm_h * max_width / wm_w))
    x, y = (W - w) // 2, int(top)
    if y < TOP_CLEAR:
        raise ValueError(f"watermark_top {y} is under the status bar and TikTok's top tabs "
                         f"(keep it at {TOP_CLEAR} or lower)")
    if y + h > CAPTION_TOP:
        raise ValueError(f"a {w}x{h} watermark at {y} reaches {y + h}, into the captions, "
                         f"which start at {CAPTION_TOP}")
    if x + w > RIGHT_CLEAR:
        raise ValueError(f"a {w}px-wide watermark reaches TikTok's buttons on the right - "
                         f"lower watermark_max_width")
    return w, h, x, y


def full_frame(inl, out):
    """The whole frame across the middle, on a blurred copy of itself. The
    backdrop is made at quarter size and scaled up: the same look for a
    sixteenth of the pixels, which matters on one CPU."""
    return (f"[{inl}]split=2[{out}g][{out}f];"
            f"[{out}g]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,"
            f"crop={W // 4}:{H // 4},boxblur=12:2,scale={W}:{H},setsar=1[{out}k];"
            f"[{out}f]scale={W}:-2:flags=lanczos,setsar=1[{out}t];"
            f"[{out}k][{out}t]overlay=(W-w)/2:(H-h)/2,format=yuv420p[{out}]")


def shot_filter(inl, out, seg):
    """One shot of a framed clip, from framing.layout(), as WxH yuv420p."""
    if seg["layout"] == "crop":
        w, h, x, y = seg["box"]
        return f"[{inl}]crop={w}:{h}:{x}:{y},scale={W}:{H}:flags=lanczos,setsar=1,format=yuv420p[{out}]"
    if seg["layout"] == "split":
        (w1, h1, x1, y1), (w2, h2, x2, y2) = seg["boxes"]
        return (f"[{inl}]split=2[{out}a][{out}b];"
                f"[{out}a]crop={w1}:{h1}:{x1}:{y1},scale={W}:{H // 2}:flags=lanczos,setsar=1[{out}t];"
                f"[{out}b]crop={w2}:{h2}:{x2}:{y2},scale={W}:{H // 2}:flags=lanczos,setsar=1[{out}u];"
                f"[{out}t][{out}u]vstack,format=yuv420p[{out}]")
    return full_frame(inl, out)


def framed(plan):
    """Every shot cut with its own layout, then joined. The last shot runs on
    past the clip's end: -t ends the clip, and a trim that stopped early
    would drop its last frames."""
    n = len(plan)
    parts = [f"[0:v]split={n}" + "".join(f"[i{k}]" for k in range(n))]
    for k, seg in enumerate(plan):
        end = f":end={seg['t1']:.3f}" if k < n - 1 else ""
        parts.append(f"[i{k}]trim=start={seg['t0']:.3f}{end},setpts=PTS-STARTPTS[j{k}]")
        parts.append(shot_filter(f"j{k}", f"s{k}", seg))
    return ";".join(parts) + ";" + "".join(f"[s{k}]" for k in range(n)) + f"concat=n={n}:v=1:a=0"


def video_filter(mode, src_w, src_h, ass_name, fps, wm=None, plan=None):
    if plan:
        chain = framed(plan)
    elif mode == "center":
        w, h, x, y = center_crop(src_w, src_h)
        chain = f"[0:v]crop={w}:{h}:{x}:{y},scale={W}:{H}:flags=lanczos,setsar=1"
    elif mode == "blur":
        chain = full_frame("0:v", "ff") + ";[ff]null"
    elif mode == "faces":                        # no plan: framing was unavailable
        w, h, x, y = center_crop(src_w, src_h)
        chain = f"[0:v]crop={w}:{h}:{x}:{y},scale={W}:{H}:flags=lanczos,setsar=1"
    else:
        raise ValueError(f"unknown crop_mode {mode!r} - faces, center or blur")
    if wm:
        # The still image is input 1. overlay repeats its one frame for as
        # long as the video runs (eof_action=repeat), so it is on screen for
        # the whole clip, first frame to last - the campaign's rule.
        w, h, x, y = wm
        chain = (f"{chain}[base];[1:v]scale={w}:{h}:flags=lanczos,format=rgba[wm];"
                 f"[base][wm]overlay={x}:{y}:eof_action=repeat")
    return f"{chain},fps={fps},ass={ass_name}[v]"


def command(src, out_name, start, end, info, ass_name, cfg, wm=None, plan=None):
    """wm: (path, (w, h, x, y)) or None. The watermark's -i comes before -t,
    so -t stays an output option (the clip's length) and is not read as
    the length of the image."""
    af = f"loudnorm=I={cfg['loudness_lufs']}:TP=-1.5:LRA=11,aresample=48000"
    extra = ["-i", str(wm[0])] if wm else []
    return ["ffmpeg", "-nostdin", "-y", "-v", "error",
            "-ss", f"{start:.3f}", *media.input_args(src), *extra, "-t", f"{end - start:.3f}",
            "-filter_complex", video_filter(cfg["crop_mode"], info["width"], info["height"],
                                            ass_name, cfg["fps"], wm[1] if wm else None, plan),
            "-map", "[v]", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", cfg["x264_preset"], "-crf", str(cfg["x264_crf"]),
            "-pix_fmt", "yuv420p", "-af", af, "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", out_name]


def has_transparency(path):
    """Whether a PNG can be see-through anywhere: an alpha channel (colour
    type 4 or 6 in its header), or a tRNS chunk giving a palette or a single
    colour transparency. Read from the file's own bytes - the PNG spec puts
    the colour type at byte 25, in the IHDR chunk that must come first."""
    data = Path(path).read_bytes()
    return len(data) > 25 and (data[25] in (4, 6) or b"tRNS" in data)


def hook_top(wm_box):
    """Where a campaign's hook goes: its usual place under the status bar, or
    just below the watermark when the watermark is up there - a hook over the
    watermark is a covered watermark, which the campaign does not pay for."""
    top = captions.HOOK_TOP
    if wm_box:
        w, h, x, y = wm_box
        if y < top + captions.hook_height() and y + h > top:
            top = y + h + 20
    if top + captions.hook_height() > CAPTION_TOP:
        raise ValueError(f"no room for a hook: it would reach the captions at {CAPTION_TOP} "
                         f"(watermark at {wm_box[3] if wm_box else '-'}; move it lower or higher)")
    return top


def source_cfg(cfg, src):
    """cfg with a source's own watermark position, if it has one."""
    top = src["watermark_top"] if src is not None else None
    return dict(cfg, watermark_top=top) if top is not None else cfg


def check_watermark(path, cfg):
    """(w, h, x, y) for a watermark file, or raise. A PNG only: the campaign's
    white-with-glow mark needs its transparency, and a JPEG would put a
    rectangle behind it - a changed watermark."""
    path = Path(path)
    if not path.is_file():
        raise media.MediaError(f"watermark {path} is missing - every clip from this source "
                               f"must carry it, so none is rendered without it")
    # The file's own first bytes, not its name: ffprobe goes by the .png on
    # the end and reads a text file called mark.png as a 0x0 PNG.
    with open(path, "rb") as f:
        if f.read(8) != PNG_SIGNATURE:
            raise ValueError("the watermark must be the PNG file the campaign supplies - "
                             "this file is not a PNG")
    info = media.probe(path)
    if not (info["width"] and info["height"]):
        raise ValueError("the watermark PNG has no picture in it")
    if not has_transparency(path):
        raise ValueError("the watermark PNG has no transparent background, so it would put a "
                         "solid box over every clip. Ask the campaign for the transparent PNG, "
                         "or, if it is a mark on a white canvas, add --cut-out-white")
    return watermark_box(info["width"], info["height"], cfg["watermark_top"],
                         cfg["watermark_max_width"])


def render(src, out_path, start, end, ass_text, cfg, watermark=None):
    """Writes <out>.ass beside <out>.mp4 and renders. ffmpeg runs in the output
    folder so the subtitle filter gets a bare filename: a full path would need
    its colons and backslashes escaped inside the filter graph. The watermark
    goes in as an input instead, so its path needs no escaping."""
    wm = (Path(watermark).resolve(), check_watermark(watermark, cfg)) if watermark else None
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ass_name = out_path.with_suffix(".ass").name
    (out_path.parent / ass_name).write_text(ass_text)
    info = media.probe(src)
    if not info["has_video"]:
        raise media.MediaError(f"{src} has no video stream")
    # Framing a clip shot by shot (crop_mode "faces", framing.py). If it cannot
    # run - no OpenCV yet - the clip is still made, centre-cropped, and says so.
    plan, framing_note = None, None
    if cfg["crop_mode"] == "faces":
        from clipper import framing
        try:
            plan = framing.plan(src, start, end, info)
            framing_note = framing.summary(plan)
        except framing.FramingUnavailable as exc:
            framing_note = f"centre crop - {exc}"
    tmp = out_path.with_name(out_path.stem + ".part.mp4")
    media.run(command(src, tmp.name, start, end, info, ass_name, cfg, wm, plan),
              cwd=out_path.parent)
    tmp.replace(out_path)
    return dict(media.probe(out_path), framing=framing_note)
