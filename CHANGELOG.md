# Changelog

All notable changes to `/scenelens` are documented here.

## [0.1.0] — Initial release

### Added
- `/scenelens <url-or-path> [question]` slash command.
- Scene-aware frame extraction (`ffmpeg select=gt(scene,T)` + `showinfo`) as the default. Real PTS timestamps instead of computed-from-fps approximations.
- Automatic fallback to fixed-fps when fewer than 8 scene changes are detected (single-take talking heads, static screen recordings).
- Tesseract OCR pass on every frame, parallelized 4-way. Skipped silently if `tesseract` isn't installed. Sub-3-word output filtered as noise.
- Whisper auto-chunking: audio over 24 MB is split into chunks under the 25 MB API cap, each transcribed separately and merged with offset timestamps. A 4-hour podcast no longer fails outright.
- Whisper retry hardening: file body is read once and reused across attempts (was re-read on every retry in the inspiration's pipeline).
- `~/.config/scenelens/.env` only — no cwd `.env` fallback. Avoids silently picking up keys from random project directories.
- `--out-dir` validation: refuses paths under `/etc`, `/bin`, `/sbin`, `/usr/{bin,sbin}`, `/boot`.
- Optional binary detection in `setup.py` — Tesseract install hints printed without blocking install.
- yt-dlp chapters surfaced in the report when present.
- `--mode auto|scene|fixed`, `--scene-threshold`, `--no-ocr`, `--ocr-lang`, `--sub-langs` flags.
- `setup.py --check` (silent preflight, exits 0/2/3/4), `--json` (structured), and installer.
- SessionStart hook prints one-line status when partially configured.
- `.skill` bundle packaging for claude.ai upload via `scripts/build-skill.sh`.
- GitHub Actions workflow auto-builds `dist/scenelens.skill` on tag push and attaches to the release.
