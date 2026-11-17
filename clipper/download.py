"""Download a stream VOD (and its chat replay when available) with yt-dlp."""
from __future__ import annotations

import sys
from pathlib import Path

from .common import (WORK_DIR, ClipperError, Job, hms, load_streamers, log, parse_time, read_json,
                     run, slugify)


def _ytdlp() -> list[str]:
    try:
        import yt_dlp  # noqa: F401
    except ImportError as e:
        raise ClipperError("yt-dlp is not installed. Run: pip install yt-dlp") from e
    return [sys.executable, "-m", "yt_dlp"]


def download(url: str, streamer: str | None = None, start: str | None = None, end: str | None = None,
             chat_file: str | None = None, max_height: int = 1080, work_dir: Path = WORK_DIR) -> Job:
    streamers = load_streamers()
    if streamer and streamer not in streamers:
        log(f"Warning: '{streamer}' is not in channels.toml. Add them there with a 'permission' note "
            "so every clip gets the right credit.")
    elif streamer and not streamers[streamer].permission:
        log(f"Warning: channels.toml has no 'permission' note for '{streamer}'. "
            "Only clip streamers who allow it.")
    elif not streamer:
        log("Tip: pass --streamer NAME (from channels.toml) so clips get the right credit.")

    # 1. Read metadata first so the job folder gets a readable name.
    info_raw = run(_ytdlp() + ["--dump-single-json", "--no-playlist", "--skip-download", url]).stdout
    import json
    info = json.loads(info_raw)
    vod_id = info.get("id") or slugify(url)
    title = info.get("title") or vod_id
    job_dir = work_dir / f"{slugify(streamer or info.get('uploader') or 'stream', 20)}-{slugify(str(vod_id), 30)}"
    if start or end:
        job_dir = job_dir.with_name(job_dir.name + f"-{int(parse_time(start or '0'))}")
    job_dir.mkdir(parents=True, exist_ok=True)

    offset = parse_time(start) if start else 0.0

    # 2. Video. Long streams: use --from/--to to fetch only part of it.
    cmd = _ytdlp() + [
        "--no-playlist",
        "-f", f"bv*[height<={max_height}]+ba/b[height<={max_height}]/bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", str(job_dir / "video.%(ext)s"),
        "--no-part",
    ]
    if start or end:
        section = f"*{start or '0'}-{end or 'inf'}"
        cmd += ["--download-sections", section, "--force-keyframes-at-cuts"]
    cmd.append(url)
    log(f"Downloading video: {title}" + (f"  [{start or '0'} → {end or 'end'}]" if start or end else ""))
    run(cmd, quiet=False)
    video = next((p for p in job_dir.glob("video.*") if p.suffix in (".mp4", ".mkv", ".webm")), None)
    if not video:
        raise ClipperError("yt-dlp finished but no video file was found.")

    # 3. Chat replay. YouTube: live_chat. Twitch/Kick: bring your own (see README).
    chat_name = None
    if chat_file:
        src = Path(chat_file)
        dst = job_dir / ("chat" + "".join(src.suffixes))
        dst.write_bytes(src.read_bytes())
        chat_name = dst.name
    elif "youtube.com" in url or "youtu.be" in url:
        log("Downloading YouTube live chat replay (skipped if the stream has none)...")
        try:
            run(_ytdlp() + ["--no-playlist", "--skip-download", "--write-subs", "--sub-langs", "live_chat",
                            "-o", str(job_dir / "chat"), url])
            found = next(job_dir.glob("chat*.live_chat.json"), None)
            if found:
                chat_name = found.name
        except ClipperError as e:
            log(f"No chat replay: {str(e).splitlines()[0]}")
    else:
        log("No chat file given. Highlights will use audio only. For Twitch chat, see README "
            "(TwitchDownloaderCLI) and pass --chat.")

    meta = {
        "url": info.get("webpage_url") or url,
        "title": title,
        "uploader": info.get("uploader"),
        "streamer": streamer,
        "video": video.name,
        "chat": chat_name,
        "offset": offset,
    }
    job = Job.create(job_dir, meta)
    log(f"Job ready: {job_dir}  (chat: {'yes' if chat_name else 'no'})")
    return job


def adopt_local(video_path: str, streamer: str | None, url: str = "", chat_file: str | None = None,
                work_dir: Path = WORK_DIR) -> Job:
    """Make a job from a video you already have (e.g. your own recording)."""
    src = Path(video_path)
    if not src.exists():
        raise ClipperError(f"{src} not found")
    job_dir = work_dir / f"{slugify(streamer or 'local', 20)}-{slugify(src.stem, 30)}"
    job_dir.mkdir(parents=True, exist_ok=True)
    dst = job_dir / ("video" + src.suffix)
    if not dst.exists():
        try:
            dst.symlink_to(src.resolve())
        except OSError:
            dst.write_bytes(src.read_bytes())
    chat_name = None
    if chat_file:
        c = Path(chat_file)
        cdst = job_dir / ("chat" + "".join(c.suffixes))
        cdst.write_bytes(c.read_bytes())
        chat_name = cdst.name
    return Job.create(job_dir, {"url": url, "title": src.stem, "uploader": None, "streamer": streamer,
                                "video": dst.name, "chat": chat_name, "offset": 0.0})


def latest_vods(count: int = 5) -> None:
    """Print the newest VODs for each streamer in channels.toml, marking ones already processed."""
    streamers = load_streamers()
    if not streamers:
        raise ClipperError("No streamers in channels.toml yet. Copy channels.example.toml to channels.toml.")
    done_urls = {(read_json(p) or {}).get("url") for p in WORK_DIR.glob("*/job.json")}
    import json
    for s in streamers.values():
        if not s.vods_url:
            continue
        print(f"\n{s.display_name}  ({s.vods_url})")
        try:
            out = run(_ytdlp() + ["--flat-playlist", "--playlist-end", str(count), "-J", s.vods_url]).stdout
        except ClipperError as e:
            print(f"  could not list VODs: {str(e).splitlines()[0]}")
            continue
        for e in json.loads(out).get("entries") or []:
            u = e.get("url") or e.get("webpage_url") or ""
            dur = hms(e["duration"]) if e.get("duration") else "?"
            mark = "done" if u in done_urls else "new "
            print(f"  [{mark}] {dur:>8}  {e.get('title', '')[:70]}\n           {u}")
