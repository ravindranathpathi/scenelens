---
description: Watch a video (URL or local path). Picks frames at scene changes, OCRs each frame, pulls captions or Whisper transcript (auto-chunked), and answers questions grounded in what's actually on screen and said.
argument-hint: <video-url-or-path> [question]
allowed-tools: [Bash, Read, AskUserQuestion]
---

Invoke the `vidsense` skill (defined in SKILL.md) with the user's arguments: $ARGUMENTS

Follow the skill's full pipeline: preflight setup check → download via yt-dlp → scene-aware frame extraction (fallback to fixed-fps if scenes are sparse) → OCR pass on each frame (skipped if tesseract missing) → captions or Whisper transcript (auto-chunked for long audio) → Read each frame → answer the user grounded in frames + OCR + transcript. If the user provided no arguments, ask them for a video URL or local path before proceeding.
