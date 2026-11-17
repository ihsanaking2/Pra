"""Make small preview videos and a local web page to approve, trim and title clips."""
from __future__ import annotations

import html
import json
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .common import ClipperError, Job, log, need, read_json, run, write_json

PAD = 5  # seconds of extra footage on each side of a preview, so you can see what to trim


def make_previews(job: Job, clips: list[dict], workers: int = 3) -> None:
    pdir = job.path / "previews"
    pdir.mkdir(exist_ok=True)

    def one(c):
        out = pdir / f"{c['id']}.mp4"
        if out.exists():
            return
        ss = max(0.0, c["start"] - PAD)
        dur = c["end"] - c["start"] + 2 * PAD
        run([need("ffmpeg"), "-y", "-v", "error", "-ss", f"{ss:.2f}", "-i", job.video, "-t", f"{dur:.2f}",
             "-vf", "scale=-2:360", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
             "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", out])

    log(f"Making {len(clips)} previews...")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, clips))


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Clip Review</title>
<style>
:root{--bg:#f6f6f4;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e2e2dc;--accent:#d6336c;--ok:#2f9e44}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1f1f1d;--ink:#ececea;--muted:#9a9a94;--line:#33332f;--accent:#f06595;--ok:#51cf66}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,sans-serif}
header{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:17px;margin:0;flex:1;min-width:200px}
.muted{color:var(--muted);font-size:13px}
main{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px;padding:16px;max-width:1500px;margin:auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;display:flex;flex-direction:column}
.card.on{border-color:var(--ok);box-shadow:0 0 0 2px var(--ok) inset}
video{width:100%;aspect-ratio:16/9;background:#000;display:block}
.body{padding:10px 12px;display:grid;gap:8px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
label{font-size:13px;color:var(--muted)}
input[type=text]{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:6px;background:transparent;color:var(--ink);font:inherit}
input.t{width:84px;padding:5px 7px;border:1px solid var(--line);border-radius:6px;background:transparent;color:var(--ink);font:inherit}
button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:6px;padding:6px 10px;cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
.approve{font-weight:600;color:var(--ink)}
.score{margin-left:auto;font-variant-numeric:tabular-nums}
a{color:var(--accent)}
#status{font-size:13px}
</style></head><body>
<header>
  <h1>__TITLE__<div class="muted">__SUB__</div></h1>
  <span id="count" class="muted"></span>
  <button class="primary" onclick="save()">Save approved clips</button>
  <span id="status"></span>
</header>
<main id="list"></main>
<script>
const PAD = __PAD__;
const clips = __CLIPS__;
const saved = __SAVED__;
const byId = Object.fromEntries((saved.clips||[]).map(c => [c.id, c]));
const fmt = s => { s = Math.max(0, Math.round(s)); const h = Math.floor(s/3600), m = Math.floor(s%3600/60), x = s%60;
  return (h ? h + ':' + String(m).padStart(2,'0') : m) + ':' + String(x).padStart(2,'0'); };
const parse = t => t.split(':').reduce((a, p) => a*60 + parseFloat(p||0), 0);
function render(){
  const list = document.getElementById('list');
  for (const c of clips) {
    const s = byId[c.id] || {};
    const el = document.createElement('div');
    el.className = 'card'; el.dataset.id = c.id;
    el.innerHTML = `
      <video src="previews/${c.id}.mp4" controls preload="metadata"></video>
      <div class="body">
        <div class="row"><label class="approve"><input type="checkbox" class="ok"> Approve ${c.id}</label>
          <span class="score muted">score ${c.score} · audio ${c.audio ?? '-'} · chat ${c.chat ?? '-'}</span></div>
        <input type="text" class="title" placeholder="Title (shown on YouTube)">
        <div class="row"><label>Start</label><input class="t start"><label>End</label><input class="t end">
          <span class="muted len"></span></div>
        <div class="row"><label><input type="checkbox" class="short" checked> Short</label>
          <label><input type="checkbox" class="long" checked> Long video</label>
          <button class="seek">Play from start</button></div>
        <div class="muted">Preview starts ${PAD}s before the clip. ${c.source ? `<a href="${c.source}" target="_blank" rel="noopener">Original</a>` : ''}</div>
      </div>`;
    const q = sel => el.querySelector(sel);
    q('.ok').checked = !!s.approved;
    q('.title').value = s.title || c.title || '';
    q('.start').value = fmt(s.start ?? c.start);
    q('.end').value = fmt(s.end ?? c.end);
    if (s.formats) { q('.short').checked = s.formats.includes('short'); q('.long').checked = s.formats.includes('long'); }
    const upd = () => { el.classList.toggle('on', q('.ok').checked);
      const L = parse(q('.end').value) - parse(q('.start').value); q('.len').textContent = Math.round(L) + 's' + (L > 60 && q('.short').checked ? ' (over 60s)' : '');
      document.getElementById('count').textContent = document.querySelectorAll('.ok:checked').length + ' approved'; };
    el.addEventListener('input', upd);
    q('.seek').onclick = () => { const v = q('video'); v.currentTime = Math.max(0, parse(q('.start').value) - c.start + PAD); v.play(); };
    list.appendChild(el); upd();
  }
}
async function save(){
  const out = [];
  for (const el of document.querySelectorAll('.card')) {
    const q = sel => el.querySelector(sel);
    const formats = [q('.short').checked && 'short', q('.long').checked && 'long'].filter(Boolean);
    out.push({ id: el.dataset.id, approved: q('.ok').checked, title: q('.title').value.trim(),
      start: parse(q('.start').value), end: parse(q('.end').value), formats });
  }
  const st = document.getElementById('status');
  try {
    const r = await fetch('save', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({clips: out}) });
    if (!r.ok) throw new Error(await r.text());
    st.textContent = 'Saved ' + out.filter(c => c.approved).length + ' approved clips. You can close this tab.';
  } catch (e) { st.textContent = 'Save failed: ' + e.message; }
}
render();
</script></body></html>
"""


def build_page(job: Job) -> Path:
    data = read_json(job.candidates_file)
    if not data:
        raise ClipperError("No candidates yet. Run 'clipper find' first.")
    saved = read_json(job.approved_file, {"clips": []})
    sub = f"{len(data['clips'])} candidates · chat {'used' if data.get('used_chat') else 'not used'}"
    page = (PAGE.replace("__TITLE__", html.escape(job.meta.get("title") or job.path.name))
                .replace("__SUB__", html.escape(sub))
                .replace("__PAD__", str(PAD))
                .replace("__CLIPS__", json.dumps(data["clips"]).replace("</", "<\\/"))
                .replace("__SAVED__", json.dumps(saved).replace("</", "<\\/")))
    out = job.path / "review.html"
    out.write_text(page, encoding="utf-8")
    return out


def serve(job: Job, port: int = 8765, open_browser: bool = True) -> None:
    root = job.path.resolve()
    approved_file = job.approved_file.resolve()

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(root), **kw)

        def log_message(self, *a):
            pass

        def do_POST(self):
            if self.path.rstrip("/") != "/save":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(length))
                assert isinstance(data.get("clips"), list)
            except Exception:
                self.send_error(400, "bad json")
                return
            write_json(approved_file, data)
            n = sum(1 for c in data["clips"] if c.get("approved"))
            log(f"Saved {n} approved clips to {approved_file}")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/review.html"
    log(f"Review page: {url}   (press Ctrl+C here when you're done)")
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


def review(job: Job, port: int = 8765, serve_page: bool = True, open_browser: bool = True) -> Path:
    data = read_json(job.candidates_file)
    if not data:
        raise ClipperError("No candidates yet. Run 'clipper find' first.")
    make_previews(job, data["clips"])
    page = build_page(job)
    if serve_page:
        serve(job, port=port, open_browser=open_browser)
    return page


def approved_clips(job: Job) -> list[dict]:
    """Approved clips, merged with their candidate info, in timeline order."""
    saved = read_json(job.approved_file)
    if not saved:
        raise ClipperError("Nothing approved yet. Run 'clipper review' and press 'Save approved clips'.")
    cands = {c["id"]: c for c in (read_json(job.candidates_file) or {}).get("clips", [])}
    out = []
    for s in saved["clips"]:
        if not s.get("approved"):
            continue
        c = dict(cands.get(s["id"], {}))
        c.update(s)
        if c["end"] <= c["start"]:
            raise ClipperError(f"Clip {c['id']}: end is before start.")
        c["source"] = job.source_link(c["start"])
        out.append(c)
    out.sort(key=lambda c: c["start"])
    return out
