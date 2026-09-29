"""Transcription providers. Each turns an audio file into the one format the
rest of Clipper reads:

    {"provider", "model", "language", "duration",
     "words": [{"start": s, "end": s, "word": " text", "p": 0.97}, ...]}

Words keep Whisper's leading space, so joining them reproduces the text.

A provider is a function (audio_path, cfg) -> that dict. To add one (Groq at
$0.04 an hour, OpenAI, a GPU box), write the function and add it to
PROVIDERS; nothing downstream changes. A paid provider must call
db.spend_guard before sending anything.

The transcript is cached beside the audio and never recomputed: the most
expensive step in the pipeline runs once per video, whatever fails after it.
"""

import json
from pathlib import Path


def faster_whisper(audio_path, cfg):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError("faster-whisper is not installed. On the droplet: "
                           "clipper/.venv/bin/pip install -r clipper/requirements.txt")
    model = WhisperModel(cfg["whisper_model"], device="cpu", compute_type="int8",
                         cpu_threads=int(cfg["whisper_threads"]))
    segments, info = model.transcribe(str(audio_path), language=cfg.get("language"),
                                      word_timestamps=True, vad_filter=True, beam_size=1)
    # float(): faster-whisper hands back numpy.float64, seen in the first
    # real run. JSON happens to accept it; nothing should have to.
    words = [{"start": round(float(w.start), 3), "end": round(float(w.end), 3), "word": w.word,
              "p": round(float(w.probability), 3)}
             for seg in segments for w in (seg.words or [])]
    return {"provider": "faster-whisper", "model": cfg["whisper_model"],
            "language": info.language, "duration": round(info.duration, 3), "words": words}


def from_file(audio_path, cfg):
    """A transcript already on disk (a test fixture, or captions the rights
    holder supplied): <audio>.words.json."""
    return json.loads(Path(str(audio_path) + ".words.json").read_text())


PROVIDERS = {"faster-whisper": faster_whisper, "file": from_file}


# ------------------------------------------------------------------ a source's spellings

def _norm(word):
    return "".join(ch for ch in word.lower() if ch.isalnum() or ch == "'").strip("'")


def parse_spellings(text):
    """'Trae Young: try young, tray young; Knicks: nicks' ->
    [[["try", "young"], "Trae Young"], [["tray", "young"], "Trae Young"],
     [["nicks"], "Knicks"]], longest first so "try young" is fixed before a
    shorter rule could take part of it. Whisper's mishearings of names are
    per-show, so they are listed per source rather than guessed."""
    out = []
    for part in (text or "").split(";"):
        right, sep, wrongs = part.partition(":")
        if not sep or not right.strip():
            continue
        for w in wrongs.split(","):
            words = [_norm(x) for x in w.split() if _norm(x)]
            if words:
                out.append([words, right.strip()])
    return sorted(out, key=lambda r: -len(r[0]))


def respell(words, rules):
    """The words with each listed mishearing replaced. Whole words only, so
    "try young" never touches "try" alone. The new words share the old ones'
    time span evenly - captions stay in sync - and keep the old last word's
    trailing punctuation, so sentences still end where they did. The cached
    transcript itself is never changed; this is applied when it is read."""
    if not rules:
        return words
    norm = [_norm(w["word"]) for w in words]
    out, i = [], 0
    while i < len(words):
        for wrong, right in rules:
            n = len(wrong)
            if norm[i:i + n] == wrong:
                old = words[i:i + n]
                new = right.split()
                start, end = old[0]["start"], old[-1]["end"]
                step = (end - start) / len(new)
                tail = old[-1]["word"].rstrip()
                punct = tail[len(tail.rstrip(".,?!;:\"'")):]
                lead = old[0]["word"][:len(old[0]["word"]) - len(old[0]["word"].lstrip())] or " "
                for k, t in enumerate(new):
                    out.append({"start": round(start + k * step, 3),
                                "end": round(start + (k + 1) * step, 3),
                                "word": (lead if k == 0 else " ") + t + (punct if k == len(new) - 1 else ""),
                                "p": min(w.get("p", 1.0) for w in old)})
                i += n
                break
        else:
            out.append(words[i])
            i += 1
    return out


def transcribe(audio_path, cache_path, cfg):
    cache_path = Path(cache_path)
    if cache_path.exists():
        return json.loads(cache_path.read_text())
    name = cfg["transcriber"]
    if name not in PROVIDERS:
        raise ValueError(f"unknown transcriber {name!r} - one of {', '.join(PROVIDERS)}")
    result = PROVIDERS[name](audio_path, cfg)
    if not result.get("words"):
        raise RuntimeError(f"{name} heard no words in {audio_path} - is there speech in it?")
    tmp = cache_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result))
    tmp.replace(cache_path)
    return result
