"""Burned-in captions: short chunks of 1-3 words, the word being spoken lit up.

Built as an ASS subtitle file, which ffmpeg renders through libass. ASS does
styling, outlines, colour changes mid-line and positioning, so one text file
gives the "pro short" look without any per-frame drawing code.

Timing comes straight from the transcript's word timestamps, shifted to the
clip's own clock. Nothing here guesses a time.
"""

# ASS colours are &HAABBGGRR - blue first. "yellow" is &H0000E1FF, not &HFFE100.
STYLES = {
    "bold-yellow": {"font": "DejaVu Sans", "size": 84, "bold": -1, "primary": "&H00FFFFFF",
                    "highlight": "&H0000E1FF", "outline": 6, "shadow": 2, "box": False},
    "clean-white": {"font": "DejaVu Sans", "size": 74, "bold": -1, "primary": "&H00FFFFFF",
                    "highlight": "&H00FFE08A", "outline": 4, "shadow": 1, "box": False},
    "boxed": {"font": "DejaVu Sans", "size": 72, "bold": -1, "primary": "&H00FFFFFF",
              "highlight": "&H0000E1FF", "outline": 12, "shadow": 0, "box": True},
}

# Bottom margin, in 1920-high pixels. TikTok and Shorts both lay the caption,
# the username and the buttons over the bottom quarter of the screen, so text
# there is covered. 560 puts it at about 70% of the way down.
MARGIN_V = 560


def ts(seconds):
    cs = max(0, int(round(seconds * 100)))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def clean(word, upper):
    w = word.strip().replace("\\", "/").replace("{", "(").replace("}", ")")
    return w.upper() if upper else w


def chunks(words, max_words=3, max_chars=18, gap=0.5):
    """Split words into caption chunks. A chunk ends at max_words, before it
    would pass max_chars, after a sentence end, or at a pause - so a caption
    never runs one sentence into the next."""
    from clipper.moments import ends_sentence
    out, cur = [], []
    for k, w in enumerate(words):
        text = w["word"].strip()
        if cur:
            joined = len(" ".join(x["word"].strip() for x in cur)) + 1 + len(text)
            prev = cur[-1]
            if (len(cur) >= max_words or joined > max_chars or ends_sentence(prev["word"])
                    or w["start"] - prev["end"] >= gap):
                out.append(cur)
                cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


def build(words, clip_start, clip_end, style="bold-yellow", upper=True, max_words=3,
          max_chars=18, pop=True):
    """The whole .ass file for one clip. `words` are transcript words (source
    clock); only those inside the clip are used, shifted to start at 0."""
    st = STYLES[style]
    inside = [dict(w, start=w["start"] - clip_start, end=w["end"] - clip_start)
              for w in words if w["start"] >= clip_start - 0.01 and w["end"] <= clip_end + 0.01]
    groups = chunks(inside, max_words, max_chars)
    border = 3 if st["box"] else 1
    back = "&H99000000" if st["box"] else "&H80000000"
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,{st['font']},{st['size']},{st['primary']},&H000000FF,&H00000000,{back},"
        f"{st['bold']},0,0,0,100,100,0,0,{border},{st['outline']},{st['shadow']},2,60,60,{MARGIN_V},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    clip_len = clip_end - clip_start
    for g, group in enumerate(groups):
        nxt = groups[g + 1][0]["start"] if g + 1 < len(groups) else clip_len
        # Hold the chunk on screen a little past its last word, but never
        # over the next chunk - two captions at once is unreadable.
        group_end = min(group[-1]["end"] + 0.25, nxt)
        for k, w in enumerate(group):
            start = w["start"] if k else group[0]["start"]
            end = group[k + 1]["start"] if k + 1 < len(group) else group_end
            if end <= start:
                continue
            parts = []
            for m, x in enumerate(group):
                t = clean(x["word"], upper)
                if m == k:
                    # No pop in a boxed style: the enlarged word's box stands
                    # out of line with its neighbours'. Seen in a real render.
                    grow = r"\fscx108\fscy108" if pop and not st["box"] else ""
                    t = f"{{\\c{st['highlight']}{grow}}}{t}{{\\r}}"
                parts.append(t)
            lines.append(f"Dialogue: 0,{ts(start)},{ts(end)},Cap,,0,0,0,,{' '.join(parts)}")
    return "\n".join(lines) + "\n"
