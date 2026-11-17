"""clipper - turn live stream VODs into YouTube Shorts and highlight videos.

Typical flow:
    clipper download URL --streamer NAME      # get the VOD (+ chat)
    clipper find work/JOB                     # pick the best moments
    clipper review work/JOB                   # approve / trim / title them in your browser
    clipper render work/JOB                   # make Shorts + 16:9 clips
    clipper compile work/JOB                  # join 16:9 clips into one long video
    clipper upload work/JOB --what shorts     # upload (private by default)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .common import ClipperError, Job, parse_time


def _facecam(text: str | None):
    if not text:
        return None
    vals = [float(v) for v in text.split(",")]
    if len(vals) != 4:
        raise argparse.ArgumentTypeError("facecam must be x,y,w,h (fractions, e.g. 0,0,0.25,0.3)")
    return vals


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="clipper", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("download", help="download a VOD (YouTube / Twitch / Kick) into a new job")
    d.add_argument("url")
    d.add_argument("--streamer", help="key from channels.toml (used for credit)")
    d.add_argument("--from", dest="start", help="only download from this time, e.g. 1:30:00")
    d.add_argument("--to", dest="end", help="only download up to this time, e.g. 3:00:00")
    d.add_argument("--chat", help="chat replay file (TwitchDownloader JSON, or CSV 'seconds,message')")
    d.add_argument("--max-height", type=int, default=1080)

    lo = sub.add_parser("local", help="make a job from a video file you already have")
    lo.add_argument("video")
    lo.add_argument("--streamer")
    lo.add_argument("--url", default="", help="link to the original stream (goes in descriptions)")
    lo.add_argument("--chat")

    sub.add_parser("check", help="list newest VODs for the streamers in channels.toml")

    f = sub.add_parser("find", help="find the best moments")
    f.add_argument("job", type=Path)
    f.add_argument("--count", type=int, default=15, help="how many candidates (default 15)")
    f.add_argument("--length", type=int, default=35, help="clip length in seconds (default 35)")
    f.add_argument("--chat-delay", type=int, default=6, help="seconds chat lags behind the stream")
    f.add_argument("--min-score", type=float, default=2.0, help="lower = more (weaker) candidates")
    f.add_argument("--skip-start", default="0", help="ignore the start, e.g. 5:00 for 'starting soon' screens")
    f.add_argument("--skip-end", default="0", help="ignore the end")

    r = sub.add_parser("review", help="preview candidates and approve them in your browser")
    r.add_argument("job", type=Path)
    r.add_argument("--port", type=int, default=8765)
    r.add_argument("--no-browser", action="store_true")

    rd = sub.add_parser("render", help="make the final videos from approved clips")
    rd.add_argument("job", type=Path)
    rd.add_argument("--format", choices=["short", "long", "both"], default="both")
    rd.add_argument("--layout", choices=["blur", "center", "split"], help="Shorts layout (default from channels.toml, else blur)")
    rd.add_argument("--facecam", type=_facecam, help="for split layout: x,y,w,h as fractions, e.g. 0.75,0,0.25,0.3")
    rd.add_argument("--no-captions", action="store_true")
    rd.add_argument("--whisper-model", default="small", help="tiny/base/small/medium/large-v3")
    rd.add_argument("--language", help="force caption language, e.g. en")
    rd.add_argument("--force", action="store_true", help="re-render even if the file exists")

    c = sub.add_parser("compile", help="join 16:9 clips into one long highlights video")
    c.add_argument("job", type=Path)
    c.add_argument("--title")

    u = sub.add_parser("upload", help="upload to YouTube (private by default)")
    u.add_argument("job", type=Path)
    u.add_argument("--what", choices=["shorts", "long", "compilation"], default="shorts")
    u.add_argument("--privacy", choices=["private", "unlisted", "public"], default="private")
    u.add_argument("--publish-at", help="schedule, ISO time e.g. 2026-11-20T17:00:00Z")
    u.add_argument("--dry-run", action="store_true")

    a = sub.add_parser("auto", help="download + find + review in one go")
    a.add_argument("url")
    a.add_argument("--streamer")
    a.add_argument("--from", dest="start")
    a.add_argument("--to", dest="end")
    a.add_argument("--chat")
    a.add_argument("--count", type=int, default=15)
    a.add_argument("--length", type=int, default=35)

    args = p.parse_args(argv)
    try:
        if args.cmd == "download":
            from .download import download
            job = download(args.url, args.streamer, args.start, args.end, args.chat, args.max_height)
            print(job.path)
        elif args.cmd == "local":
            from .download import adopt_local
            print(adopt_local(args.video, args.streamer, args.url, args.chat).path)
        elif args.cmd == "check":
            from .download import latest_vods
            latest_vods()
        elif args.cmd == "find":
            from .highlights import find
            find(Job(args.job), args.count, args.length, args.chat_delay, args.min_score,
                 int(parse_time(args.skip_start)), int(parse_time(args.skip_end)))
        elif args.cmd == "review":
            from .review import review
            review(Job(args.job), args.port, open_browser=not args.no_browser)
        elif args.cmd == "render":
            from .render import render
            fmts = ("short", "long") if args.format == "both" else (args.format,)
            render(Job(args.job), fmts, args.layout, args.facecam, not args.no_captions,
                   args.whisper_model, args.language, args.force)
        elif args.cmd == "compile":
            from .compile import compile_long
            compile_long(Job(args.job), args.title)
        elif args.cmd == "upload":
            from .upload import upload
            upload(Job(args.job), args.what, args.privacy, args.publish_at, dry_run=args.dry_run)
        elif args.cmd == "auto":
            from .download import download
            from .highlights import find
            from .review import review
            job = download(args.url, args.streamer, args.start, args.end, args.chat)
            find(job, args.count, args.length)
            review(job)
    except ClipperError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
