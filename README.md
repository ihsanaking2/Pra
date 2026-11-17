# Clipper: a live stream clipping toolkit

Turns long live stream VODs from **YouTube Live, Twitch and Kick** into:

- **YouTube Shorts**: vertical 1080×1920, with word-by-word captions and the streamer's credit on screen
- **Long highlight videos**: 16:9 clips joined into one video, with YouTube chapters

It finds the best moments automatically, and **you approve every clip** before anything is made or uploaded.

```
download ──► find ──► review (you) ──► render ──► compile ──► upload (private)
  VOD+chat   loud +    approve, trim,   Shorts +   long       to YouTube
             chat      add titles       16:9 clips  video
             spikes
```

---

## 1. Install (once)

You need **Python 3.11+** and **ffmpeg** on your computer.

| OS | ffmpeg |
|---|---|
| Windows | `winget install Gyan.FFmpeg` |
| macOS | `brew install ffmpeg` |
| Linux | `sudo apt install ffmpeg` |

Then, in this folder:

```bash
pip install -e ".[all]"          # everything: captions (Whisper) + YouTube upload
# or just: pip install -e .      # no captions, no upload
cp channels.example.toml channels.toml
```

Edit `channels.toml` and add each streamer you have permission to clip (see section 7).

## 2. Download a stream

```bash
clipper download "https://www.twitch.tv/videos/2345678901" --streamer example
clipper download "https://www.youtube.com/watch?v=XXXXXXXXXXX" --streamer example
clipper download "https://kick.com/example/videos/abcd-1234" --streamer kickexample
```

- 8-hour stream? Grab just part of it: `--from 1:00:00 --to 3:30:00`
- `clipper check` lists the newest VODs for every streamer in `channels.toml`.
- Already have the file? `clipper local my_stream.mp4 --streamer example --url "https://…"`

This prints a job folder like `work/example-v2345678901`. Everything for that stream goes in that folder.

### Chat replay (makes highlight picking much better)
| Platform | How |
|---|---|
| YouTube | Downloaded automatically |
| Twitch | Install [TwitchDownloaderCLI](https://github.com/lay295/TwitchDownloader), then `TwitchDownloaderCLI chatdownload --id 2345678901 -o chat.json` and add `--chat chat.json` to `clipper download` |
| Kick | No easy export. Audio alone still works well |

## 3. Find the best moments

```bash
clipper find work/example-v2345678901 --count 20 --length 35 --skip-start 5:00
```

Each second of the stream gets a score:
- **Audio**: how much louder it is than the few minutes around it (screams, laughs, hype). Quiet and loud streamers both work.
- **Chat**: bursts of messages, weighted up for `KEKW`, `LMAO`, `W`, 😂, 💀, and **`clip it`** (strongest signal). Chat reacts late, so it's shifted back 6s (`--chat-delay`).

Clips start a bit before the peak (the build-up) and end after it (the reaction), on a quiet second so nobody gets cut off mid-word.

## 4. Review and approve (you)

```bash
clipper review work/example-v2345678901
```

A page opens in your browser with a preview of every moment. For each one:
- tick **Approve**
- write the **Title** (this matters a lot for views)
- fix **Start/End** if needed (the preview starts 5s early so you can see what to trim)
- choose **Short** and/or **Long video**

Click **Save approved clips**, then press Ctrl+C in the terminal.

## 5. Render

```bash
clipper render work/example-v2345678901                      # Shorts + 16:9
clipper render work/example-v2345678901 --format short --layout split --facecam 0.75,0,0.25,0.3
```

Shorts layouts:

| Layout | Looks like | Best for |
|---|---|---|
| `blur` | full frame in the middle, blurred copy behind it | anything (safe default) |
| `center` | middle of the frame cropped to fill the screen | just chatting, IRL |
| `split` | facecam on top, gameplay below | gaming streams with a facecam |

Captions come from Whisper running on your computer (`--whisper-model small` by default, `medium` is more accurate but slower). Use `--no-captions` to skip them.

Output goes to `work/…/out/`: `c01_short.mp4`, `c01_long.mp4`, plus a `.json` next to each with the title, description and tags. You can edit those before uploading.

## 6. Long video + upload

```bash
clipper compile work/example-v2345678901 --title "ExampleStreamer's CRAZIEST Stream Yet"
clipper upload  work/example-v2345678901 --what shorts --dry-run   # check first
clipper upload  work/example-v2345678901 --what shorts             # private uploads
clipper upload  work/example-v2345678901 --what compilation --publish-at 2026-11-20T17:00:00Z
```

Uploads are **private by default**. Check them in YouTube Studio, then publish. Already uploaded files are remembered and skipped.

### Uploading to YouTube: one-time setup
1. Go to [console.cloud.google.com](https://console.cloud.google.com), create a project.
2. **APIs & Services → Library** → enable **YouTube Data API v3**.
3. **OAuth consent screen** → External → add your own Google account as a test user.
4. **Credentials → Create credentials → OAuth client ID → Desktop app** → download the JSON, save as `client_secret.json` in this folder.
5. The first `clipper upload` opens a browser to sign in to your channel.

The free API quota allows roughly **6 uploads a day**. That's plenty, since posting 2–4 Shorts a day is a good pace anyway.

## 7. Staying safe: permission and copyright

Clip channels get taken down when they upload streams they weren't allowed to use. Protect the channel:

- **Only clip streamers who allow it.** Many have clipping programs or rules in their Discord. Write where they said so in the `permission` field of `channels.toml` and keep a screenshot.
- **Follow each streamer's rules.** Some require a credit format, a link, or no monetization, or ban certain topics.
- **Always credit.** Clipper puts the name on screen and the streamer and stream links in every description automatically.
- **Watch out for music.** Streams often have copyrighted music playing, which gets Content ID claims even when the streamer is fine with clipping. Skip or trim those moments.
- **Add something of your own.** Good titles, captions and edits (and later, your own commentary) make the channel stronger and look less like a re-upload.

## Everyday workflow

```bash
clipper check                                           # anything new?
clipper auto "https://www.twitch.tv/videos/…" --streamer example --chat chat.json   # download+find+review
clipper render work/<job>
clipper upload work/<job> --what shorts
```

## Tips for growing a clip channel
- **Speed wins.** Post the best Shorts within hours of the stream ending.
- **The first 2 seconds decide everything.** Trim the start so the clip opens right on the action.
- **Titles**: short, curious, no spoilers ("He didn't expect THIS" beats "Funny moment at 2:13").
- **Consistency**: 1–3 Shorts a day, and one long highlights video a week.
- Focus on 1–3 streamers at first, so viewers know what your channel is for.

## Tests

```bash
pip install -e ".[dev]" && pytest
```

The tests build a fake 10-minute stream with known loud moments and chat spikes, then check that `find` catches them and that rendering gives correct 1080×1920 and 1920×1080 videos.
