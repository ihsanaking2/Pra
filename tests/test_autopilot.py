"""Autopilot run with the network parts replaced by a local fake stream."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import clipper.autopilot as ap
from clipper.common import ffprobe_size

LOUD = [150, 400]


@pytest.fixture
def fake_stream(tmp_path):
    video = tmp_path / "stream.mp4"
    loud = "+".join(f"between(t,{t},{t + 6})" for t in LOUD)
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=600",
        "-f", "lavfi", "-i", "anoisesrc=color=pink:amplitude=0.5:duration=600",
        "-af", f"volume='if({loud},1.0,0.05)':eval=frame",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35", "-c:a", "aac", "-shortest", str(video)], check=True)
    return video


def test_autopilot(tmp_path, fake_stream, monkeypatch):
    config = tmp_path / "autopilot.toml"
    config.write_text('[streamers.tester]\ndisplay_name = "Tester"\ncredit = "@Tester"\n'
                      'vods_url = "https://www.twitch.tv/tester/videos"\n')
    vod = {"id": "twitch-v1", "url": "https://www.twitch.tv/videos/1", "title": "t", "platform": "twitch",
           "src": str(fake_stream)}
    monkeypatch.setattr(ap, "list_vods", lambda s, limit=1: [vod])
    monkeypatch.setattr(ap, "fetch_chat", lambda v, f, timeout_min=30: None)

    def fake_audio(v, folder):
        folder.mkdir(parents=True, exist_ok=True)
        dst = folder / "audio.mp4"
        shutil.copy(v["src"], dst)
        return dst

    def fake_section(v, start, end, folder, name):
        folder.mkdir(parents=True, exist_ok=True)
        dst = folder / f"{name}.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-i", v["src"], "-t", str(end - start),
                        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(dst)], check=True)
        return dst

    monkeypatch.setattr(ap, "fetch_audio", fake_audio)
    monkeypatch.setattr(ap, "fetch_section", fake_section)

    out, state = tmp_path / "out", tmp_path / "state.json"
    clips = ap.autopilot(config, out, state, tmp_path / "work", clips_per_day=1, length=30)
    assert len(clips) == 1
    c = clips[0]
    assert ffprobe_size(out / c["file"]) == (1080, 1920)
    assert "#shorts" in c["description"] and "with permission" not in c["description"]
    assert c["source"].startswith("https://www.twitch.tv/videos/1?t=")
    st = json.loads(state.read_text())
    assert st["done_vods"] == ["twitch-v1"]
    # the other loud moment is saved for tomorrow
    assert any(b["start"] <= LOUD[1] + 3 <= b["end"] for b in st["backlog"])
    backlog_before = len(st["backlog"])

    # Next day: no new VOD, but the backlog moment gets used.
    clips2 = ap.autopilot(config, out, state, tmp_path / "work", clips_per_day=1, length=30)
    assert len(clips2) == 1 and clips2[0]["source"] != c["source"]
    assert len(json.loads(state.read_text())["backlog"]) == backlog_before - 1


def test_choose_takes_turns():
    cands = [{"streamer": "a", "score": 9}, {"streamer": "a", "score": 8}, {"streamer": "a", "score": 7},
             {"streamer": "b", "score": 3}]
    assert [c["streamer"] for c in ap.choose(cands, 3)] == ["a", "b", "a"]


def test_auto_title():
    words = [{"start": 1.0, "end": 1.2, "text": "bro"}, {"start": 1.3, "end": 1.5, "text": "what"},
             {"start": 1.6, "end": 1.9, "text": "is"}, {"start": 2.0, "end": 2.2, "text": "that?"}]
    t = ap.auto_title("Kai Cenat", words, 2.0, "x")
    assert t.startswith('Kai Cenat: "BRO WHAT IS THAT?"')
    assert "Kai Cenat" in ap.auto_title("Kai Cenat", [], 2.0, "x")
