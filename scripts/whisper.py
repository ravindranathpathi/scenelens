#!/usr/bin/env python3
"""Transcribe a video via Groq or OpenAI Whisper API.

Hardening over the original /watch design:
  - Audio body is read once and reused across retries (no re-read on each attempt).
  - Audio over the API's 25 MB cap is auto-chunked and re-merged with offset
    timestamps; long videos no longer fail outright.
  - .env is read ONLY from `~/.config/scenelens/.env` and process env. The
    cwd .env fallback is intentionally removed — it would silently pick up
    keys from any random project directory.

Pure stdlib. No pip install groq / openai needed.
"""
from __future__ import annotations

import io
import json
import mimetypes
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import uuid
from pathlib import Path
from urllib.request import Request, urlopen


GROQ_ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3"

OPENAI_ENDPOINT = "https://api.openai.com/v1/audio/transcriptions"
OPENAI_MODEL = "whisper-1"

WHISPER_MAX_BYTES = 24 * 1024 * 1024   # 1 MB headroom under the 25 MB API cap
CHUNK_OVERLAP_SEC = 0.5                # tiny overlap to avoid losing word boundaries

CONFIG_FILE = Path.home() / ".config" / "scenelens" / ".env"


def load_api_key(preferred: str | None = None) -> tuple[str, str] | tuple[None, None]:
    """Return (backend, api_key). Prefers Groq, falls back to OpenAI.

    Sources, in order: process env, then ~/.config/scenelens/.env. We do NOT
    read .env from the current working directory — that would silently leak
    keys between projects.
    """
    def _from_env(name: str) -> str | None:
        value = os.environ.get(name)
        return value.strip() if value else None

    def _from_dotenv(path: Path, name: str) -> str | None:
        if not path.exists():
            return None
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() != name:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
                    value = value[1:-1]
                return value or None
        except OSError:
            return None
        return None

    candidates = (("GROQ_API_KEY", "groq"), ("OPENAI_API_KEY", "openai"))
    if preferred is not None:
        candidates = tuple(c for c in candidates if c[1] == preferred)

    for key_name, backend in candidates:
        value = _from_env(key_name) or _from_dotenv(CONFIG_FILE, key_name)
        if value:
            return backend, value

    return None, None


def _ffmpeg_or_die() -> None:
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed. Run scripts/setup.py.")


def extract_audio(video_path: str, out_path: Path) -> Path:
    """Mono 16 kHz 64 kbps mp3 — ~480 kB/min, fits any Whisper limit at <50 min."""
    _ffmpeg_or_die()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", video_path,
        "-vn",
        "-acodec", "libmp3lame",
        "-ar", "16000",
        "-ac", "1",
        "-b:a", "64k",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"ffmpeg audio extraction failed: {result.stderr.strip()}")
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise SystemExit("ffmpeg produced no audio — video may have no audio track")
    return out_path


def _audio_duration(audio_path: Path) -> float:
    """ffprobe duration in seconds, 0.0 on failure."""
    if shutil.which("ffprobe") is None:
        return 0.0
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return 0.0
    try:
        return float(result.stdout.strip() or 0.0)
    except ValueError:
        return 0.0


def split_audio(audio_path: Path, max_bytes: int = WHISPER_MAX_BYTES) -> list[tuple[Path, float]]:
    """Split a too-large audio file into <max_bytes chunks. Returns [(path, start_offset), ...].

    Splits at uniform time boundaries computed from the size:duration ratio.
    Re-encodes each chunk so byte size lands predictably under the cap.
    """
    size = audio_path.stat().st_size
    if size <= max_bytes:
        return [(audio_path, 0.0)]

    _ffmpeg_or_die()
    duration = _audio_duration(audio_path)
    if duration <= 0:
        raise SystemExit("Cannot split audio: ffprobe couldn't read duration")

    # Aim for chunks ~80% of the cap to leave headroom for codec overhead.
    target_bytes = int(max_bytes * 0.80)
    chunk_dur = max(60.0, duration * (target_bytes / size))

    chunks: list[tuple[Path, float]] = []
    out_dir = audio_path.parent
    stem = audio_path.stem
    start = 0.0
    idx = 0
    while start < duration:
        end = min(duration, start + chunk_dur)
        chunk_path = out_dir / f"{stem}.chunk{idx:03d}.mp3"
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start:.3f}",
            "-to", f"{end:.3f}",
            "-i", str(audio_path),
            "-vn",
            "-acodec", "libmp3lame",
            "-ar", "16000",
            "-ac", "1",
            "-b:a", "64k",
            str(chunk_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise SystemExit(f"ffmpeg chunking failed: {result.stderr.strip()}")
        if chunk_path.stat().st_size > max_bytes:
            chunk_path.unlink(missing_ok=True)
            chunk_dur = chunk_dur * 0.85
            continue
        chunks.append((chunk_path, start))
        idx += 1
        start = max(start + chunk_dur - CHUNK_OVERLAP_SEC, end)

    if not chunks:
        raise SystemExit("Audio chunking produced no usable chunks")
    return chunks


def _build_multipart(fields: dict[str, str], file_path: Path, file_bytes: bytes) -> tuple[bytes, str]:
    """Assemble a multipart/form-data body. file_bytes is pre-read to avoid
    hitting disk on every retry."""
    boundary = f"----ScenelensBoundary{uuid.uuid4().hex}"
    eol = b"\r\n"
    buf = io.BytesIO()

    for name, value in fields.items():
        buf.write(f"--{boundary}".encode()); buf.write(eol)
        buf.write(f'Content-Disposition: form-data; name="{name}"'.encode()); buf.write(eol)
        buf.write(eol)
        buf.write(str(value).encode()); buf.write(eol)

    mimetype = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    buf.write(f"--{boundary}".encode()); buf.write(eol)
    buf.write(
        f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"'.encode()
    )
    buf.write(eol)
    buf.write(f"Content-Type: {mimetype}".encode()); buf.write(eol)
    buf.write(eol)
    buf.write(file_bytes)
    buf.write(eol)
    buf.write(f"--{boundary}--".encode()); buf.write(eol)

    return buf.getvalue(), boundary


MAX_ATTEMPTS = 4
MAX_429_RETRIES = 2
RETRY_BASE_DELAY = 2.0


def _post_whisper(endpoint: str, api_key: str, model: str, audio_path: Path) -> dict:
    fields = {
        "model": model,
        "response_format": "verbose_json",
        "temperature": "0",
    }
    file_bytes = audio_path.read_bytes()  # read once, reuse on retries
    body, boundary = _build_multipart(fields, audio_path, file_bytes)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        # Default Python-urllib UA trips Cloudflare WAF on Groq before auth runs.
        "User-Agent": "scenelens/0.1 (+claude; python-urllib)",
    }

    context = ssl.create_default_context()
    rate_limit_hits = 0
    last_exc: Exception | None = None
    last_detail = ""

    for attempt in range(MAX_ATTEMPTS):
        request = Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=300, context=context) as response:
                payload = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = _read_error_body(exc)
            last_exc, last_detail = exc, detail

            if 400 <= exc.code < 500 and exc.code != 429:
                raise SystemExit(f"Whisper request failed: {exc}{detail}")

            if exc.code == 429:
                rate_limit_hits += 1
                if rate_limit_hits >= MAX_429_RETRIES:
                    raise SystemExit(f"Whisper request failed: {exc}{detail}")
                delay = _retry_after(exc) or RETRY_BASE_DELAY * (2 ** attempt) + 1
            else:
                delay = RETRY_BASE_DELAY * (2 ** attempt)

            if attempt < MAX_ATTEMPTS - 1:
                print(
                    f"[scenelens] whisper HTTP {exc.code} — retrying in {delay:.1f}s "
                    f"(attempt {attempt + 2}/{MAX_ATTEMPTS})",
                    file=sys.stderr,
                )
                time.sleep(delay)
            continue
        except (urllib.error.URLError, TimeoutError, ConnectionResetError, OSError) as exc:
            last_exc, last_detail = exc, ""
            if attempt < MAX_ATTEMPTS - 1:
                delay = RETRY_BASE_DELAY * (attempt + 1)
                print(
                    f"[scenelens] whisper network error ({type(exc).__name__}: {exc}) — "
                    f"retrying in {delay:.1f}s (attempt {attempt + 2}/{MAX_ATTEMPTS})",
                    file=sys.stderr,
                )
                time.sleep(delay)
            continue

        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Whisper returned non-JSON response: {exc}: {payload[:200]}")

    raise SystemExit(
        f"Whisper request failed after {MAX_ATTEMPTS} attempts: {last_exc}{last_detail}"
    )


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read()
    except Exception:
        return ""
    if not body:
        return ""
    try:
        return f" — {body.decode('utf-8', errors='replace')[:400]}"
    except Exception:
        return ""


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    header = exc.headers.get("Retry-After") if getattr(exc, "headers", None) else None
    if not header:
        return None
    try:
        return float(header)
    except ValueError:
        return None


def _segments_from_response(data: dict, time_offset: float = 0.0) -> list[dict]:
    out: list[dict] = []
    for seg in data.get("segments") or []:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        out.append({
            "start": round(float(seg.get("start") or 0.0) + time_offset, 2),
            "end": round(float(seg.get("end") or 0.0) + time_offset, 2),
            "text": text,
        })

    if not out:
        full = (data.get("text") or "").strip()
        if full:
            out.append({"start": round(time_offset, 2), "end": round(time_offset, 2), "text": full})

    return out


def transcribe_video(
    video_path: str,
    audio_out: Path,
    backend: str | None = None,
    api_key: str | None = None,
) -> tuple[list[dict], str]:
    """Extract audio → split if oversize → upload each chunk → merge segments.

    Returns (segments, backend_used).
    """
    if backend is None or api_key is None:
        detected_backend, detected_key = load_api_key()
        backend = backend or detected_backend
        api_key = api_key or detected_key

    if not backend or not api_key:
        setup_py = Path(__file__).resolve().parent / "setup.py"
        raise SystemExit(
            "No Whisper API key available. Set GROQ_API_KEY (preferred) or OPENAI_API_KEY "
            "in the environment or in ~/.config/scenelens/.env. "
            f"Run `python3 {setup_py}` to configure."
        )

    print(f"[scenelens] extracting audio for Whisper ({backend})…", file=sys.stderr)
    audio_path = extract_audio(video_path, audio_out)
    size_kb = audio_path.stat().st_size / 1024
    print(f"[scenelens] audio: {size_kb:.0f} kB", file=sys.stderr)

    chunks = split_audio(audio_path)
    if len(chunks) > 1:
        print(f"[scenelens] audio exceeds 24 MB cap — split into {len(chunks)} chunks", file=sys.stderr)

    endpoint, model = (GROQ_ENDPOINT, GROQ_MODEL) if backend == "groq" else (OPENAI_ENDPOINT, OPENAI_MODEL)
    if backend not in ("groq", "openai"):
        raise SystemExit(f"Unknown whisper backend: {backend}")

    all_segments: list[dict] = []
    for i, (chunk_path, offset) in enumerate(chunks):
        if len(chunks) > 1:
            print(f"[scenelens] uploading chunk {i+1}/{len(chunks)} (offset {offset:.1f}s)…", file=sys.stderr)
        else:
            print(f"[scenelens] uploading audio to {backend} Whisper…", file=sys.stderr)
        response = _post_whisper(endpoint, api_key, model, chunk_path)
        all_segments.extend(_segments_from_response(response, time_offset=offset))

    if not all_segments:
        raise SystemExit("Whisper returned no transcript segments")

    all_segments.sort(key=lambda s: s["start"])
    print(f"[scenelens] transcribed {len(all_segments)} segments via {backend}", file=sys.stderr)
    return all_segments, backend


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: whisper.py <video-path> [<audio-out.mp3>] [--backend groq|openai]", file=sys.stderr)
        raise SystemExit(2)

    video = sys.argv[1]
    audio_out = Path(sys.argv[2]) if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else Path("audio.mp3")
    backend_override = None
    if "--backend" in sys.argv:
        backend_override = sys.argv[sys.argv.index("--backend") + 1]

    segments, backend = transcribe_video(video, audio_out, backend=backend_override)
    print(json.dumps({"backend": backend, "segments": segments}, indent=2))
