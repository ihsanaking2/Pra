"""Shared helpers: running ffmpeg, job folders, time formatting, config."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

WORK_DIR = Path("work")
CONFIG_FILE = Path("channels.toml")


class ClipperError(RuntimeError):
    pass


def need(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise ClipperError(f"'{tool}' is not installed or not on PATH.")
    return path


def run(cmd: list[str], cwd: Path | None = None, quiet: bool = True) -> subprocess.CompletedProcess:
    """Run a command and raise a readable error if it fails."""
    proc = subprocess.run(
        [str(c) for c in cmd],
        cwd=cwd,
        stdout=subprocess.PIPE if quiet else None,
        stderr=subprocess.PIPE if quiet else None,
        text=True,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-2000:]
        raise ClipperError(f"Command failed ({proc.returncode}): {' '.join(map(str, cmd[:6]))} ...\n{tail}")
    return proc


def ffprobe_duration(path: Path) -> float:
    out = run([need("ffprobe"), "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", path]).stdout.strip()
    return float(out)


def ffprobe_size(path: Path) -> tuple[int, int]:
    out = run([need("ffprobe"), "-v", "error", "-select_streams", "v:0",
               "-show_entries", "stream=width,height", "-of", "csv=p=0", path]).stdout.strip()
    w, h = out.split(",")[:2]
    return int(w), int(h)


def hms(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_time(text: str) -> float:
    """'1:02:03', '62:03', '3723' or '3723.5' -> seconds."""
    parts = text.strip().split(":")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total


def slugify(text: str, max_len: int = 60) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text[:max_len] or "job"


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- config

@dataclass
class Streamer:
    key: str
    display_name: str
    credit: str = ""
    links: list[str] = field(default_factory=list)
    vods_url: str = ""
    permission: str = ""
    layout: str = "blur"
    facecam: list[float] | None = None  # x, y, w, h as fractions of the frame


def load_streamers(path: Path = CONFIG_FILE) -> dict[str, Streamer]:
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    result = {}
    for key, s in data.get("streamers", {}).items():
        result[key] = Streamer(
            key=key,
            display_name=s.get("display_name", key),
            credit=s.get("credit", ""),
            links=list(s.get("links", [])),
            vods_url=s.get("vods_url", ""),
            permission=s.get("permission", ""),
            layout=s.get("layout", "blur"),
            facecam=s.get("facecam"),
        )
    return result


# --------------------------------------------------------------------------- jobs

class Job:
    """A folder under work/ holding one VOD and everything made from it."""

    def __init__(self, path: Path):
        self.path = Path(path)
        if not (self.path / "job.json").exists():
            raise ClipperError(f"{self.path} is not a clipper job (no job.json). Run 'clipper download' first.")
        self.meta = read_json(self.path / "job.json")

    @classmethod
    def create(cls, path: Path, meta: dict) -> "Job":
        path.mkdir(parents=True, exist_ok=True)
        write_json(path / "job.json", meta)
        return cls(path)

    def save(self) -> None:
        write_json(self.path / "job.json", self.meta)

    @property
    def video(self) -> Path:
        return self.path / self.meta["video"]

    @property
    def chat(self) -> Path | None:
        c = self.meta.get("chat")
        return self.path / c if c else None

    @property
    def candidates_file(self) -> Path:
        return self.path / "candidates.json"

    @property
    def approved_file(self) -> Path:
        return self.path / "approved.json"

    @property
    def out_dir(self) -> Path:
        return self.path / "out"

    def streamer(self) -> Streamer | None:
        key = self.meta.get("streamer")
        return load_streamers().get(key) if key else None

    def source_link(self, t: float) -> str:
        """Link to the original VOD at the moment a clip came from."""
        url = self.meta.get("url", "")
        t = int(t + self.meta.get("offset", 0))
        if not url:
            return ""
        if "twitch.tv" in url:
            h, rem = divmod(t, 3600)
            m, s = divmod(rem, 60)
            return f"{url}{'&' if '?' in url else '?'}t={h}h{m}m{s}s"
        if "youtube.com" in url or "youtu.be" in url:
            return f"{url}{'&' if '?' in url else '?'}t={t}s"
        return url
