#!/usr/bin/env python3
"""OCR extracted frames with Tesseract.

Why this exists: when a frame contains slides, code, terminal output, or any
on-screen text, vision tokens are an expensive way to read it. A 30 ms
Tesseract pass yields the text directly, so Claude can reason over the
substance instead of decoding pixels.

Tesseract is OPTIONAL. If it's not installed, OCR returns None for every
frame and the rest of the pipeline keeps working — just without the text
sidechannel.
"""
from __future__ import annotations

import concurrent.futures as cf
import shutil
import subprocess
from pathlib import Path


MIN_WORDS = 3            # below this = noise (compression artifacts, watermarks)
MAX_OCR_WORKERS = 4
TESSERACT_TIMEOUT = 15   # per-frame, seconds


def is_available() -> bool:
    return shutil.which("tesseract") is not None


def ocr_one(path: str, lang: str = "eng") -> str | None:
    """Run Tesseract on a single image. Returns None on noise / failure."""
    if not is_available():
        return None
    try:
        result = subprocess.run(
            ["tesseract", path, "stdout", "-l", lang, "--psm", "6"],
            capture_output=True,
            text=True,
            timeout=TESSERACT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None
    except OSError:
        return None

    if result.returncode != 0:
        return None

    text = (result.stdout or "").strip()
    if not text:
        return None

    # Tesseract on photographs returns short garbage. Filter sub-3-word output
    # to keep the report readable; drops watermarks ("HD", "©") too.
    words = text.split()
    if len(words) < MIN_WORDS:
        return None

    # Collapse runs of repeated whitespace; preserve newlines because slide
    # structure (bullets, code blocks) reads better with line breaks intact.
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def ocr_frames(frames: list[dict], lang: str = "eng") -> list[dict]:
    """Run OCR on every frame in parallel. Mutates each frame dict with `ocr`."""
    if not is_available():
        for f in frames:
            f["ocr"] = None
        return frames

    paths = [f["path"] for f in frames]
    results: dict[str, str | None] = {}

    workers = min(MAX_OCR_WORKERS, max(1, len(paths)))
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        for path, text in zip(paths, pool.map(lambda p: ocr_one(p, lang), paths)):
            results[path] = text

    for f in frames:
        f["ocr"] = results.get(f["path"])
    return frames


def summarize_ocr(frames: list[dict]) -> dict:
    total = len(frames)
    with_text = sum(1 for f in frames if f.get("ocr"))
    chars = sum(len(f["ocr"]) for f in frames if f.get("ocr"))
    return {"frames_total": total, "frames_with_text": with_text, "ocr_chars": chars}


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) < 2:
        print("usage: ocr.py <frame-path> [<frame-path> ...]")
        raise SystemExit(2)
    if not is_available():
        print("tesseract not installed", flush=True)
        raise SystemExit(1)
    out = [{"path": p, "ocr": ocr_one(p)} for p in sys.argv[1:]]
    print(json.dumps(out, indent=2))
