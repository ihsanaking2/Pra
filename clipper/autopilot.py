"""Hands-off daily run: new stream VODs -> best moments -> captioned vertical Shorts + manifest.

To keep it fast and small enough for a free GitHub Actions runner, it never downloads a
whole stream video. It downloads the AUDIO only (plus chat when available), scores every
second, then downloads just the few seconds of video around each chosen moment.

Output (in --out):  <date>_<streamer>_<n>.mp4  and  manifest.json  (title/description per clip)
State (in --state): which VODs were already done, plus a small backlog of good moments not
                    yet used, so days without new streams still get clips.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

from .common import ClipperError, Streamer, ffprobe_duration, hms, load_streamers, log, need, read_json, run, write_json
from .highlights import audio_loudness, chat_activity, load_chat, pick_moments, score_seconds
from .render import SHORT_H, SHORT_W, CAPTION_MARGIN, build_ass, clip_words, encode, short_filter

TITLE_TEMPLATES = [
    "{name} couldn't believe this 😭",
    "{name} lost it 💀",
    "{name} was NOT ready 😂",
    "This {name} moment is crazy 😭",
    "{name} really did this 💀",
    "Chat went crazy after this 😂 ({name})",
]


# --------------------------------------------------------------------------- listing VODs

def platform_of(url: str) -> str:
    if "twitch.tv" in url:
        return "twitch"
    if "kick.com" in url:
        return "kick"
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    return "other"


def _ytdlp() -> list[str]:
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-progress"]
    cookies = os.environ.get("YTDLP_COOKIES_FILE")
    if cookies and Path(cookies).exists():
        cmd += ["--cookies", cookies]
    return cmd


def _kick_slug(url: str) -> str:
    m = re.search(r"kick\.com/([^/?#]+)", url)
    if not m:
        raise ClipperError(f"Can't read the Kick channel name from {url}")
    return m.group(1)


def _kick_get(url: str):
    try:
        from curl_cffi import requests as cr
        r = cr.get(url, impersonate="chrome", timeout=30)
        r.raise_for_status()
        return r.json()
    except ImportError:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())


def list_vods(s: Streamer, limit: int = 2) -> list[dict]:
    """Newest finished broadcasts for a streamer: [{id, url, title, platform, src}]."""
    plat = platform_of(s.vods_url)
    vods = []
    if plat == "kick":
        slug = _kick_slug(s.vods_url)
        data = _kick_get(f"https://kick.com/api/v2/channels/{slug}/videos")
        items = data if isinstance(data, list) else data.get("data", [])
        for it in items:
            if it.get("is_live"):
                continue
            uuid = (it.get("video") or {}).get("uuid") or it.get("uuid")
            src = it.get("source")
            if not uuid or not src:
                continue
            vods.append({"id": f"kick-{uuid}", "url": f"https://kick.com/{slug}/videos/{uuid}",
                         "title": it.get("session_title") or "", "platform": "kick", "src": src})
            if len(vods) >= limit:
                break
        return vods
    out = run(_ytdlp() + ["--flat-playlist", "--playlist-end", str(limit + 2), "-J", s.vods_url]).stdout
    for e in json.loads(out).get("entries") or []:
        if e.get("live_status") in ("is_live", "is_upcoming", "post_live"):
            continue
        url = e.get("url") or e.get("webpage_url")
        if not url:
            continue
        if plat == "youtube" and not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={url}"
        vods.append({"id": f"{plat}-{e.get('id')}", "url": url, "title": e.get("title") or "",
                     "platform": plat, "src": url})
        if len(vods) >= limit:
            break
    return vods


# --------------------------------------------------------------------------- downloads

def fetch_audio(vod: dict, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    # Audio-only where the site offers it; otherwise the smallest video (still has the audio).
    run(_ytdlp() + ["--no-playlist", "-N", "8", "-f", "bestaudio/worst", "-o", str(folder / "audio.%(ext)s"),
                    vod["src"]])
    found = [p for p in folder.glob("audio.*") if p.suffix not in (".part", ".ytdl")]
    if not found:
        raise ClipperError("audio download produced no file")
    return found[0]


def fetch_chat(vod: dict, folder: Path, timeout_min: int = 30) -> Path | None:
    try:
        if vod["platform"] == "youtube":
            run(_ytdlp() + ["--no-playlist", "--skip-download", "--write-subs", "--sub-langs", "live_chat",
                            "-o", str(folder / "chat"), vod["src"]])
            return next(folder.glob("chat*.live_chat.json"), None)
        if vod["platform"] == "twitch":
            cli = os.environ.get("TWITCH_DOWNLOADER") or "TwitchDownloaderCLI"
            if not any((Path(p) / cli).exists() for p in os.environ.get("PATH", "").split(os.pathsep)) and not Path(cli).exists():
                log("  (TwitchDownloaderCLI not installed - no Twitch chat)")
                return None
            vid = vod["id"].split("-", 1)[1].lstrip("v")
            out = folder / "chat.json"
            subprocess.run([cli, "chatdownload", "--id", vid, "-o", str(out), "--threads", "8",
                            "--compression", "None", "--collision", "Overwrite"],
                           check=True, timeout=timeout_min * 60, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            return out if out.exists() else None
    except (ClipperError, subprocess.SubprocessError, OSError) as e:
        log(f"  no chat ({type(e).__name__}): {str(e).splitlines()[0][:200] if str(e) else ''}")
    return None


def fetch_section(vod: dict, start: float, end: float, folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob(f"{name}.*"):
        old.unlink()
    run(_ytdlp() + ["--no-playlist", "-N", "8",
                    "-f", "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
                    "--merge-output-format", "mp4",
                    "--download-sections", f"*{start:.0f}-{end:.0f}", "--force-keyframes-at-cuts",
                    "-o", str(folder / f"{name}.%(ext)s"), vod["src"]])
    found = [p for p in folder.glob(f"{name}.*") if p.suffix in (".mp4", ".mkv", ".webm")]
    if not found:
        raise ClipperError("section download produced no file")
    return found[0]


# --------------------------------------------------------------------------- titles / text

def auto_title(name: str, words: list[dict], peak_rel: float, seed: str) -> str:
    near = [w["text"] for w in words if peak_rel - 5 <= w["start"] <= peak_rel + 3]
    phrase = re.sub(r"\s+", " ", " ".join(near)).strip(" ,.-")
    phrase = re.sub(r"[^\w\s'?!]", "", phrase)
    tokens = phrase.split()
    rnd = random.Random(seed)
    bad = re.compile(r"\b(n[i1]gg|f[a@]gg|retard)", re.I)
    if 2 <= len(tokens) and not bad.search(phrase):
        quote = " ".join(tokens[-7:]).upper()
        return f'{name}: "{quote}" ' + rnd.choice(["😭", "💀", "😂"])
    return rnd.choice(TITLE_TEMPLATES).format(name=name)


def hashtag(name: str) -> str:
    return "#" + re.sub(r"[^A-Za-z0-9]", "", name).lower()


def clip_description(title: str, s: Streamer, source: str) -> str:
    lines = [title, "", f"🎥 {s.display_name}" + (f" ({s.credit})" if s.credit else "")]
    if source:
        lines.append(f"📺 Full stream: {source}")
    lines += [f"🔗 {link}" for link in s.links]
    lines += ["", f"All credit to {s.display_name}." + (" Clipped with permission." if s.permission else ""),
              f"Subscribe for daily {s.display_name} clips!", "",
              f"#shorts {hashtag(s.display_name)} #clips #funny"]
    return "\n".join(lines)


def source_link(vod: dict, t: float) -> str:
    t = int(t)
    if vod["platform"] == "twitch":
        h, rem = divmod(t, 3600)
        m, sec = divmod(rem, 60)
        return f"{vod['url']}?t={h}h{m}m{sec}s"
    if vod["platform"] == "youtube":
        return f"{vod['url']}&t={t}s" if "?" in vod["url"] else f"{vod['url']}?t={t}s"
    return vod["url"]


# --------------------------------------------------------------------------- main run

def analyse_vod(vod: dict, s: Streamer, work: Path, length: int, per_vod: int) -> list[dict]:
    folder = work / re.sub(r"[^A-Za-z0-9_-]", "_", vod["id"])
    log(f"  audio: {vod['url']}")
    audio = fetch_audio(vod, folder)
    loud = audio_loudness(audio)
    log(f"  {hms(len(loud))} of audio")
    chat = None
    chat_file = fetch_chat(vod, folder)
    if chat_file:
        msgs = load_chat(chat_file)
        log(f"  {len(msgs)} chat messages")
        if msgs:
            chat = chat_activity(msgs, len(loud))
    total, za, zc = score_seconds(loud, chat)
    skip = 300 if len(loud) > 1800 else 0
    moments = pick_moments(total, loud, count=per_vod, length=length, min_score=2.5,
                           skip_start=skip, skip_end=60 if len(loud) > 1800 else 0, za=za, zc=zc)
    audio.unlink(missing_ok=True)
    found = []
    for m in moments:
        found.append({"streamer": s.key, "vod": vod, "start": m["start"], "end": m["end"], "peak": m["peak"],
                      "score": m["score"], "found": dt.date.today().isoformat()})
    log(f"  {len(found)} good moments")
    return found


def choose(cands: list[dict], n: int) -> list[dict]:
    """Best first, but take turns between streamers so one streamer doesn't fill the whole day."""
    by: dict[str, list[dict]] = {}
    for c in sorted(cands, key=lambda c: -c["score"]):
        by.setdefault(c["streamer"], []).append(c)
    order = sorted(by, key=lambda k: -by[k][0]["score"])
    picked = []
    while len(picked) < n and any(by.values()):
        for k in order:
            if by[k] and len(picked) < n:
                picked.append(by[k].pop(0))
    return picked


def autopilot(config: Path, out: Path, state_file: Path, work: Path, clips_per_day: int = 6,
              length: int = 35, vods_per_streamer: int = 1, per_vod: int = 8, layout: str = "blur",
              whisper_model: str = "small", time_budget_min: int = 240) -> list[dict]:
    t0 = time.time()
    streamers = load_streamers(config)
    if not streamers:
        raise ClipperError(f"No streamers in {config}")
    state = read_json(state_file, {"done_vods": [], "backlog": [], "posted": []})
    out.mkdir(parents=True, exist_ok=True)
    today = dt.date.today()

    # Drop backlog moments older than 4 days - clips need to be fresh.
    state["backlog"] = [b for b in state["backlog"]
                        if (today - dt.date.fromisoformat(b["found"])).days <= 4 and b["streamer"] in streamers]

    new_cands: list[dict] = []
    for s in streamers.values():
        if time.time() - t0 > time_budget_min * 60 * 0.6:
            log("Time budget: skipping remaining streamers")
            break
        log(f"\n== {s.display_name} ==")
        try:
            vods = list_vods(s, limit=vods_per_streamer)
        except Exception as e:  # one broken site shouldn't stop the others
            log(f"  could not list VODs: {str(e).splitlines()[0][:300]}")
            continue
        for vod in vods:
            if vod["id"] in state["done_vods"]:
                log(f"  already done: {vod['title'][:60]}")
                continue
            try:
                new_cands += analyse_vod(vod, s, work, length, per_vod)
                state["done_vods"].append(vod["id"])
            except Exception as e:
                log(f"  failed: {str(e).splitlines()[0][:300]}")

    pool = new_cands + state["backlog"]
    chosen = choose(pool, clips_per_day)
    chosen_keys = {(c["vod"]["id"], c["start"]) for c in chosen}
    state["backlog"] = [c for c in pool if (c["vod"]["id"], c["start"]) not in chosen_keys][:40]

    manifest = []
    stamp = today.strftime("%Y%m%d")
    for i, c in enumerate(chosen, 1):
        if time.time() - t0 > time_budget_min * 60:
            log("Time budget reached, stopping")
            break
        s = streamers[c["streamer"]]
        name = f"{stamp}_{s.key}_{i}"
        log(f"\n[{i}/{len(chosen)}] {s.display_name} {hms(c['start'])} (score {c['score']})")
        try:
            pad = 3.0
            sec_start = max(0.0, c["start"] - pad)
            section = fetch_section(c["vod"], sec_start, c["end"] + pad, work / "sections", name)
            rel = {"id": name, "start": c["start"] - sec_start,
                   "end": min(c["end"] - sec_start, ffprobe_duration(section))}
            words = clip_words(section, work / "captions", rel, whisper_model, None)
            title = auto_title(s.display_name, words, c["peak"] - c["start"], name)
            dur = rel["end"] - rel["start"]
            ass = out / f"{name}.ass"
            ass.write_text(build_ass(words, s.credit or s.display_name, SHORT_W, SHORT_H, dur, vertical=True,
                                     caption_margin=CAPTION_MARGIN.get(layout, 470)), encoding="utf-8")
            mp4 = out / f"{name}.mp4"
            encode(section, rel, mp4, short_filter(s.layout or layout, s.facecam), ass.name)
            ass.unlink(missing_ok=True)
            section.unlink(missing_ok=True)
            src = source_link(c["vod"], c["start"])
            manifest.append({
                "file": mp4.name, "title": title[:100], "description": clip_description(title, s, src),
                "tags": [s.display_name, f"{s.display_name} clips", "shorts", "funny", "stream clips"],
                "streamer": s.display_name, "source": src, "score": c["score"], "duration": round(dur, 1),
            })
            state["posted"].append({"vod": c["vod"]["id"], "start": c["start"], "file": mp4.name})
        except Exception as e:
            log(f"  failed: {str(e).splitlines()[0][:300]}")

    write_json(out / "manifest.json", {"date": today.isoformat(), "clips": manifest})
    state["done_vods"] = state["done_vods"][-300:]
    state["posted"] = state["posted"][-500:]
    write_json(state_file, state)
    log(f"\nMade {len(manifest)} Shorts in {out}  ({(time.time() - t0) / 60:.0f} min)")
    return manifest
