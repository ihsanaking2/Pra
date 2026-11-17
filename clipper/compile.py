"""Join the approved 16:9 clips into one long highlights video with YouTube chapters."""
from __future__ import annotations

from pathlib import Path

from .common import ClipperError, Job, ffprobe_duration, hms, log, need, read_json, run, write_json


def compile_long(job: Job, title: str | None = None) -> Path:
    metas = sorted((read_json(p) for p in job.out_dir.glob("c*_long.json")), key=lambda m: m["start"])
    metas = [m for m in metas if (job.out_dir / m["file"]).exists()]
    if len(metas) < 2:
        raise ClipperError("Need at least 2 rendered long clips. Run 'clipper render --format long' first.")
    listing = job.out_dir / "concat.txt"
    listing.write_text("".join(f"file '{m['file']}'\n" for m in metas), encoding="utf-8")
    out = job.out_dir / "highlights_long.mp4"
    log(f"Joining {len(metas)} clips...")
    # All clips were encoded with identical settings, so they can be joined without re-encoding.
    run([need("ffmpeg"), "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", listing.name,
         "-c", "copy", "-movflags", "+faststart", out.name], cwd=job.out_dir)

    streamer = job.streamer()
    name = streamer.display_name if streamer else (job.meta.get("uploader") or "Stream")
    chapters, t = [], 0.0
    for m in metas:
        chapters.append(f"{hms(t)} {m['title']}")
        t += ffprobe_duration(job.out_dir / m["file"])
    desc = [f"Best moments from {name}'s stream.", ""]
    if len(chapters) >= 3:  # YouTube needs 3+ chapters starting at 0:00
        desc += chapters + [""]
    desc.append(f"🎥 Streamer: {name}" + (f" ({streamer.credit})" if streamer and streamer.credit else ""))
    if job.meta.get("url"):
        desc.append(f"📺 Full stream: {job.meta['url']}")
    if streamer:
        desc += [f"🔗 {link}" for link in streamer.links]
    desc += ["", f"All credit to {name}." + (" Clipped with permission." if streamer and streamer.permission else "")]
    write_json(out.with_suffix(".json"), {
        "file": out.name, "format": "compilation",
        "title": (title or f"{name} - Best Stream Moments")[:100],
        "description": "\n".join(desc),
        "tags": [name, "stream highlights", "best moments", "compilation"],
    })
    log(f"Made {out} ({hms(ffprobe_duration(out))})")
    return out
