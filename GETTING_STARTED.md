# Getting started with vidsense

A first-run guide. If you've never used vidsense before, read this once, then never again.

---

## In one sentence

`/vidsense` lets Claude actually watch a video — by URL or local file — and answer questions about it grounded in what's on screen and what's said.

---

## Install (pick your surface)

### Claude Code

```
/plugin marketplace add YOUR_GITHUB_HANDLE/vidsense
/plugin install vidsense@vidsense
```

That's it. The skill is registered. First time you type `/vidsense ...` it'll auto-check setup and walk you through any missing pieces.

### claude.ai (web)

1. Download `vidsense.skill` from the [latest release](https://github.com/YOUR_GITHUB_HANDLE/vidsense/releases/latest).
2. Open Settings → Capabilities → Skills.
3. Drag `vidsense.skill` into the upload area.
4. **Enable "Code execution and file creation"** in Capabilities — vidsense shells out to `ffmpeg` and `yt-dlp`, so it won't run without it.

### Codex (and other generic skill loaders)

```bash
git clone https://github.com/YOUR_GITHUB_HANDLE/vidsense.git ~/.codex/skills/vidsense
```

### Manual / dev install

```bash
git clone https://github.com/YOUR_GITHUB_HANDLE/vidsense.git ~/.claude/skills/vidsense
```

---

## First-run setup (60 seconds)

vidsense needs three command-line tools and one optional API key:

| Tool | Required? | Why |
|---|---|---|
| `ffmpeg` + `ffprobe` | Yes | Frame extraction, audio extraction, video metadata |
| `yt-dlp` | Yes | Downloading videos from URLs |
| `tesseract` | Optional | OCR text from on-screen content (slides, code, terminals) |
| Whisper API key | Optional | Transcription when a video has no captions |

When you run `/vidsense` for the first time and any of these are missing, vidsense will tell you the exact one-line install command for your platform.

To trigger this proactively without running the skill, run the installer directly:

**macOS** (auto-installs via Homebrew):
```bash
python3 ~/.claude/skills/vidsense/scripts/setup.py
```

**Linux**:
```bash
sudo apt install ffmpeg tesseract-ocr
pipx install yt-dlp     # or: pip install --user yt-dlp
```

**Windows** (run in PowerShell or cmd):
```powershell
winget install Gyan.FFmpeg
winget install yt-dlp.yt-dlp
winget install UB-Mannheim.TesseractOCR
```

Then, optionally, add a Whisper API key to `~/.config/vidsense/.env`:

```bash
GROQ_API_KEY=gsk_...      # preferred — cheaper, faster (console.groq.com/keys)
OPENAI_API_KEY=sk-...     # fallback (platform.openai.com/api-keys)
```

You don't need a key if every video you watch has captions (most YouTube does). The key is only used when captions are absent — local files, TikToks, some Vimeo, the occasional caption-less YouTube upload.

---

## Your first vidsense command

Pick a short YouTube video. Try this one — it works as a tutorial because it has captions, has cuts (so scene-aware sampling shines), and has on-screen text (so OCR shines):

```
/vidsense https://www.youtube.com/watch?v=x7X9w_GIm1s what is this video about?
```

Expected behavior:
1. Claude runs `setup.py --check` (silent if you're set up)
2. yt-dlp downloads the video (~3 MB, ~5 seconds)
3. ffmpeg picks frames at scene changes and writes them as JPEGs
4. Tesseract reads any text from each frame
5. yt-dlp's caption file is parsed into timestamped transcript
6. Claude reads each frame as an image and combines everything to answer

You'll get a markdown report you don't usually need to look at directly — Claude reads it and answers your question. The first time, it's worth scrolling through to see what's there.

---

## Reading the report (one-time tour)

If you scroll up after a `/vidsense` call, you'll see Claude's thought process used a structured report. Five sections:

```markdown
# vidsense: video report

- **Source:** ...                 ← what you passed in
- **Title:** ...                  ← from yt-dlp metadata
- **Duration:** 2:23              ← how long the video is
- **Frames:** 12 via scene detection (threshold 0.30, max 30)
- **OCR:** 9/12 frames have text (493 chars total)
- **Transcript:** 71 segments (via captions)
- **Chapters:** 5 (see Chapters section below)

## Chapters
- `00:00` — Intro
- `00:12` — What is Python
...

## Frames
- `frame_0001.jpg` (t=00:08)
  - **OCR:** TIOBE INDEX 2020 PYTHON 11.27% ...
- `frame_0002.jpg` (t=00:20)
  ...

## Transcript
[00:02] python a highlevel interpreted programming language ...
```

Three things to know about the report:

**Frames are JPEGs Claude `Read`s as images.** Each line under `## Frames` is a path to a real file. When Claude reads them, the JPEGs render as images in its context — so it actually sees the video.

**OCR text appears inline next to frames that contain words.** That's how Claude reads slides, code, terminals, and dashboards as text instead of decoding pixels.

**Transcript timestamps line up with frame timestamps.** Claude can cross-reference: "the speaker says X at 0:22 while frame_0003 at t=00:21 shows Y on screen."

---

## Five patterns you'll use most

**1. Summarize a video you don't want to watch**

```
/vidsense https://youtu.be/abc summarize this
```

**2. Zoom into a specific moment**

```
/vidsense https://youtu.be/abc --start 2:15 --end 2:45 what happens here?
```

The video gets downloaded fully (yt-dlp doesn't do partial), but frame extraction and transcript filtering happen only in that 30-second window — so the frame budget is dense, not sparse.

**3. Read on-screen text in a screen recording**

```
/vidsense ~/loom-bug-repro.mp4 --resolution 1024 what error message does the terminal show?
```

The default 512px frame width is fine for normal content; bump to 1024 when text is small enough that Tesseract can't read it.

**4. Watch a long video**

```
/vidsense https://youtu.be/long-podcast-url --start 1:12:00 --end 1:13:00 what was just argued?
```

For anything over ~10 minutes, focus on the part you care about. A sparse 80-frame scan of a 2-hour video is rarely useful; a dense 80-frame scan of one minute is.

**5. Watch something without captions**

```
/vidsense https://www.tiktok.com/@user/video/123 summarize this
```

vidsense automatically falls back to Whisper transcription. Make sure you have `GROQ_API_KEY` set in `~/.config/vidsense/.env`.

---

## Cheat sheet — when to use which flag

| You want to... | Flag |
|---|---|
| Read tiny on-screen text (small code, dense slides) | `--resolution 1024` |
| Focus on a specific moment | `--start MM:SS --end MM:SS` |
| Capture more frames in fast-cut content | `--scene-threshold 0.20` (default 0.30) |
| Force fixed-fps (e.g. for an animation) | `--mode fixed` |
| Save tokens — fewer frames, smaller report | `--max-frames 30` |
| Skip OCR entirely (audio podcasts) | `--no-ocr` |
| Skip Whisper entirely (frames-only) | `--no-whisper` |
| Force OpenAI for transcription | `--whisper openai` |
| Watch in a non-English language | `--sub-langs es,fr,de,...` |
| Keep working files for inspection | `--out-dir ~/some-dir` |

Run `python scripts/vidsense.py --help` for the full reference.

---

## Follow-up questions are free

Once Claude has watched a video, you can ask follow-up questions in the same conversation **without re-running vidsense**. The frames, OCR, and transcript are already in context. Don't re-paste the URL — just ask.

✓ Good:
```
/vidsense https://youtu.be/abc summarize this
[Claude answers]
what specifically did they say at the 1 minute mark?
[Claude answers from existing context — no API calls, no token cost]
```

✗ Wasteful:
```
/vidsense https://youtu.be/abc summarize this
/vidsense https://youtu.be/abc what specifically did they say at the 1 minute mark?
```

---

## When something goes wrong

| Symptom | Likely cause | Fix |
|---|---|---|
| `setup incomplete (missing binaries)` | ffmpeg/yt-dlp not installed | Run the install command for your platform from the table above |
| `setup incomplete (no Whisper API key)` | Caption-less video and no key | Add `GROQ_API_KEY` to `~/.config/vidsense/.env` (or use `--no-whisper`) |
| `Frames: 0` | Auto-mode misfire on weird content | Try `--mode fixed` |
| `OCR: 0/N frames have text` on screen recording | Default 512px resolution is too small | Add `--resolution 1024` |
| `yt-dlp did not produce a video file` | Geo-locked, age-gated, or yt-dlp out of date | `pip install --user --upgrade yt-dlp` |
| `Whisper request failed: HTTP 401` | Invalid or expired API key | Rotate at console.groq.com/keys, update `~/.config/vidsense/.env` |
| `Whisper request failed: HTTP 413` | Audio over 25 MB | vidsense should auto-chunk; if it doesn't, file an issue |
| Em-dashes or arrows crash on Windows | Python < 3.7 | Upgrade Python (`python --version` should be 3.7+) |
| Long video, sparse answer | Frame budget spread too thin | Re-run with `--start`/`--end` on the part you care about |

---

## What vidsense will NOT do

A few things that are not bugs — they're deliberate scope limits:

- **No private platforms.** Only public URLs and local files. If yt-dlp can't reach a video without authentication, neither can vidsense.
- **No account login flows.** No Vimeo password support, no YouTube Premium auth, no Loom team-private-link handling.
- **No telemetry.** vidsense doesn't phone home. The only outbound network calls are: yt-dlp pulling video/captions from the source platform, and (if Whisper fires) audio going to Groq or OpenAI.
- **No upload of raw video to any API.** When Whisper is used, only the extracted mono 16 kHz audio clip leaves your machine — never the video itself.

---

## What's next

- Real use, real videos. Try it on something you actually want to know about.
- See the [README](README.md) for architecture details and the structure of the project.
- Check the [CHANGELOG](CHANGELOG.md) for what's new.
- Roadmap and known limits live with the launch evidence.

If something doesn't work the way this guide says it should: open an issue on the repo. The honest report ("I tried X, expected Y, got Z") is exactly what improves the skill for the next person.
