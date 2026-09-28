"""One clip, rendered: cut, 9:16, captions burned in, loudness evened out.

The output is what both TikTok and Shorts recommend: 1080x1920, H.264,
yuv420p, 30 fps, AAC 48 kHz, moov atom at the front (+faststart) so it
starts playing before it has fully downloaded.
"""

from pathlib import Path

from clipper import media

W, H = 1080, 1920


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


def video_filter(mode, src_w, src_h, ass_name, fps):
    if mode == "center":
        w, h, x, y = center_crop(src_w, src_h)
        chain = f"[0:v]crop={w}:{h}:{x}:{y},scale={W}:{H}:flags=lanczos,setsar=1"
    elif mode == "blur":
        # The blurred backdrop is made at quarter size and scaled up: the same
        # look for a sixteenth of the pixels, which matters on one CPU.
        chain = (f"[0:v]split=2[bg][fg];"
                 f"[bg]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,"
                 f"crop={W // 4}:{H // 4},boxblur=12:2,scale={W}:{H},setsar=1[back];"
                 f"[fg]scale={W}:-2:flags=lanczos,setsar=1[front];"
                 f"[back][front]overlay=(W-w)/2:(H-h)/2")
    else:
        raise ValueError(f"unknown crop_mode {mode!r} - center or blur")
    return f"{chain},fps={fps},ass={ass_name}[v]"


def command(src, out_name, start, end, info, ass_name, cfg):
    af = f"loudnorm=I={cfg['loudness_lufs']}:TP=-1.5:LRA=11,aresample=48000"
    return ["ffmpeg", "-nostdin", "-y", "-v", "error",
            "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{end - start:.3f}",
            "-filter_complex", video_filter(cfg["crop_mode"], info["width"], info["height"],
                                            ass_name, cfg["fps"]),
            "-map", "[v]", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", cfg["x264_preset"], "-crf", str(cfg["x264_crf"]),
            "-pix_fmt", "yuv420p", "-af", af, "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", out_name]


def render(src, out_path, start, end, ass_text, cfg):
    """Writes <out>.ass beside <out>.mp4 and renders. ffmpeg runs in the output
    folder so the subtitle filter gets a bare filename: a full path would need
    its colons and backslashes escaped inside the filter graph."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ass_name = out_path.with_suffix(".ass").name
    (out_path.parent / ass_name).write_text(ass_text)
    info = media.probe(src)
    if not info["has_video"]:
        raise media.MediaError(f"{src} has no video stream")
    tmp = out_path.with_name(out_path.stem + ".part.mp4")
    media.run(command(Path(src).resolve(), tmp.name, start, end, info, ass_name, cfg),
              cwd=out_path.parent)
    tmp.replace(out_path)
    return media.probe(out_path)
