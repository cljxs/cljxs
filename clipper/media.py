"""ffmpeg and ffprobe, wrapped. The only module that shells out for media.

Every command is an argument list, never a shell string: a video title with
a quote in it must not become a second command.
"""

import json
import os
import re
import subprocess
from pathlib import Path

SYSTEM_CA = "/etc/ssl/certs/ca-certificates.crt"


class MediaError(RuntimeError):
    pass


def run(cmd, cwd=None, timeout=None):
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout)
    if r.returncode != 0:
        tail = "\n".join((r.stderr or "").strip().splitlines()[-6:])
        raise MediaError(f"{Path(cmd[0]).name} failed ({r.returncode}): {tail}")
    return r


def is_url(src):
    return str(src).startswith(("https://", "http://"))


def input_args(src):
    """The -i for a source, local or remote. A local path is made absolute,
    because renders run inside the output folder. A remote one - a video
    left on the campaign's Dropbox - gets what reading over a network needs:
    the certificate checked (ffmpeg does not check it unless told to, and an
    https link is only worth having if it is), a reconnect after a dropped
    connection, and a timeout, so a stalled download fails instead of
    hanging the run forever. Seeking (-ss before this) is done with range
    requests: a 40-second clip reads about 40 seconds of the file."""
    if not is_url(src):
        return ["-i", str(Path(src).resolve())]
    ca = os.environ.get("SSL_CERT_FILE") or SYSTEM_CA
    return ["-tls_verify", "1", "-ca_file", ca, "-reconnect", "1", "-reconnect_delay_max", "30",
            "-rw_timeout", "60000000", "-i", str(src)]


def probe(path):
    """{duration, width, height, has_audio, has_video} from ffprobe."""
    r = run(["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", *input_args(path)])
    info = json.loads(r.stdout)
    streams = info.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return {
        "duration": float((info.get("format") or {}).get("duration") or 0),
        "width": int(video["width"]) if video else None,
        "height": int(video["height"]) if video else None,
        "has_video": video is not None,
        "has_audio": audio is not None,
    }


def extract_audio(src, dest):
    """16 kHz mono WAV - what Whisper resamples to anyway, so decoding once
    here keeps the transcriber's memory down and makes the loudness pass cheap.
    Written under a temporary name and renamed when complete: the pipeline
    skips extraction when audio.wav exists, so a run killed halfway (the
    droplet's kernel did kill one, 2026-09-29) must not leave a short one."""
    dest = Path(dest)
    tmp = dest.with_name(dest.stem + ".part" + dest.suffix)
    run(["ffmpeg", "-nostdin", "-y", "-v", "error", *input_args(src),
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(tmp)])
    tmp.replace(dest)
    return dest


# ebur128 logs one line per 100 ms. Real lines, captured 2026-09-28
# (tests/fixtures/clipper-ebur128.log), write M two ways - "M:-120.7" when the
# number fills the column and "M: -16.0" when it does not - so the space is
# optional here. Silence reads as -120.7, not -inf.
_FRAME = re.compile(r"\bt:\s*([0-9.]+)\s.*?\bM:\s*(-?[0-9.]+|-inf)")


def parse_ebur128(text):
    """[(t_seconds, momentary_lufs)] from ffmpeg's ebur128 log."""
    out = []
    for line in text.splitlines():
        m = _FRAME.search(line)
        if m:
            m_val = m.group(2)
            out.append((float(m.group(1)), -120.7 if m_val == "-inf" else float(m_val)))
    return out


def loudness_per_second(frames):
    """The loudest momentary reading in each whole second. Momentary loudness
    already averages 400 ms, so the max of ten readings is a fair 'how loud
    did this second get' without chasing single-sample spikes."""
    per = {}
    for t, lufs in frames:
        s = int(t)
        per[s] = max(per.get(s, -120.7), lufs)
    if not per:
        return []
    return [per.get(s, -120.7) for s in range(max(per) + 1)]


def loudness(audio_path):
    r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-i", str(audio_path),
                        "-af", "ebur128", "-f", "null", "-"], capture_output=True, text=True)
    if r.returncode != 0:
        raise MediaError(f"ebur128 failed: {(r.stderr or '')[-400:]}")
    return loudness_per_second(parse_ebur128(r.stderr))
