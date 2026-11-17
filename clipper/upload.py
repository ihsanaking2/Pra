"""Upload rendered videos to YouTube with the YouTube Data API.

One-time setup (see README): create an OAuth "Desktop app" client in Google Cloud,
enable "YouTube Data API v3" and save the file as client_secret.json in this folder.
The first upload opens a browser to sign in; the login is then kept in token.json.

Uploads default to PRIVATE so you can check them in YouTube Studio before publishing.
"""
from __future__ import annotations

from pathlib import Path

from .common import ClipperError, Job, log, read_json, write_json

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def _youtube(client_secret: Path, token: Path):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as e:
        raise ClipperError("Upload needs: pip install google-api-python-client google-auth-oauthlib") from e
    creds = Credentials.from_authorized_user_file(str(token), SCOPES) if token.exists() else None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not client_secret.exists():
                raise ClipperError(f"{client_secret} not found. See 'Uploading to YouTube' in README.md.")
            creds = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES).run_local_server(port=0)
        token.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds)


def upload(job: Job, what: str = "shorts", privacy: str = "private", publish_at: str | None = None,
           client_secret: Path = Path("client_secret.json"), token: Path = Path("token.json"),
           dry_run: bool = False) -> None:
    pattern = {"shorts": "c*_short.json", "long": "c*_long.json", "compilation": "highlights_long.json"}[what]
    metas = sorted(job.out_dir.glob(pattern))
    if not metas:
        raise ClipperError(f"Nothing to upload for '{what}'. Render it first.")
    log_file = job.out_dir / "uploads.json"
    done = read_json(log_file, {})
    yt = None
    for mp in metas:
        meta = read_json(mp)
        video = job.out_dir / meta["file"]
        if meta["file"] in done:
            log(f"  {meta['file']}: already uploaded → https://youtu.be/{done[meta['file']]}")
            continue
        if not video.exists():
            log(f"  {meta['file']}: video file missing, skipping")
            continue
        body = {
            "snippet": {"title": meta["title"][:100], "description": meta["description"][:5000],
                        "tags": meta.get("tags", []), "categoryId": "20"},  # 20 = Gaming
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
        }
        if publish_at:
            body["status"].update({"privacyStatus": "private", "publishAt": publish_at})
        if dry_run:
            log(f"  [dry run] would upload {meta['file']}: {meta['title']!r} ({body['status']['privacyStatus']})")
            continue
        if yt is None:
            yt = _youtube(client_secret, token)
        from googleapiclient.http import MediaFileUpload
        log(f"  uploading {meta['file']}: {meta['title']!r}")
        req = yt.videos().insert(part="snippet,status", body=body,
                                 media_body=MediaFileUpload(str(video), chunksize=8 * 1024 * 1024, resumable=True))
        resp = None
        while resp is None:
            status, resp = req.next_chunk()
            if status:
                log(f"    {int(status.progress() * 100)}%")
        done[meta["file"]] = resp["id"]
        write_json(log_file, done)
        log(f"    done → https://youtu.be/{resp['id']}")
