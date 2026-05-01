#!/usr/bin/env python3
"""/vidsense entry point.

Pipeline:
  1. download / resolve the source
  2. ffprobe metadata
  3. extract frames — scene-aware by default, fixed-fps fallback
  4. OCR each frame (optional, skipped if tesseract missing)
  5. parse captions OR call Whisper (auto-chunked if oversize)
  6. print a markdown report

Claude then `Read`s every frame path and answers grounded in frames + OCR
text + transcript.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path


# Windows consoles default to cp1252 and crash on em-dashes, arrows, ellipses
# in argparse help and the markdown report. Force UTF-8 with `replace` fallback
# so the script never aborts on a glyph the host encoding can't render.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

from download import download, is_url  # noqa: E402
from frames import (  # noqa: E402
    DEFAULT_SCENE_THRESHOLD,
    extract,
    format_time,
    get_metadata,
    parse_time,
)
from ocr import is_available as ocr_available, ocr_frames, summarize_ocr  # noqa: E402
from transcribe import filter_range, format_transcript, parse_vtt  # noqa: E402
from whisper import load_api_key, transcribe_video  # noqa: E402


def _validate_out_dir(path_arg: str | None) -> Path:
    """Resolve --out-dir under user control. Refuses obvious system paths."""
    if not path_arg:
        return Path(tempfile.mkdtemp(prefix="vidsense-"))

    p = Path(path_arg).expanduser().resolve()
    forbidden_prefixes = [Path("/etc"), Path("/bin"), Path("/sbin"), Path("/usr/bin"), Path("/usr/sbin"), Path("/boot")]
    for bad in forbidden_prefixes:
        try:
            p.relative_to(bad)
            raise SystemExit(f"--out-dir cannot be inside {bad}")
        except ValueError:
            continue
    p.mkdir(parents=True, exist_ok=True)
    return p


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="vidsense",
        description="Watch a video — scene-aware frames + OCR + transcript, surfaced for Claude.",
    )
    ap.add_argument("source", help="Video URL or local file path")
    ap.add_argument("--max-frames", type=int, default=80, help="Cap on frame count (default 80, hard max 100)")
    ap.add_argument("--resolution", type=int, default=512, help="Frame width in pixels (default 512)")
    ap.add_argument("--fps", type=float, default=None, help="Override auto-fps (only applies in fixed-fps mode)")
    ap.add_argument("--mode", choices=["auto", "scene", "fixed"], default="auto",
                    help="Frame selection: auto (scene→fixed fallback), scene-only, or fixed-fps")
    ap.add_argument("--scene-threshold", type=float, default=DEFAULT_SCENE_THRESHOLD,
                    help=f"Scene-change sensitivity 0-1 (default {DEFAULT_SCENE_THRESHOLD})")
    ap.add_argument("--start", type=str, default=None, help="Range start (SS, MM:SS, or HH:MM:SS)")
    ap.add_argument("--end", type=str, default=None, help="Range end (SS, MM:SS, or HH:MM:SS)")
    ap.add_argument("--out-dir", type=str, default=None, help="Working directory (default: tmp)")
    ap.add_argument("--no-ocr", action="store_true", help="Skip the OCR pass on frames")
    ap.add_argument("--ocr-lang", type=str, default="eng", help="Tesseract language code (default eng)")
    ap.add_argument("--no-whisper", action="store_true", help="Disable Whisper fallback")
    ap.add_argument("--whisper", choices=["groq", "openai"], default=None,
                    help="Force a specific Whisper backend")
    ap.add_argument("--sub-langs", type=str, default="en,en-US,en-GB,en-orig",
                    help="Caption languages to fetch in priority order")
    args = ap.parse_args()

    max_frames = min(args.max_frames, 100)
    work = _validate_out_dir(args.out_dir)
    print(f"[vidsense] working dir: {work}", file=sys.stderr)

    print(
        "[vidsense] downloading via yt-dlp…" if is_url(args.source) else "[vidsense] using local file…",
        file=sys.stderr,
    )
    dl = download(args.source, work / "download", sub_langs=args.sub_langs)
    video_path = dl["video_path"]

    meta = get_metadata(video_path)
    full_duration = meta["duration_seconds"]

    start_sec = parse_time(args.start)
    end_sec = parse_time(args.end)

    if start_sec is not None and start_sec < 0:
        raise SystemExit("--start must be non-negative")
    if end_sec is not None and start_sec is not None and end_sec <= start_sec:
        raise SystemExit("--end must be greater than --start")
    if full_duration > 0 and start_sec is not None and start_sec >= full_duration:
        raise SystemExit(f"--start {start_sec:.1f}s is past end of video ({full_duration:.1f}s)")

    effective_start = start_sec if start_sec is not None else 0.0
    effective_end = end_sec if end_sec is not None else full_duration
    effective_duration = max(0.0, effective_end - effective_start)
    focused = start_sec is not None or end_sec is not None

    scope = (
        f"{format_time(effective_start)}-{format_time(effective_end)} ({effective_duration:.1f}s)"
        if focused else f"full {effective_duration:.1f}s"
    )
    print(f"[vidsense] extracting frames ({args.mode}) over {scope}…", file=sys.stderr)

    frames, frame_info = extract(
        video_path,
        work / "frames",
        duration_seconds=effective_duration,
        resolution=args.resolution,
        max_frames=max_frames,
        start_seconds=start_sec,
        end_seconds=end_sec,
        mode=args.mode,
        fps_override=args.fps,
        scene_threshold=args.scene_threshold,
        focused=focused,
    )

    if not frames:
        raise SystemExit("Frame extraction produced 0 frames — check the video and your --mode flag")

    ocr_summary = None
    if not args.no_ocr:
        if ocr_available():
            print(f"[vidsense] running OCR on {len(frames)} frames…", file=sys.stderr)
            ocr_frames(frames, lang=args.ocr_lang)
            ocr_summary = summarize_ocr(frames)
            print(
                f"[vidsense] OCR found text on {ocr_summary['frames_with_text']}/{ocr_summary['frames_total']} frames "
                f"({ocr_summary['ocr_chars']} chars)",
                file=sys.stderr,
            )
        else:
            print("[vidsense] tesseract not installed — skipping OCR (run setup.py to enable)", file=sys.stderr)

    transcript_segments: list[dict] = []
    transcript_text: str | None = None
    transcript_source: str | None = None
    if dl.get("subtitle_path"):
        try:
            all_segments = parse_vtt(dl["subtitle_path"])
            transcript_segments = filter_range(all_segments, start_sec, end_sec) if focused else all_segments
            transcript_text = format_transcript(transcript_segments)
            transcript_source = "captions"
        except Exception as exc:
            print(f"[vidsense] subtitle parse failed: {exc}", file=sys.stderr)

    if not transcript_segments and not args.no_whisper:
        backend, api_key = load_api_key(args.whisper)
        if backend and api_key:
            try:
                all_segments, used_backend = transcribe_video(
                    video_path,
                    work / "audio.mp3",
                    backend=backend,
                    api_key=api_key,
                )
                transcript_segments = filter_range(all_segments, start_sec, end_sec) if focused else all_segments
                transcript_text = format_transcript(transcript_segments)
                transcript_source = f"whisper ({used_backend})"
            except SystemExit as exc:
                print(f"[vidsense] whisper fallback failed: {exc}", file=sys.stderr)
        else:
            hint = (
                f"--whisper {args.whisper} was set but the matching API key is missing"
                if args.whisper else
                "no subtitles and no Whisper API key found"
            )
            setup_py = SCRIPT_DIR / "setup.py"
            print(
                f"[vidsense] {hint} — run `python3 {setup_py}` to enable Whisper",
                file=sys.stderr,
            )

    info = dl.get("info") or {}
    chapters = info.get("chapters") or []

    print()
    print("# vidsense: video report")
    print()
    print(f"- **Source:** {args.source}")
    if info.get("title"):
        print(f"- **Title:** {info['title']}")
    if info.get("uploader"):
        print(f"- **Uploader:** {info['uploader']}")
    print(f"- **Duration:** {format_time(full_duration)} ({full_duration:.1f}s)")
    if focused:
        print(
            f"- **Focus range:** {format_time(effective_start)} → {format_time(effective_end)} "
            f"({effective_duration:.1f}s)"
        )
    if meta.get("width") and meta.get("height"):
        print(f"- **Resolution:** {meta['width']}x{meta['height']} ({meta.get('codec') or 'unknown codec'})")

    mode_used = frame_info.get("mode_used")
    if mode_used == "scene":
        print(
            f"- **Frames:** {len(frames)} via scene detection (threshold "
            f"{frame_info['scene_threshold']:.2f}, max {max_frames})"
        )
    else:
        fps = frame_info.get("fps") or 0.0
        reason = frame_info.get("fallback_reason")
        suffix = f" — fallback: {reason}" if reason else ""
        print(f"- **Frames:** {len(frames)} @ {fps:.3f} fps fixed (max {max_frames}){suffix}")
    print(f"- **Frame size:** {args.resolution}px wide")

    if ocr_summary is not None:
        print(
            f"- **OCR:** {ocr_summary['frames_with_text']}/{ocr_summary['frames_total']} frames have text "
            f"({ocr_summary['ocr_chars']} chars total)"
        )
    elif args.no_ocr:
        print("- **OCR:** disabled (`--no-ocr`)")
    else:
        print("- **OCR:** unavailable (tesseract not installed)")

    if transcript_segments:
        in_range = " in range" if focused else ""
        print(
            f"- **Transcript:** {len(transcript_segments)} segments{in_range} "
            f"(via {transcript_source or 'captions'})"
        )
    else:
        print("- **Transcript:** none available")

    if chapters and not focused:
        print(f"- **Chapters:** {len(chapters)} (see Chapters section below)")

    if not focused and full_duration > 600:
        mins = int(full_duration // 60)
        print()
        print(
            f"> **Note:** This is a {mins}-minute video. Even with scene-aware extraction, "
            "long-form content benefits from focused passes — re-run with "
            "`--start HH:MM:SS --end HH:MM:SS` to zoom into a specific section."
        )

    if chapters and not focused:
        print()
        print("## Chapters")
        print()
        for ch in chapters:
            try:
                start = float(ch.get("start_time") or 0)
                title = ch.get("title") or "(untitled)"
                print(f"- `{format_time(start)}` — {title}")
            except (TypeError, ValueError):
                continue

    print()
    print("## Frames")
    print()
    print(f"Frames live at: `{work / 'frames'}`")
    print()
    print(
        "**Read each frame path below with the Read tool to view the image.** "
        "Frames are in chronological order; `t=MM:SS` is the absolute timestamp in the source video. "
        "Where present, `OCR` shows on-screen text extracted from that frame — use it instead of "
        "burning vision tokens to read static text."
    )
    print()
    for frame in frames:
        line = f"- `{frame['path']}` (t={format_time(frame['timestamp_seconds'])})"
        ocr_text = frame.get("ocr")
        if ocr_text:
            preview = ocr_text.replace("\n", " ⏎ ")
            if len(preview) > 280:
                preview = preview[:277] + "…"
            line += f"\n  - **OCR:** {preview}"
        print(line)

    print()
    print("## Transcript")
    print()
    if transcript_text:
        label = transcript_source or "captions"
        if focused:
            print(f"_Source: {label}. Filtered to {format_time(effective_start)} → {format_time(effective_end)}:_")
        else:
            print(f"_Source: {label}._")
        print()
        print("```")
        print(transcript_text)
        print("```")
    elif focused and dl.get("subtitle_path"):
        print(f"_No transcript lines fell inside {format_time(effective_start)} → {format_time(effective_end)}._")
    else:
        setup_py = SCRIPT_DIR / "setup.py"
        print(
            "_No transcript available — proceed with frames only. "
            "Captions were missing and the Whisper fallback was unavailable "
            "(no API key set, or `--no-whisper` was used). "
            f"Run `python3 {setup_py}` to enable Whisper, then re-run._"
        )

    print()
    print("---")
    print(f"_Work dir: `{work}` — delete when done._")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
