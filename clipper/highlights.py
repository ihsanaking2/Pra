"""Find the best moments in a stream.

Every second of the VOD gets a score from two signals:
  * audio  - sudden loud moments (screaming, laughing, explosions) compared with the
             loudness around them, so quiet and loud streamers both work.
  * chat   - bursts of messages, weighted up for hype words like KEKW / LMAO / "clip it".
             Chat reacts a few seconds late, so it is shifted back by `chat_delay`.
The top peaks become clip windows that start a bit before the peak (the build-up)
and end a bit after it (the reaction).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import numpy as np

from .common import ClipperError, Job, ffprobe_duration, hms, log, need, write_json

HYPE_WORDS = re.compile(
    r"\b(lol+|lmao+|lmfao|rofl|kekw|kek|lul|lulw|omegalul|icant|pog+|poggers|pogchamp|pogu|"
    r"holy|wtf|no\s*way|bruh+|insane|clutch|w+|ww+|hahaha+|xd+|monkas|pepelaugh|aware|"
    r"sheesh|goat|real|dead)\b", re.I)
HYPE_EMOJI = re.compile("[\U0001F602\U0001F923\U0001F480\U0001F525\U0001F62D\U0001F631]")
CLIP_WORDS = re.compile(r"\bclip\s*(it|that|this)?\b", re.I)


# --------------------------------------------------------------------------- signals

def audio_loudness(video: Path, sample_rate: int = 8000) -> np.ndarray:
    """Loudness in dBFS for every second of the video (streamed, so 10h VODs are fine)."""
    cmd = [need("ffmpeg"), "-v", "error", "-i", str(video), "-vn", "-ac", "1",
           "-ar", str(sample_rate), "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    chunk_seconds = 60
    per_sec = sample_rate * 2
    values: list[float] = []
    leftover = b""
    assert proc.stdout is not None
    while True:
        data = proc.stdout.read(per_sec * chunk_seconds)
        if not data:
            break
        data = leftover + data
        full = len(data) // per_sec * per_sec
        leftover = data[full:]
        if full:
            samples = np.frombuffer(data[:full], dtype=np.int16).astype(np.float32).reshape(-1, sample_rate)
            rms = np.sqrt(np.mean(samples ** 2, axis=1)) / 32768.0
            values.extend(20 * np.log10(np.maximum(rms, 1e-5)))
    if len(leftover) >= per_sec // 2:
        s = np.frombuffer(leftover[: len(leftover) // 2 * 2], dtype=np.int16).astype(np.float32)
        values.append(float(20 * np.log10(max(np.sqrt(np.mean(s ** 2)) / 32768.0, 1e-5))))
    proc.wait()
    if proc.returncode != 0 and not values:
        err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        raise ClipperError(f"ffmpeg could not read audio: {err[-500:]}")
    return np.array(values, dtype=np.float32)


def load_chat(path: Path) -> list[tuple[float, str]]:
    """Read chat from YouTube (yt-dlp live_chat.json), TwitchDownloader JSON, or a simple CSV/JSON.

    Returns (seconds_into_video, message_text) pairs.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    msgs: list[tuple[float, str]] = []
    stripped = text.lstrip()

    if path.suffix == ".csv" or (stripped and stripped[0] not in "{["):
        # simple format: one "seconds,message" per line (seconds may be h:mm:ss)
        from .common import parse_time
        for line in text.splitlines():
            if not line.strip() or line.lower().startswith(("time,", "seconds,", "t,")):
                continue
            t, _, msg = line.partition(",")
            try:
                msgs.append((parse_time(t), msg))
            except ValueError:
                continue
        return msgs

    # TwitchDownloader / generic JSON document
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        doc = None
    if isinstance(doc, dict) and "comments" in doc:
        for c in doc["comments"]:
            body = (c.get("message") or {}).get("body", "") if isinstance(c.get("message"), dict) else str(c.get("message", ""))
            msgs.append((float(c.get("content_offset_seconds", 0)), body))
        return msgs
    if isinstance(doc, list):
        for c in doc:
            t = c.get("t", c.get("time", c.get("seconds")))
            if t is not None:
                msgs.append((float(t), str(c.get("text", c.get("message", "")))))
        return msgs

    # YouTube live_chat.json from yt-dlp: one JSON object per line
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        replay = obj.get("replayChatItemAction", {})
        t_ms = replay.get("videoOffsetTimeMsec")
        if t_ms is None:
            continue
        for action in replay.get("actions", []):
            item = action.get("addChatItemAction", {}).get("item", {})
            r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
            if not r:
                continue
            runs = r.get("message", {}).get("runs", [])
            body = "".join(x.get("text") or x.get("emoji", {}).get("shortcuts", [""])[0] for x in runs)
            msgs.append((int(t_ms) / 1000.0, body))
    return msgs


def chat_activity(msgs: list[tuple[float, str]], n_seconds: int) -> np.ndarray:
    """Weighted messages per second: 1 per message, +1 per hype word, +4 if someone says 'clip it'."""
    act = np.zeros(n_seconds, dtype=np.float32)
    for t, body in msgs:
        i = int(t)
        if 0 <= i < n_seconds:
            w = 1.0 + min(3, len(HYPE_WORDS.findall(body)) + len(HYPE_EMOJI.findall(body)))
            if CLIP_WORDS.search(body):
                w += 4
            act[i] += w
    return act


# --------------------------------------------------------------------------- scoring

def smooth(x: np.ndarray, win: int) -> np.ndarray:
    if win <= 1 or len(x) == 0:
        return x.astype(np.float32)
    k = np.ones(win, dtype=np.float32) / win
    return np.convolve(x, k, mode="same").astype(np.float32)


def robust_z(x: np.ndarray, window: int = 300, step: int = 30) -> np.ndarray:
    """How unusual each second is compared with the few minutes around it."""
    n = len(x)
    if n == 0:
        return x
    centers = np.arange(0, n, step)
    meds = np.empty(len(centers), dtype=np.float32)
    mads = np.empty(len(centers), dtype=np.float32)
    for j, c in enumerate(centers):
        seg = x[max(0, c - window // 2): min(n, c + window // 2 + 1)]
        m = np.median(seg)
        meds[j] = m
        mads[j] = np.median(np.abs(seg - m))
    idx = np.arange(n)
    med = np.interp(idx, centers, meds)
    mad = np.interp(idx, centers, mads)
    global_spread = float(np.median(np.abs(x - np.median(x)))) or 1.0
    scale = 1.4826 * np.maximum(mad, 0.25 * global_spread) + 1e-6
    return ((x - med) / scale).astype(np.float32)


def score_seconds(loudness: np.ndarray, chat: np.ndarray | None, chat_delay: int = 6,
                  audio_weight: float = 1.0, chat_weight: float = 1.2) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    n = len(loudness)
    za = np.clip(robust_z(smooth(loudness, 3)), -3, 8)
    if chat is not None and chat.sum() > 0:
        c = np.zeros(n, dtype=np.float32)
        c[: max(0, n - chat_delay)] = chat[chat_delay:n]
        zc = np.clip(robust_z(smooth(c, 8)), -3, 10)
    else:
        zc = np.zeros(n, dtype=np.float32)
        chat_weight = 0.0
    total = smooth(audio_weight * np.maximum(za, 0) + chat_weight * np.maximum(zc, 0), 5)
    return total, za, zc


def _quietest(loudness: np.ndarray, lo: int, hi: int) -> int:
    lo, hi = max(0, lo), min(len(loudness) - 1, hi)
    if hi <= lo:
        return lo
    return lo + int(np.argmin(loudness[lo: hi + 1]))


def pick_moments(score: np.ndarray, loudness: np.ndarray, count: int = 15, length: int = 35,
                 lead: float = 0.65, min_score: float = 1.0, skip_start: int = 0, skip_end: int = 0,
                 za: np.ndarray | None = None, zc: np.ndarray | None = None) -> list[dict]:
    """Greedy peak picking: take the best second, make a window around it, block that area, repeat."""
    n = len(score)
    s = score.copy()
    s[: min(n, skip_start)] = -np.inf
    if skip_end:
        s[max(0, n - skip_end):] = -np.inf
    picked: list[dict] = []
    before = int(round(length * lead))
    after = length - before
    while len(picked) < count:
        peak = int(np.argmax(s))
        if not np.isfinite(s[peak]) or s[peak] < min_score:
            break
        start = max(0, peak - before)
        end = min(n, peak + after)
        # Start/end on a quiet second so clips don't cut someone off mid-word.
        start = _quietest(loudness, start - 3, start + 2)
        end = _quietest(loudness, end - 2, end + 3) + 1
        end = min(end, n)
        picked.append({
            "start": float(start),
            "end": float(end),
            "peak": float(peak),
            "score": round(float(score[peak]), 2),
            "audio": round(float(za[peak]), 2) if za is not None else None,
            "chat": round(float(zc[peak]), 2) if zc is not None else None,
        })
        block_lo = max(0, start - length // 2)
        block_hi = min(n, end + length // 2)
        s[block_lo:block_hi] = -np.inf
    picked.sort(key=lambda c: c["start"])
    for i, c in enumerate(picked, 1):
        c["id"] = f"c{i:02d}"
    return picked


# --------------------------------------------------------------------------- command

def find(job: Job, count: int = 15, length: int = 35, chat_delay: int = 6, min_score: float = 2.0,
         skip_start: int = 0, skip_end: int = 0) -> list[dict]:
    log("Measuring loudness...")
    loud = audio_loudness(job.video)
    if len(loud) < length:
        raise ClipperError(f"Video is only {len(loud)}s long - shorter than one clip ({length}s).")
    chat = None
    if job.chat and job.chat.exists():
        msgs = load_chat(job.chat)
        log(f"Read {len(msgs)} chat messages.")
        if msgs:
            chat = chat_activity(msgs, len(loud))
    total, za, zc = score_seconds(loud, chat, chat_delay=chat_delay)
    np.save(job.path / "scores.npy", np.stack([loud, total, za, zc]))
    moments = pick_moments(total, loud, count=count, length=length, min_score=min_score,
                           skip_start=skip_start, skip_end=skip_end, za=za, zc=zc)
    for m in moments:
        m["title"] = ""
        m["source"] = job.source_link(m["start"])
    write_json(job.candidates_file, {"length": length, "duration": ffprobe_duration(job.video),
                                     "used_chat": chat is not None, "clips": moments})
    log(f"Found {len(moments)} candidate moments:")
    for m in moments:
        log(f"  {m['id']}  {hms(m['start'])} - {hms(m['end'])}  score {m['score']:.1f}"
            f"  (audio {m['audio']}, chat {m['chat']})")
    return moments
