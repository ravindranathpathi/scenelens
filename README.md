# /scenelens

**Give Claude a smarter video input.**

> **New here?** Start with [GETTING_STARTED.md](GETTING_STARTED.md) — install, first command, common patterns, and troubleshooting in one read. The rest of this README is the architecture / design reference.

Most video-for-LLM scripts grab frames at a fixed rate and call it a day. `/scenelens` does three things differently:

1. **Picks frames at scene changes** — content-aware sampling instead of time-uniform sampling. Same token budget, far better signal.
2. **OCRs every frame** — on-screen text (slides, code, terminals, dashboards) is extracted as text alongside the image, so Claude doesn't burn vision tokens reading static pixels.
3. **Auto-chunks long audio** — the 25 MB Whisper API cap no longer kills long videos. They just take more API calls.

Plus a few quieter improvements: only reads `.env` from `~/.config/scenelens/`, validates `--out-dir` against system paths, caches request bodies between Whisper retries.

```
/scenelens https://youtu.be/dQw4w9WgXcQ what hook did they open with?
```

## Install

| Surface | Install |
|---------|---------|
| **Claude Code** | `/plugin marketplace add ravindranathpathi/scenelens` then `/plugin install scenelens@scenelens` |
| **claude.ai** (web) | [Download `scenelens.skill`](https://github.com/ravindranathpathi/scenelens/releases/latest) → Settings → Capabilities → Skills → `+` |
| **Codex** | `git clone https://github.com/ravindranathpathi/scenelens.git ~/.codex/skills/scenelens` |
| **Manual / dev** | `git clone https://github.com/ravindranathpathi/scenelens.git ~/.claude/skills/scenelens` |

On the first run, `setup.py` checks for `ffmpeg`, `ffprobe`, `yt-dlp` (required) and `tesseract` (optional, for OCR). On macOS with Homebrew it auto-installs; on Linux/Windows it prints exact `apt`/`dnf`/`winget`/`pipx` commands.

## How it works

1. **You paste a video and a question.** URL (anything yt-dlp supports) or a local path (`.mp4`, `.mov`, `.mkv`, `.webm`).
2. **`yt-dlp` downloads it** (or the local file is probed in place).
3. **`ffmpeg` extracts frames at scene changes.** Uses ffmpeg's `select=gt(scene,T)` filter with `showinfo` to capture real PTS timestamps. If fewer than 8 scene changes are detected (single-take talking head, static screen recording), automatically falls back to fixed-fps sampling so the budget isn't wasted.
4. **Tesseract OCRs each frame.** Skipped silently if Tesseract isn't installed. Filters out sub-3-word output (compression noise, watermarks).
5. **Transcript comes from one of two places.** First try: yt-dlp pulls native captions. Free, instant. Fallback: ffmpeg extracts mono 16 kHz mp3 (~480 kB/min), then Groq's `whisper-large-v3` (preferred — cheaper, faster) or OpenAI's `whisper-1`. **Audio over 24 MB is auto-split into chunks**, each transcribed separately with offset timestamps merged back together.
6. **Frames + OCR + transcript get handed to Claude.** Each frame line in the report includes the OCR text inline. Claude `Read`s frames in parallel, combines all three signals, answers grounded.
7. **Cleanup.** Working directory printed at end; Claude removes it when no follow-ups are coming.

## What's different vs. the obvious approach

| Concern | Naïve approach | scenelens |
|---------|---------------|----------|
| Frame selection | Fixed fps | Scene-aware with fixed-fps fallback |
| Frame timestamps | Computed from fps | Real PTS via ffmpeg `showinfo` |
| On-screen text | Vision tokens decode pixels | Tesseract OCR runs first |
| Audio over 25 MB | Hard fail | Auto-chunked, merged |
| Whisper retries | Re-read file from disk each attempt | Read once, reuse bytes |
| `.env` resolution | Falls back to cwd | Strictly `~/.config/scenelens/` + env |
| `--out-dir` | Any path the process can write | Refuses `/etc`, `/bin`, `/sbin`, `/usr/{bin,sbin}`, `/boot` |
| Long-video warning | Just prints | Prints + suggests focused mode with `--start`/`--end` |

## Usage

```bash
# Whole-video scene-aware scan with OCR
/scenelens https://youtu.be/dQw4w9WgXcQ what's the hook?
/scenelens ~/screen-recording.mp4 when does the UI break?

# Focused on a section — denser frame budget
/scenelens https://youtu.be/abc --start 2:15 --end 2:45

# Force fixed-fps for a static talking head with no cuts
/scenelens interview.mp4 --mode fixed

# Force scene-only, error if no scenes detected
/scenelens ad.mp4 --mode scene --scene-threshold 0.20

# Skip OCR for content with no on-screen text
/scenelens podcast.mp4 --no-ocr

# Frames-only (no Whisper, no captions = no transcript)
/scenelens video.mp4 --no-whisper
```

Other flags (`scripts/scenelens.py --help` for the full list):

- `--max-frames N` — lower the cap (default 80, hard max 100).
- `--resolution W` — frame width in px (default 512). Bump to 1024 only when OCR doesn't catch what's needed.
- `--scene-threshold F` — sensitivity 0-1 (default 0.30). Lower = more frames captured.
- `--whisper groq|openai` — force a specific Whisper backend.
- `--ocr-lang CODE` — Tesseract language (default `eng`).
- `--sub-langs L1,L2` — caption languages in priority order.
- `--out-dir DIR` — keep working files somewhere specific.

## Frame budget

Tokens are dominated by frames. The script targets a duration-aware budget:

| Duration | Default budget | Strategy |
|----------|----------------|----------|
| ≤30 s | ~30 frames | Scene-aware → falls back to dense fps if static |
| 30 s – 1 min | ~40 frames | Same |
| 1 – 3 min | ~60 frames | Same |
| 3 – 10 min | ~80 frames | Scene-aware shines here |
| > 10 min | 100 frames | Re-run focused with `--start`/`--end` for better results |

Hard caps: 2 fps in fixed-fps mode, 100 frames total. In scene-aware mode the cap is just 100 frames — fps is irrelevant.

## Bring your own keys

Captions cover most public videos for free. Whisper only fires when a video genuinely has no caption track.

| Capability | What you need | Cost |
|------------|---------------|------|
| Download + native captions | `yt-dlp` + `ffmpeg` | Free |
| OCR on frames | `tesseract` | Free, optional |
| Whisper fallback (preferred) | [Groq API key](https://console.groq.com/keys) — `whisper-large-v3` | Cheap, fast |
| Whisper fallback (alt) | [OpenAI API key](https://platform.openai.com/api-keys) — `whisper-1` | Standard pricing |
| Disable Whisper entirely | `--no-whisper` | Free, frames-only when no captions |

Keys live at `~/.config/scenelens/.env`, mode `0600` on POSIX. The skill does NOT read `.env` from the current working directory — that's deliberate, to avoid silently picking up keys from random project dirs.

## Limits

- **Best accuracy: under 10 minutes.** Past that, even scene-aware extraction is sparse on long-form content. Re-run focused.
- **Hard caps: 100 frames total, 2 fps in fixed-fps mode.**
- **Whisper chunk size: 24 MB** (1 MB headroom under the 25 MB API cap). Audio above this is split automatically.
- **No private platforms.** Public URLs and local files only. If yt-dlp can't reach it without auth, neither can `/scenelens`.

## Structure

```
.
├── SKILL.md                 # skill contract — loaded by all three surfaces
├── scripts/
│   ├── scenelens.py          # entry point — orchestrates the full pipeline
│   ├── download.py          # yt-dlp wrapper
│   ├── frames.py            # scene + fixed-fps frame extraction
│   ├── ocr.py               # Tesseract wrapper, parallelized over frames
│   ├── transcribe.py        # VTT parser + dedupe
│   ├── whisper.py           # Groq / OpenAI clients, with chunking + body caching
│   ├── setup.py             # preflight + installer (required + optional binaries)
│   └── build-skill.sh       # build dist/scenelens.skill for claude.ai upload
├── hooks/                   # SessionStart status hook (Claude Code)
├── .claude-plugin/          # plugin.json + marketplace.json
├── .codex-plugin/           # codex packaging
└── .github/workflows/       # release.yml — auto-builds scenelens.skill on tag push
```

## Develop

```bash
bash scripts/build-skill.sh      # → dist/scenelens.skill
```

Releasing: tag `vX.Y.Z`, push the tag. The workflow builds `dist/scenelens.skill` and attaches it to the GitHub release.

See [CHANGELOG.md](CHANGELOG.md).

## License

MIT. Built on `yt-dlp`, `ffmpeg`, `tesseract`, and Claude's multimodal `Read` tool. Whisper transcription via [Groq](https://groq.com) or [OpenAI](https://openai.com).

Inspired by [bradautomates/claude-video](https://github.com/bradautomates/claude-video) — a fixed-fps video skill for Claude. scenelens rebuilds the pipeline around scene-aware extraction, OCR, and audio chunking.
