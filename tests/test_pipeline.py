"""End-to-end test on a generated 'stream' with loud moments + chat spikes at known times."""
import json
import random
import subprocess
from pathlib import Path

import numpy as np
import pytest

from clipper.common import Job, ffprobe_duration, ffprobe_size, write_json
from clipper.download import adopt_local
from clipper.highlights import chat_activity, find, load_chat, pick_moments, score_seconds
from clipper.render import build_ass, render
from clipper.compile import compile_long
from clipper.review import approved_clips, build_page, make_previews

LENGTH = 600
HYPE = [120, 300, 470]   # loud + chat goes crazy
LOUD_ONLY = 210          # loud, chat quiet (Kick-style: no chat)


def make_stream(path: Path) -> None:
    # Quiet noise with loud bursts: volume expression is 1.0 during bursts, 0.05 otherwise.
    loud = "+".join(f"between(t,{t},{t + 6})" for t in HYPE + [LOUD_ONLY])
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration={LENGTH}",
        "-f", "lavfi", "-i", f"anoisesrc=color=pink:amplitude=0.5:duration={LENGTH}:sample_rate=44100",
        "-af", f"volume='if({loud},1.0,0.05)':eval=frame",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35", "-g", "60",
        "-c:a", "aac", "-shortest", str(path)], check=True)


def make_chat(path: Path) -> None:
    rnd = random.Random(1)
    rows = ["seconds,message"]
    for t in range(LENGTH):
        for _ in range(rnd.randint(0, 2)):
            rows.append(f"{t + rnd.random():.2f},hello chat")
    for h in HYPE:
        for _ in range(120):
            t = h + 6 + rnd.random() * 8          # chat reacts a few seconds later
            rows.append(f"{t:.2f},{rnd.choice(['KEKW', 'LMAO', 'clip it', 'NO WAY', 'W'])}")
    path.write_text("\n".join(rows))


@pytest.fixture(scope="module")
def job(tmp_path_factory):
    d = tmp_path_factory.mktemp("stream")
    video, chat = d / "stream.mp4", d / "chat.csv"
    make_stream(video)
    make_chat(chat)
    return adopt_local(str(video), None, url="https://www.twitch.tv/videos/123", chat_file=str(chat),
                       work_dir=d / "work")


def test_find_hits_known_moments(job):
    moments = find(job, count=4, length=30)
    peaks = [m["peak"] for m in moments]
    for h in HYPE + [LOUD_ONLY]:
        assert any(m["start"] <= h + 3 <= m["end"] for m in moments), (h, peaks)
    # chat-backed moments should outrank the audio-only one
    best3 = sorted(moments, key=lambda m: -m["score"])[:3]
    assert all(any(m["start"] <= h + 3 <= m["end"] for h in HYPE) for m in best3)
    assert moments[0]["source"].startswith("https://www.twitch.tv/videos/123?t=")


def test_no_overlap_and_lengths():
    rng = np.random.default_rng(0)
    loud = rng.normal(-40, 2, 3000).astype(np.float32)
    for p in (500, 520, 1500, 2500):
        loud[p:p + 5] = -5
    total, za, zc = score_seconds(loud, None)
    ms = pick_moments(total, loud, count=10, length=40, za=za, zc=zc)
    for a, b in zip(ms, ms[1:]):
        assert a["end"] <= b["start"]
    assert all(30 <= m["end"] - m["start"] <= 50 for m in ms)


def test_chat_formats(tmp_path):
    yt = tmp_path / "c.live_chat.json"
    yt.write_text(json.dumps({"replayChatItemAction": {"videoOffsetTimeMsec": "61500", "actions": [
        {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {"message": {"runs": [{"text": "clip it LMAO"}]}}}}}]}}) + "\n")
    tw = tmp_path / "chat.json"
    tw.write_text(json.dumps({"comments": [{"content_offset_seconds": 12.3, "message": {"body": "KEKW"}}]}))
    assert load_chat(yt) == [(61.5, "clip it LMAO")]
    assert load_chat(tw) == [(12.3, "KEKW")]
    act = chat_activity(load_chat(yt), 100)
    assert act[61] == 1 + 1 + 4  # message + hype word + "clip it"


def test_ass_captions():
    words = [{"start": 0.2, "end": 0.5, "text": "no"}, {"start": 0.5, "end": 0.8, "text": "way"},
             {"start": 0.8, "end": 1.2, "text": "{bro}"}, {"start": 3.0, "end": 3.4, "text": "what"}]
    ass = build_ass(words, "@Streamer", 1080, 1920, 10, vertical=True)
    assert "PlayResY: 1920" in ass and "@Streamer" in ass
    assert "{\\c&H00F5FF&}NO{\\c&HFFFFFF&} WAY (BRO)" in ass
    assert ass.count("Caption,,") == 4


def test_review_render_compile(job):
    if not job.candidates_file.exists():
        find(job, count=4, length=30)
    cands = json.loads(job.candidates_file.read_text())["clips"]
    make_previews(job, cands[:2])
    assert (job.path / "previews" / f"{cands[0]['id']}.mp4").exists()
    page = build_page(job).read_text()
    assert "Save approved clips" in page

    # Approve 3 clips the way the review page would save them; trim one.
    saved = [{"id": c["id"], "approved": i < 3, "title": f"Moment {i}", "start": c["start"],
              "end": c["end"] - (5 if i == 0 else 0), "formats": ["short", "long"]} for i, c in enumerate(cands)]
    write_json(job.approved_file, {"clips": saved})
    clips = approved_clips(job)
    assert len(clips) == 3

    made = render(job, ("short", "long"), layout="blur", captions=False)
    shorts = [m for m in made if m.name.endswith("_short.mp4")]
    longs = [m for m in made if m.name.endswith("_long.mp4")]
    assert len(shorts) == 3 and len(longs) == 3
    assert ffprobe_size(shorts[0]) == (1080, 1920)
    assert ffprobe_size(longs[0]) == (1920, 1080)
    assert abs(ffprobe_duration(shorts[0]) - (clips[0]["end"] - clips[0]["start"])) < 0.6
    meta = json.loads(shorts[0].with_suffix(".json").read_text())
    assert "#shorts" in meta["description"] and meta["title"] == "Moment 0"

    render(job, ("short",), layout="split", facecam=[0.75, 0.0, 0.25, 0.3], captions=False, force=True)
    assert ffprobe_size(shorts[0]) == (1080, 1920)
    render(job, ("short",), layout="center", captions=False, force=True)
    assert ffprobe_size(shorts[0]) == (1080, 1920)

    out = compile_long(job)
    total = sum(ffprobe_duration(p) for p in longs)
    assert abs(ffprobe_duration(out) - total) < 1.5
    desc = json.loads(out.with_suffix(".json").read_text())["description"]
    assert desc.splitlines()[2].startswith("0:00 Moment 0")
