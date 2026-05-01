#!/usr/bin/env python3
"""Probe video metadata and extract frames.

Two extraction strategies:
  1. Scene-aware (default): pick frames where the visual content actually
     changes. Massively better signal for talks, demos, ads, anything with
     cuts. Falls back to fixed-fps when too few scene changes are detected
     (e.g. a static talking-head recording).
  2. Fixed-fps (legacy / --fixed-fps): the original auto-budget approach.
     Useful for content with no hard cuts where you want time-uniform sampling.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


MAX_FPS = 2.0
DEFAULT_SCENE_THRESHOLD = 0.30
MIN_SCENE_FRAMES = 8  # below this, scene mode is too sparse — fall back

PTS_RE = re.compile(r"pts_time:(\d+(?:\.\d+)?)")


def _clamp_fps(fps: float, duration_seconds: float, max_frames: int) -> tuple[float, int]:
    fps = min(fps, MAX_FPS)
    target = min(max_frames, max(1, int(round(fps * duration_seconds))))
    return fps, target


def parse_time(value: str | float | int | None) -> float | None:
    """Parse SS, MM:SS, or HH:MM:SS (with optional .ms) into seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    parts = s.split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        pass
    raise SystemExit(f"Cannot parse time value: {value!r} (expected SS, MM:SS, or HH:MM:SS)")


def format_time(seconds: float) -> str:
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, sec = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def get_metadata(video_path: str) -> dict:
    if shutil.which("ffprobe") is None:
        raise SystemExit("ffprobe is not installed. Install ffmpeg first.")

    result = subprocess.run(
        [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            video_path,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"ffprobe failed: {result.stderr.strip()}")

    data = json.loads(result.stdout or "{}")
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = float(fmt.get("duration") or video_stream.get("duration") or 0)
    return {
        "duration_seconds": duration,
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
        "codec": video_stream.get("codec_name"),
        "size_bytes": int(fmt.get("size") or 0),
        "has_audio": audio_stream is not None,
    }


def auto_fps(duration_seconds: float, max_frames: int = 100) -> tuple[float, int]:
    """Pick fps that targets a sensible frame budget for full-video scans."""
    if duration_seconds <= 0:
        return 1.0, 1

    if duration_seconds <= 30:
        target = min(max_frames, max(12, int(round(duration_seconds))))
    elif duration_seconds <= 60:
        target = min(max_frames, 40)
    elif duration_seconds <= 180:
        target = min(max_frames, 60)
    elif duration_seconds <= 600:
        target = min(max_frames, 80)
    else:
        target = max_frames

    return _clamp_fps(target / duration_seconds, duration_seconds, max_frames)


def auto_fps_focus(duration_seconds: float, max_frames: int = 100) -> tuple[float, int]:
    """Denser budget for user-specified ranges — they're zooming in for detail."""
    if duration_seconds <= 0:
        return min(MAX_FPS, 2.0), 2

    if duration_seconds <= 5:
        target = min(max_frames, max(10, int(round(duration_seconds * 6))))
    elif duration_seconds <= 15:
        target = min(max_frames, max(30, int(round(duration_seconds * 4))))
    elif duration_seconds <= 30:
        target = min(max_frames, 60)
    elif duration_seconds <= 60:
        target = min(max_frames, 80)
    else:
        target = max_frames

    return _clamp_fps(target / duration_seconds, duration_seconds, max_frames)


def extract_fixed_fps(
    video_path: str,
    out_dir: Path,
    fps: float,
    resolution: int = 512,
    max_frames: int = 100,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
) -> list[dict]:
    """Original strategy: extract one frame every 1/fps seconds, capped."""
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed.")

    out_dir.mkdir(parents=True, exist_ok=True)
    for existing in out_dir.glob("frame_*.jpg"):
        existing.unlink()

    output_pattern = str(out_dir / "frame_%04d.jpg")
    cmd: list[str] = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
    ]

    if start_seconds is not None:
        cmd += ["-ss", f"{start_seconds:.3f}"]
    if end_seconds is not None:
        cmd += ["-to", f"{end_seconds:.3f}"]

    cmd += [
        "-i", video_path,
        "-vf", f"fps={fps},scale={resolution}:-2",
        "-frames:v", str(max_frames),
        "-q:v", "4",
        output_pattern,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"ffmpeg frame extraction failed: {result.stderr.strip()}")

    offset = start_seconds or 0.0
    frames = sorted(out_dir.glob("frame_*.jpg"))
    return [
        {
            "index": i,
            "timestamp_seconds": round(offset + (i / fps if fps > 0 else 0.0), 2),
            "path": str(p),
            "source": "fixed_fps",
        }
        for i, p in enumerate(frames)
    ]


def extract_scene_aware(
    video_path: str,
    out_dir: Path,
    threshold: float = DEFAULT_SCENE_THRESHOLD,
    resolution: int = 512,
    max_frames: int = 100,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
) -> list[dict]:
    """Pick frames at scene changes. Returns [] if fewer than MIN_SCENE_FRAMES detected."""
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed.")

    out_dir.mkdir(parents=True, exist_ok=True)
    for existing in out_dir.glob("frame_*.jpg"):
        existing.unlink()

    output_pattern = str(out_dir / "frame_%04d.jpg")
    cmd: list[str] = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "info",
        "-y",
    ]

    if start_seconds is not None:
        cmd += ["-ss", f"{start_seconds:.3f}"]
    if end_seconds is not None:
        cmd += ["-to", f"{end_seconds:.3f}"]

    # Important: -vsync vfr drops duplicates that the select filter would
    # otherwise pad. Without it, ffmpeg fills gaps with copies of the last frame.
    cmd += [
        "-i", video_path,
        "-vf", f"select='gt(scene,{threshold})',scale={resolution}:-2,showinfo",
        "-vsync", "vfr",
        "-frames:v", str(max_frames),
        "-q:v", "4",
        output_pattern,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    frames = sorted(out_dir.glob("frame_*.jpg"))

    # When the select filter emits zero frames (no scene cuts above threshold),
    # ffmpeg's encoder init can fail with non-zero exit even though the pipeline
    # was logically successful. Treat "ffmpeg failed AND no frames written" as
    # the legitimate "no scenes detected" case so auto-mode can fall back.
    if result.returncode != 0:
        if not frames:
            return []
        # Some frames did land — likely a late warning, not a fatal error.
        # Continue and use what we got.

    # showinfo logs one line per emitted frame. Parse pts_time to recover
    # absolute timestamps — these are real PTS, not computed from fps.
    timestamps: list[float] = []
    for line in result.stderr.splitlines():
        if "Parsed_showinfo" not in line:
            continue
        match = PTS_RE.search(line)
        if match:
            timestamps.append(float(match.group(1)))

    if len(frames) < MIN_SCENE_FRAMES:
        for f in frames:
            f.unlink()
        return []

    offset = start_seconds or 0.0
    out: list[dict] = []
    for i, p in enumerate(frames):
        ts = timestamps[i] if i < len(timestamps) else (i * 1.0)
        out.append({
            "index": i,
            "timestamp_seconds": round(offset + ts, 2),
            "path": str(p),
            "source": "scene",
        })
    return out


def extract(
    video_path: str,
    out_dir: Path,
    duration_seconds: float,
    resolution: int = 512,
    max_frames: int = 100,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    mode: str = "auto",
    fps_override: float | None = None,
    scene_threshold: float = DEFAULT_SCENE_THRESHOLD,
    focused: bool = False,
) -> tuple[list[dict], dict]:
    """Top-level frame extraction. Returns (frames, info_dict).

    mode:
      "auto"   — try scene-aware, fall back to fixed-fps if too sparse
      "scene"  — scene-aware only
      "fixed"  — fixed-fps only (original behavior)
    """
    info: dict = {"mode_requested": mode, "scene_threshold": scene_threshold}

    if mode in ("auto", "scene"):
        frames = extract_scene_aware(
            video_path, out_dir,
            threshold=scene_threshold,
            resolution=resolution,
            max_frames=max_frames,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
        )
        if frames:
            info["mode_used"] = "scene"
            info["fps"] = None
            info["scene_frames"] = len(frames)
            return frames, info
        if mode == "scene":
            info["mode_used"] = "scene"
            info["scene_frames"] = 0
            info["fallback_reason"] = "no scenes above threshold; --mode scene returned empty"
            return [], info
        info["fallback_reason"] = f"<{MIN_SCENE_FRAMES} scene changes detected"

    if focused:
        fps, _target = auto_fps_focus(duration_seconds, max_frames=max_frames)
    else:
        fps, _target = auto_fps(duration_seconds, max_frames=max_frames)
    if fps_override is not None:
        fps = min(fps_override, MAX_FPS)

    frames = extract_fixed_fps(
        video_path, out_dir,
        fps=fps,
        resolution=resolution,
        max_frames=max_frames,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
    )
    info["mode_used"] = "fixed_fps"
    info["fps"] = fps
    return frames, info


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: frames.py <video-path> <out-dir> [--mode auto|scene|fixed]", file=sys.stderr)
        raise SystemExit(2)

    video = sys.argv[1]
    out = Path(sys.argv[2])
    mode = "auto"
    if "--mode" in sys.argv:
        mode = sys.argv[sys.argv.index("--mode") + 1]

    meta = get_metadata(video)
    frames, info = extract(
        video, out,
        duration_seconds=meta["duration_seconds"],
        mode=mode,
    )
    print(json.dumps({"meta": meta, "info": info, "frames": frames}, indent=2))
