"""Turn approved clips into finished videos.

Shorts (1080x1920) layouts:
  blur   - whole stream frame in the middle, blurred copy filling the background. Works for anything.
  center - crop the middle of the frame. Good for just-chatting / IRL streams.
  split  - facecam on top, gameplay below. Needs the facecam position in channels.toml.
Long (1920x1080) clips keep the original framing.
Both get the streamer credit on screen and, if faster-whisper is installed, word-by-word captions.
"""
from __future__ import annotations

from pathlib import Path

from .common import ClipperError, Job, Streamer, log, need, read_json, run, write_json
from .review import approved_clips

SHORT_W, SHORT_H = 1080, 1920
LONG_W, LONG_H = 1920, 1080
CAPTION_MARGIN = {"blur": 470, "center": 420, "split": 420}


# --------------------------------------------------------------------------- captions

_model_cache: dict = {}


def transcribe(audio: Path, model_size: str = "small", language: str | None = None) -> list[dict]:
    """Word timestamps with faster-whisper. Returns [] (no captions) if it's not installed."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log("Captions skipped: run 'pip install faster-whisper' to turn them on.")
        return []
    if model_size not in _model_cache:
        log(f"Loading Whisper '{model_size}' model (first time downloads it)...")
        _model_cache[model_size] = WhisperModel(model_size, device="auto", compute_type="int8")
    segments, _ = _model_cache[model_size].transcribe(str(audio), word_timestamps=True, language=language,
                                                      vad_filter=True)
    words = []
    for seg in segments:
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append({"start": float(w.start), "end": float(w.end), "text": text})
    return words


def clip_words(job: Job, clip: dict, model_size: str, language: str | None) -> list[dict]:
    cache_dir = job.out_dir / "captions"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / f"{clip['id']}_{int(clip['start'])}_{int(clip['end'])}.json"
    if cache.exists():
        return read_json(cache)
    wav = cache_dir / f"{clip['id']}.wav"
    run([need("ffmpeg"), "-y", "-v", "error", "-ss", f"{clip['start']:.2f}", "-i", job.video,
         "-t", f"{clip['end'] - clip['start']:.2f}", "-vn", "-ac", "1", "-ar", "16000", wav])
    words = transcribe(wav, model_size, language)
    wav.unlink(missing_ok=True)
    if words:
        write_json(cache, words)
    return words


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_text(s: str) -> str:
    return s.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(words: list[dict], credit: str, width: int, height: int, duration: float,
              vertical: bool, caption_margin: int = 470, words_per_line: int = 3) -> str:
    if vertical:
        cap_size, credit_size, credit_align, credit_margin = 86, 44, 8, 90
    else:
        cap_size, credit_size, credit_align, credit_margin = 64, 34, 7, 40
        caption_margin = 70
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Arial,{cap_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,7,3,2,60,60,{caption_margin},1
Style: Credit,Arial,{credit_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,4,2,{credit_align},40,40,{credit_margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if credit:
        lines.append(f"Dialogue: 1,{_ass_time(0)},{_ass_time(duration)},Credit,,0,0,0,,{_ass_text(credit)}")
    # Group words into short lines; highlight the word being spoken in yellow.
    chunks: list[list[dict]] = []
    for w in words:
        if (not chunks or len(chunks[-1]) >= words_per_line
                or w["start"] - chunks[-1][-1]["end"] > 0.6
                or chunks[-1][-1]["text"][-1:] in ".?!"):
            chunks.append([])
        chunks[-1].append(w)
    for chunk in chunks:
        texts = [_ass_text(w["text"]).upper() for w in chunk]
        chunk_end = chunk[-1]["end"] + 0.15
        for i, w in enumerate(chunk):
            start = w["start"]
            end = chunk[i + 1]["start"] if i + 1 < len(chunk) else chunk_end
            if end <= start:
                continue
            parts = [("{\\c&H00F5FF&}" + t + "{\\c&HFFFFFF&}") if j == i else t for j, t in enumerate(texts)]
            lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(min(end, duration))},Caption,,0,0,0,,"
                         + " ".join(parts))
    return head + "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- video filters

def short_filter(layout: str, facecam: list[float] | None) -> str:
    if layout == "center":
        return f"[0:v]crop=ih*9/16:ih,scale={SHORT_W}:{SHORT_H},setsar=1[base]"
    if layout == "split":
        if not facecam or len(facecam) != 4:
            raise ClipperError("Layout 'split' needs facecam = [x, y, w, h] (fractions of the frame) "
                               "in channels.toml or --facecam.")
        fx, fy, fw, fh = facecam
        top_h = 640
        bot_h = SHORT_H - top_h
        return (f"[0:v]split[a][b];"
                f"[a]crop=iw*{fw}:ih*{fh}:iw*{fx}:ih*{fy},"
                f"scale={SHORT_W}:{top_h}:force_original_aspect_ratio=increase,crop={SHORT_W}:{top_h}[top];"
                f"[b]crop=ih*{SHORT_W}/{bot_h}:ih,scale={SHORT_W}:{bot_h}[bot];"
                f"[top][bot]vstack,setsar=1[base]")
    if layout == "blur":
        return (f"[0:v]split[a][b];"
                f"[a]scale={SHORT_W}:{SHORT_H}:force_original_aspect_ratio=increase,crop={SHORT_W}:{SHORT_H},"
                f"boxblur=24:2,eq=brightness=-0.08[bg];"
                f"[b]scale={SHORT_W}:-2[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[base]")
    raise ClipperError(f"Unknown layout '{layout}'. Use blur, center or split.")


def long_filter() -> str:
    return (f"[0:v]scale={LONG_W}:{LONG_H}:force_original_aspect_ratio=decrease,"
            f"pad={LONG_W}:{LONG_H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[base]")


def encode(job: Job, clip: dict, out: Path, vfilter: str, ass_name: str) -> None:
    graph = vfilter + f";[base]subtitles={ass_name}[v]"
    run([need("ffmpeg"), "-y", "-v", "error",
         "-ss", f"{clip['start']:.2f}", "-i", job.video.resolve(), "-t", f"{clip['end'] - clip['start']:.2f}",
         "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
         "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
         "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-ar", "48000", "-c:a", "aac", "-b:a", "160k",
         "-movflags", "+faststart", out.name], cwd=out.parent)


# --------------------------------------------------------------------------- metadata

def description(job: Job, clip: dict, streamer: Streamer | None, short: bool) -> str:
    name = streamer.display_name if streamer else (job.meta.get("uploader") or "the streamer")
    lines = [clip.get("title") or "", ""]
    lines.append(f"🎥 Streamer: {name}" + (f" ({streamer.credit})" if streamer and streamer.credit else ""))
    if clip.get("source"):
        lines.append(f"📺 Full stream: {clip['source']}")
    if streamer:
        for link in streamer.links:
            lines.append(f"🔗 {link}")
    lines += ["", f"All credit to {name}. Clipped with permission."]
    if short:
        lines += ["", "#shorts"]
    return "\n".join(lines).strip()


def default_title(job: Job, clip: dict, streamer: Streamer | None) -> str:
    name = streamer.display_name if streamer else (job.meta.get("uploader") or "Stream")
    return f"{name} highlight {clip['id'].lstrip('c')}"


# --------------------------------------------------------------------------- command

def render(job: Job, formats: tuple[str, ...] = ("short", "long"), layout: str | None = None,
           facecam: list[float] | None = None, captions: bool = True, whisper_model: str = "small",
           language: str | None = None, force: bool = False) -> list[Path]:
    clips = approved_clips(job)
    if not clips:
        raise ClipperError("No approved clips. Approve some in the review page first.")
    streamer = job.streamer()
    layout = layout or (streamer.layout if streamer else "blur")
    facecam = facecam or (streamer.facecam if streamer else None)
    credit = ""
    if streamer:
        credit = streamer.credit or streamer.display_name
    elif job.meta.get("uploader"):
        credit = job.meta["uploader"]
    job.out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for clip in clips:
        dur = clip["end"] - clip["start"]
        want = [f for f in formats if f in clip.get("formats", ["short", "long"])]
        if not want:
            continue
        clip.setdefault("title", "")
        if not clip["title"]:
            clip["title"] = default_title(job, clip, streamer)
        words = clip_words(job, clip, whisper_model, language) if captions else []
        for fmt in want:
            is_short = fmt == "short"
            if is_short and dur > 180:
                log(f"  {clip['id']}: {dur:.0f}s is too long for a Short (max 3 min), skipping the Short.")
                continue
            out = job.out_dir / f"{clip['id']}_{fmt}.mp4"
            meta_path = out.with_suffix(".json")
            meta = {
                "file": out.name, "format": fmt, "clip_id": clip["id"],
                "title": clip["title"][:100], "description": description(job, clip, streamer, is_short),
                "tags": [t for t in [streamer.display_name if streamer else None, "highlights", "clips",
                                     "shorts" if is_short else "stream highlights"] if t],
                "start": clip["start"], "end": clip["end"], "source": clip.get("source", ""),
            }
            old = read_json(meta_path)
            if out.exists() and not force and old and old.get("start") == clip["start"] and old.get("end") == clip["end"]:
                write_json(meta_path, {**old, **{k: meta[k] for k in ("title", "description")}})
                made.append(out)
                continue
            w, h = (SHORT_W, SHORT_H) if is_short else (LONG_W, LONG_H)
            ass = out.with_suffix(".ass")
            ass.write_text(build_ass(words, credit, w, h, dur, vertical=is_short,
                                     caption_margin=CAPTION_MARGIN.get(layout, 470)), encoding="utf-8")
            vf = short_filter(layout, facecam) if is_short else long_filter()
            log(f"  rendering {out.name}  ({dur:.0f}s, {'Short/' + layout if is_short else '16:9'})")
            encode(job, clip, out, vf, ass.name)
            write_json(meta_path, meta)
            made.append(out)
    log(f"Done: {len(made)} videos in {job.out_dir}")
    return made
