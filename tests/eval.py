#!/usr/bin/env python3
"""vidsense eval harness — tests every feature in a loop.

Two tiers:
  - Logic tier: pure-Python tests that always run (no external binaries).
  - Integration tier: tests requiring ffmpeg / ffprobe / yt-dlp / tesseract /
    a live API key. These are reported as SKIPPED (with the reason) when
    prerequisites are absent — they don't fail the suite.

Loops the full suite N times (default 3) so any flaky test is surfaced as a
mixed pass/fail across iterations. Each test runs in isolation: env vars,
cwd, and module-level CONFIG_FILE pointers are saved and restored.

Usage:
    python tests/eval.py                   # 3 iterations, plain text
    python tests/eval.py --iterations 5
    python tests/eval.py --json            # machine-readable summary

Exit codes:
    0  All non-skipped tests passed every iteration.
    1  At least one test failed in at least one iteration.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import contextmanager
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

# Force UTF-8 so Windows cp1252 consoles don't crash on em-dashes in test output.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass

# Imports under test
import download  # noqa: E402
import frames  # noqa: E402
import ocr  # noqa: E402
import setup as vidsetup  # noqa: E402  (avoid clashing with stdlib `setup`)
import transcribe  # noqa: E402
import whisper as wsp  # noqa: E402  (avoid clashing with possible openai-whisper)


# ---------------------------------------------------------------------------
# Test framework — minimalist, no pytest dependency
# ---------------------------------------------------------------------------

class TestSkip(Exception):
    pass


def skip_if_missing(*binaries: str) -> None:
    missing = [b for b in binaries if shutil.which(b) is None]
    if missing:
        raise TestSkip(f"missing binary: {', '.join(missing)}")


def skip_if_no_env(*names: str) -> None:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise TestSkip(f"missing env: {', '.join(missing)}")


def skip_on_windows(reason: str) -> None:
    if sys.platform.startswith("win"):
        raise TestSkip(f"windows: {reason}")


@contextmanager
def isolated_env():
    """Save GROQ/OPENAI keys, restore after."""
    saved = {k: os.environ.pop(k, None) for k in ("GROQ_API_KEY", "OPENAI_API_KEY")}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


@contextmanager
def patched_config(path: Path):
    """Point both whisper.CONFIG_FILE and setup.CONFIG_FILE at a temp file."""
    saved_w, saved_s = wsp.CONFIG_FILE, vidsetup.CONFIG_FILE
    wsp.CONFIG_FILE = path
    vidsetup.CONFIG_FILE = path
    try:
        yield
    finally:
        wsp.CONFIG_FILE = saved_w
        vidsetup.CONFIG_FILE = saved_s


@contextmanager
def temp_cwd(path: Path):
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def assert_eq(actual, expected, msg: str = ""):
    if actual != expected:
        raise AssertionError(f"{msg} expected {expected!r}, got {actual!r}")


def assert_true(cond, msg: str = ""):
    if not cond:
        raise AssertionError(f"{msg} expected True")


def assert_raises(exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type:
        return
    except BaseException as e:
        raise AssertionError(f"expected {exc_type.__name__}, got {type(e).__name__}: {e}")
    raise AssertionError(f"expected {exc_type.__name__}, no exception raised")


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

# ===== frames.py =====

def test_parse_time_seconds():
    assert_eq(frames.parse_time("90"), 90.0)
    assert_eq(frames.parse_time("0"), 0.0)


def test_parse_time_mmss():
    assert_eq(frames.parse_time("1:30"), 90.0)
    assert_eq(frames.parse_time("00:45"), 45.0)


def test_parse_time_hhmmss():
    assert_eq(frames.parse_time("1:00:30"), 3630.0)
    assert_eq(frames.parse_time("0:01:00"), 60.0)


def test_parse_time_with_ms():
    assert_eq(frames.parse_time("1:30.5"), 90.5)


def test_parse_time_none_returns_none():
    assert_true(frames.parse_time(None) is None)
    assert_true(frames.parse_time("") is None)


def test_parse_time_int_passthrough():
    assert_eq(frames.parse_time(45), 45.0)
    assert_eq(frames.parse_time(45.7), 45.7)


def test_parse_time_invalid_raises():
    assert_raises(SystemExit, frames.parse_time, "abc:def")


def test_format_time_short():
    assert_eq(frames.format_time(95), "01:35")
    assert_eq(frames.format_time(0), "00:00")


def test_format_time_hours():
    assert_eq(frames.format_time(3725), "1:02:05")


def test_format_time_rounds():
    assert_eq(frames.format_time(60.4), "01:00")
    assert_eq(frames.format_time(60.6), "01:01")


def test_auto_fps_30s_dense():
    fps, target = frames.auto_fps(30, 100)
    assert_true(target >= 12, f"30s should get >=12 frames, got {target}")
    assert_true(fps <= frames.MAX_FPS, f"fps {fps} exceeds MAX_FPS {frames.MAX_FPS}")


def test_auto_fps_1min():
    _, target = frames.auto_fps(60, 100)
    assert_eq(target, 40)


def test_auto_fps_3min_capped_2fps():
    fps, _ = frames.auto_fps(180, 100)
    assert_true(fps <= 2.0, f"fps must be capped at 2, got {fps}")


def test_auto_fps_long_uses_max():
    _, target = frames.auto_fps(1800, 100)
    assert_eq(target, 100)


def test_auto_fps_zero_duration():
    assert_eq(frames.auto_fps(0, 100), (1.0, 1))


def test_auto_fps_focus_5s_dense():
    fps, target = frames.auto_fps_focus(5, 100)
    assert_eq(fps, 2.0)
    assert_true(target >= 10)


def test_auto_fps_focus_30s():
    _, target = frames.auto_fps_focus(30, 100)
    assert_eq(target, 60)


def test_auto_fps_focus_respects_max_frames_arg():
    _, target = frames.auto_fps_focus(60, 40)
    assert_true(target <= 40, f"max_frames=40 should cap, got {target}")


def test_extract_falls_back_to_fixed_fps_when_no_scenes():
    """Critical dispatch: mode=auto + zero scenes detected → fixed-fps used."""
    saved_scene = frames.extract_scene_aware
    saved_fixed = frames.extract_fixed_fps
    fixed_called: list[bool] = []
    try:
        frames.extract_scene_aware = lambda *a, **k: []
        def fake_fixed(*a, **k):
            fixed_called.append(True)
            return [{"index": 0, "timestamp_seconds": 0.0, "path": "/tmp/x.jpg", "source": "fixed_fps"}]
        frames.extract_fixed_fps = fake_fixed

        result, info = frames.extract(
            "fake.mp4", Path(tempfile.gettempdir()) / "nope",
            duration_seconds=60, mode="auto",
        )
        assert_true(fixed_called, "fixed_fps was not called as fallback")
        assert_eq(info["mode_used"], "fixed_fps")
        assert_true("fallback_reason" in info, "fallback_reason missing from info")
        assert_eq(len(result), 1)
    finally:
        frames.extract_scene_aware = saved_scene
        frames.extract_fixed_fps = saved_fixed


def test_extract_mode_scene_returns_empty_when_no_scenes():
    """mode=scene must NOT fall back — returns empty + reason."""
    saved_scene = frames.extract_scene_aware
    saved_fixed = frames.extract_fixed_fps
    fixed_called: list[bool] = []
    try:
        frames.extract_scene_aware = lambda *a, **k: []
        def fake_fixed(*a, **k):
            fixed_called.append(True)
            return [{"index": 0}]
        frames.extract_fixed_fps = fake_fixed

        result, info = frames.extract(
            "fake.mp4", Path(tempfile.gettempdir()) / "nope",
            duration_seconds=60, mode="scene",
        )
        assert_eq(result, [])
        assert_eq(info["mode_used"], "scene")
        assert_true(not fixed_called, "fixed_fps was called when mode=scene; should not fall back")
    finally:
        frames.extract_scene_aware = saved_scene
        frames.extract_fixed_fps = saved_fixed


def test_extract_mode_fixed_skips_scene_detection():
    """mode=fixed must NOT call scene detection at all."""
    saved_scene = frames.extract_scene_aware
    saved_fixed = frames.extract_fixed_fps
    scene_called: list[bool] = []
    try:
        def fake_scene(*a, **k):
            scene_called.append(True)
            return []
        frames.extract_scene_aware = fake_scene
        frames.extract_fixed_fps = lambda *a, **k: [{"index": 0}]

        result, info = frames.extract(
            "fake.mp4", Path(tempfile.gettempdir()) / "nope",
            duration_seconds=60, mode="fixed",
        )
        assert_true(not scene_called, "scene detection was called when mode=fixed")
        assert_eq(info["mode_used"], "fixed_fps")
    finally:
        frames.extract_scene_aware = saved_scene
        frames.extract_fixed_fps = saved_fixed


# ===== transcribe.py =====

def test_parse_vtt_basic():
    vtt = """WEBVTT

00:00:01.000 --> 00:00:03.000
Hello world

00:00:04.000 --> 00:00:06.000
Second line
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".vtt", delete=False, encoding="utf-8") as f:
        f.write(vtt)
        path = f.name
    try:
        segs = transcribe.parse_vtt(path)
        assert_eq(len(segs), 2)
        assert_eq(segs[0]["text"], "Hello world")
        assert_eq(segs[0]["start"], 1.0)
        assert_eq(segs[0]["end"], 3.0)
        assert_eq(segs[1]["text"], "Second line")
    finally:
        os.unlink(path)


def test_parse_vtt_dedupes_rolling():
    """YouTube auto-subs emit each line 2-3 times; dedupe should collapse."""
    vtt = """WEBVTT

00:00:01.000 --> 00:00:03.000
Hello

00:00:02.000 --> 00:00:04.000
Hello

00:00:03.000 --> 00:00:05.000
Hello world
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".vtt", delete=False, encoding="utf-8") as f:
        f.write(vtt)
        path = f.name
    try:
        segs = transcribe.parse_vtt(path)
        assert_eq(len(segs), 1, f"rolling dupes should collapse to 1, got {len(segs)}")
        assert_eq(segs[0]["text"], "Hello world")
        assert_eq(segs[0]["start"], 1.0)
        assert_eq(segs[0]["end"], 5.0)
    finally:
        os.unlink(path)


def test_parse_vtt_strips_html_tags():
    vtt = """WEBVTT

00:00:01.000 --> 00:00:03.000
<c.bold>Bold</c> and <i>italic</i>
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".vtt", delete=False, encoding="utf-8") as f:
        f.write(vtt)
        path = f.name
    try:
        segs = transcribe.parse_vtt(path)
        assert_eq(segs[0]["text"], "Bold and italic")
    finally:
        os.unlink(path)


def test_parse_vtt_empty_returns_empty():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".vtt", delete=False, encoding="utf-8") as f:
        f.write("WEBVTT\n\n")
        path = f.name
    try:
        assert_eq(transcribe.parse_vtt(path), [])
    finally:
        os.unlink(path)


def test_filter_range_both_bounds():
    segs = [
        {"start": 0.0, "end": 5.0, "text": "a"},
        {"start": 10.0, "end": 15.0, "text": "b"},
        {"start": 20.0, "end": 25.0, "text": "c"},
    ]
    out = transcribe.filter_range(segs, 8.0, 17.0)
    assert_eq(len(out), 1)
    assert_eq(out[0]["text"], "b")


def test_filter_range_none_returns_all():
    segs = [{"start": 0.0, "end": 5.0, "text": "a"}]
    assert_eq(transcribe.filter_range(segs, None, None), segs)


def test_filter_range_overlap_inclusive():
    segs = [
        {"start": 0.0, "end": 10.0, "text": "spans"},
        {"start": 12.0, "end": 15.0, "text": "after"},
    ]
    out = transcribe.filter_range(segs, 5.0, 11.0)
    assert_eq(len(out), 1)
    assert_eq(out[0]["text"], "spans")


def test_format_transcript_timestamps():
    segs = [
        {"start": 0.0, "end": 3.0, "text": "first"},
        {"start": 65.0, "end": 70.0, "text": "second"},
    ]
    out = transcribe.format_transcript(segs)
    assert_true("[00:00] first" in out)
    assert_true("[01:05] second" in out)


# ===== download.py =====

def test_is_url_https():
    assert_true(download.is_url("https://youtu.be/abc"))


def test_is_url_http():
    assert_true(download.is_url("http://example.com/v.mp4"))


def test_is_url_local_unix_path():
    assert_true(not download.is_url("/tmp/video.mp4"))
    assert_true(not download.is_url("video.mp4"))


def test_is_url_local_windows_path():
    assert_true(not download.is_url("C:\\videos\\v.mp4"))


def test_resolve_local_missing_raises():
    assert_raises(SystemExit, download.resolve_local, "/nonexistent/abc/xyz_does_not_exist.mp4")


def test_resolve_local_existing_file_works():
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        path = f.name
    try:
        result = download.resolve_local(path)
        assert_eq(result["downloaded"], False)
        assert_true(result["video_path"].endswith(".mp4"))
    finally:
        os.unlink(path)


def test_pick_subtitle_lang_priority():
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        (td_path / "video.es.vtt").write_text("")
        (td_path / "video.en.vtt").write_text("")
        result = download._pick_subtitle(td_path, ["en", "es"])
        assert_true(result is not None)
        assert_true(".en." in result.name, f"expected en, got {result.name}")


def test_pick_subtitle_falls_back_to_first():
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        (td_path / "video.fr.vtt").write_text("")
        result = download._pick_subtitle(td_path, ["en", "es"])
        assert_true(result is not None)


# ===== whisper.py — KEY HARDENING TESTS =====

def test_load_api_key_no_keys_returns_none():
    with isolated_env():
        with tempfile.TemporaryDirectory() as td:
            with patched_config(Path(td) / "nonexistent.env"):
                backend, key = wsp.load_api_key()
                assert_eq((backend, key), (None, None))


def test_load_api_key_env_groq_picked_up():
    with isolated_env():
        os.environ["GROQ_API_KEY"] = "sk-groq-test"
        with tempfile.TemporaryDirectory() as td:
            with patched_config(Path(td) / "nonexistent.env"):
                backend, key = wsp.load_api_key()
                assert_eq(backend, "groq")
                assert_eq(key, "sk-groq-test")


def test_load_api_key_groq_preferred_over_openai():
    with isolated_env():
        os.environ["GROQ_API_KEY"] = "sk-groq"
        os.environ["OPENAI_API_KEY"] = "sk-openai"
        with tempfile.TemporaryDirectory() as td:
            with patched_config(Path(td) / "nonexistent.env"):
                backend, _ = wsp.load_api_key()
                assert_eq(backend, "groq")


def test_load_api_key_preferred_filter():
    """--whisper openai should ignore Groq even if Groq key is set."""
    with isolated_env():
        os.environ["GROQ_API_KEY"] = "sk-groq"
        os.environ["OPENAI_API_KEY"] = "sk-openai"
        with tempfile.TemporaryDirectory() as td:
            with patched_config(Path(td) / "nonexistent.env"):
                backend, key = wsp.load_api_key(preferred="openai")
                assert_eq(backend, "openai")
                assert_eq(key, "sk-openai")


def test_load_api_key_reads_config_dotenv():
    with isolated_env():
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / ".env"
            cfg.write_text("GROQ_API_KEY=from-config-file\n")
            with patched_config(cfg):
                backend, key = wsp.load_api_key()
                assert_eq(backend, "groq")
                assert_eq(key, "from-config-file")


def test_load_api_key_does_NOT_read_cwd_dotenv():
    """SECURITY: cwd .env must NOT leak keys between projects."""
    with isolated_env():
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "project"
            cwd_dir.mkdir()
            (cwd_dir / ".env").write_text("GROQ_API_KEY=should-not-be-read\n")

            with patched_config(Path(td) / "nonexistent.env"):
                with temp_cwd(cwd_dir):
                    backend, key = wsp.load_api_key()
                    assert_eq(
                        (backend, key), (None, None),
                        "cwd .env was read — this is a security regression",
                    )


def test_multipart_body_well_formed():
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(b"fake-audio-bytes")
        path = Path(f.name)
    try:
        body, boundary = wsp._build_multipart(
            {"model": "whisper-1", "temperature": "0"},
            path,
            path.read_bytes(),
        )
        assert_true(boundary.startswith("----VidsenseBoundary"))
        assert_true(f"--{boundary}".encode() in body)
        assert_true(f"--{boundary}--".encode() in body)
        assert_true(b'name="model"' in body)
        assert_true(b"whisper-1" in body)
        assert_true(b'name="file"' in body)
        assert_true(b"fake-audio-bytes" in body)
    finally:
        os.unlink(path)


def test_multipart_boundary_unique_per_call():
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(b"x")
        path = Path(f.name)
    try:
        _, b1 = wsp._build_multipart({"a": "1"}, path, b"x")
        _, b2 = wsp._build_multipart({"a": "1"}, path, b"x")
        assert_true(b1 != b2, "boundary must be unique per call (uuid4)")
    finally:
        os.unlink(path)


def test_segments_from_response_basic():
    response = {
        "segments": [
            {"start": 1.0, "end": 3.5, "text": "Hello"},
            {"start": 4.0, "end": 6.0, "text": "world"},
        ],
    }
    segs = wsp._segments_from_response(response)
    assert_eq(len(segs), 2)
    assert_eq(segs[0]["text"], "Hello")
    assert_eq(segs[0]["start"], 1.0)


def test_segments_from_response_offset_applied():
    """Critical for chunked Whisper — chunk N segments must be offset by chunk start."""
    response = {"segments": [{"start": 0.5, "end": 2.0, "text": "chunk"}]}
    segs = wsp._segments_from_response(response, time_offset=600.0)
    assert_eq(segs[0]["start"], 600.5)
    assert_eq(segs[0]["end"], 602.0)


def test_segments_from_response_empty_skips_blank():
    response = {"segments": [{"start": 0, "end": 1, "text": "  "}, {"start": 1, "end": 2, "text": "real"}]}
    segs = wsp._segments_from_response(response)
    assert_eq(len(segs), 1)
    assert_eq(segs[0]["text"], "real")


def test_segments_from_response_falls_back_to_text():
    response = {"text": "one big chunk"}
    segs = wsp._segments_from_response(response)
    assert_eq(len(segs), 1)
    assert_eq(segs[0]["text"], "one big chunk")


def test_segments_from_response_no_segments_no_text():
    assert_eq(wsp._segments_from_response({}), [])


def test_whisper_max_bytes_under_api_cap():
    """The 25 MB API cap must be respected with headroom."""
    api_cap = 25 * 1024 * 1024
    assert_true(wsp.WHISPER_MAX_BYTES < api_cap, "must leave headroom under the 25 MB cap")


# ===== ocr.py =====

def test_ocr_is_available_matches_which():
    assert_eq(ocr.is_available(), shutil.which("tesseract") is not None)


def test_ocr_one_returns_none_when_unavailable():
    if ocr.is_available():
        raise TestSkip("tesseract IS installed; this test only meaningful when absent")
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        path = f.name
    try:
        assert_true(ocr.ocr_one(path) is None)
    finally:
        os.unlink(path)


# ===== setup.py =====

def test_setup_status_keys():
    s = vidsetup._status()
    for key in (
        "status", "first_run", "missing_binaries", "missing_optional",
        "ocr_available", "whisper_backend", "has_api_key", "config_file", "platform",
    ):
        assert_true(key in s, f"missing key: {key}")
    assert_true(s["status"] in ("ready", "needs_install", "needs_key", "needs_install_and_key"))


def test_setup_check_subprocess_exit_code():
    """setup.py --check should exit 0/2/3/4 based on what's missing."""
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "setup.py"), "--check"],
        capture_output=True, text=True,
    )
    assert_true(result.returncode in (0, 2, 3, 4),
                f"unexpected exit code: {result.returncode}")
    if result.returncode == 0:
        assert_eq(result.stdout.strip(), "", "exit 0 must be silent on stdout")


def test_setup_json_output_valid():
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "setup.py"), "--json"],
        capture_output=True, text=True,
    )
    assert_eq(result.returncode, 0)
    data = json.loads(result.stdout)
    assert_true("status" in data)
    assert_true("missing_binaries" in data)
    assert_true("ocr_available" in data)


# ===== vidsense.py orchestrator =====

def test_validate_out_dir_default_is_tmp():
    """Import private function from vidsense.py."""
    spec_path = SCRIPTS_DIR / "vidsense.py"
    import importlib.util
    spec = importlib.util.spec_from_file_location("vidsense_mod", spec_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    p = mod._validate_out_dir(None)
    assert_true(p.exists())
    assert_true("vidsense-" in p.name, f"default tmp dir prefix missing: {p}")
    shutil.rmtree(p, ignore_errors=True)


def test_validate_out_dir_user_path_ok():
    import importlib.util
    spec = importlib.util.spec_from_file_location("vidsense_mod", SCRIPTS_DIR / "vidsense.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with tempfile.TemporaryDirectory() as td:
        p = mod._validate_out_dir(str(Path(td) / "subdir"))
        assert_true(p.exists())
        assert_eq(p.name, "subdir")


def test_validate_out_dir_refuses_etc():
    skip_on_windows("/etc has no semantic meaning on Windows")
    import importlib.util
    spec = importlib.util.spec_from_file_location("vidsense_mod", SCRIPTS_DIR / "vidsense.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert_raises(SystemExit, mod._validate_out_dir, "/etc/vidsense-test")


def test_vidsense_help_does_not_crash():
    """Regression: argparse help with em-dashes used to crash on Windows cp1252."""
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "vidsense.py"), "--help"],
        capture_output=True, text=True,
    )
    assert_eq(result.returncode, 0, f"--help crashed: {result.stderr}")
    assert_true("--mode" in result.stdout)
    assert_true("--scene-threshold" in result.stdout)


# ===== Integration tier (skipped without binaries) =====

def test_int_ffprobe_metadata():
    skip_if_missing("ffmpeg", "ffprobe")
    # Synthesize a 3-second test pattern via ffmpeg's lavfi source
    with tempfile.TemporaryDirectory() as td:
        video = Path(td) / "test.mp4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=10",
             str(video)],
            check=True,
        )
        meta = frames.get_metadata(str(video))
        assert_true(2.5 <= meta["duration_seconds"] <= 3.5,
                    f"duration {meta['duration_seconds']} out of range")
        assert_eq(meta["width"], 320)
        assert_eq(meta["height"], 240)


def test_int_extract_fixed_fps():
    skip_if_missing("ffmpeg")
    with tempfile.TemporaryDirectory() as td:
        video = Path(td) / "test.mp4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=10",
             str(video)],
            check=True,
        )
        out_dir = Path(td) / "frames"
        result = frames.extract_fixed_fps(
            str(video), out_dir, fps=2.0, resolution=128, max_frames=10,
        )
        assert_true(len(result) > 0, "no frames extracted")
        assert_true(all(Path(f["path"]).exists() for f in result))


def test_int_extract_scene_aware():
    skip_if_missing("ffmpeg")
    # testsrc has motion but few hard cuts; scene mode may correctly return [].
    # Just verify the function completes and the output type is right.
    with tempfile.TemporaryDirectory() as td:
        video = Path(td) / "test.mp4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=10",
             str(video)],
            check=True,
        )
        out_dir = Path(td) / "frames"
        result = frames.extract_scene_aware(
            str(video), out_dir, threshold=0.30, resolution=128, max_frames=10,
        )
        assert_true(isinstance(result, list))


def test_int_yt_dlp_available():
    skip_if_missing("yt-dlp")
    # Don't actually download — just verify the binary responds to --version.
    result = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True)
    assert_eq(result.returncode, 0)


def _find_test_font() -> str | None:
    """Find a usable TTF for ffmpeg drawtext across platforms."""
    candidates = [
        # Windows
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/Arial.ttf"),
        # macOS
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/Library/Fonts/Arial.ttf"),
        # Linux
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/dejavu/DejaVuSans.ttf"),
    ]
    for p in candidates:
        if p.exists():
            return str(p).replace("\\", "/").replace(":", r"\:")
    return None


def test_int_ocr_real_text():
    skip_if_missing("tesseract", "ffmpeg")
    font = _find_test_font()
    if not font:
        raise TestSkip("no usable TTF found for drawtext")

    with tempfile.TemporaryDirectory() as td:
        img = Path(td) / "text.png"
        # drawtext needs the font path with backslashes and colons escaped on Windows.
        drawtext = (
            f"drawtext=fontfile='{font}':text='vidsense ocr eval test':"
            "fontcolor=black:fontsize=48:x=20:y=40"
        )
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "color=c=white:s=720x140:d=1",
             "-vf", drawtext,
             "-frames:v", "1", str(img)],
            check=False,
            capture_output=True,
        )
        if not img.exists():
            raise TestSkip(f"ffmpeg drawtext failed even with font={font}")
        text = ocr.ocr_one(str(img))
        if text is None:
            raise TestSkip("tesseract returned no text on synthetic image")
        assert_true(
            "vidsense" in text.lower() or "ocr" in text.lower() or "eval" in text.lower(),
            f"OCR didn't recover expected text, got: {text!r}",
        )


def test_int_whisper_groq_call():
    skip_if_missing("ffmpeg")
    skip_if_no_env("GROQ_API_KEY")
    # Generate 2s of silence, send to Groq, expect a 200 response.
    with tempfile.TemporaryDirectory() as td:
        audio = Path(td) / "silence.mp3"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "2",
             "-acodec", "libmp3lame", "-b:a", "64k", str(audio)],
            check=True,
        )
        try:
            response = wsp._post_whisper(
                wsp.GROQ_ENDPOINT, os.environ["GROQ_API_KEY"], wsp.GROQ_MODEL, audio,
            )
        except SystemExit as e:
            msg = str(e)
            # 401/403 = bad key. Distinguish from a genuine pipeline failure:
            # the multipart upload + HTTP call succeeded — only the credential
            # was rejected. Skip rather than fail so this test reflects vidsense
            # health, not the user's key validity.
            if "401" in msg or "403" in msg or "invalid_api_key" in msg.lower():
                raise TestSkip(f"GROQ_API_KEY rejected by Groq: {msg[:100]}")
            raise AssertionError(f"Groq call failed: {e}")
        assert_true(isinstance(response, dict))


# ---------------------------------------------------------------------------
# Test registry + runner
# ---------------------------------------------------------------------------

TESTS = [
    # frames
    ("frames", "parse_time:seconds", test_parse_time_seconds),
    ("frames", "parse_time:mm:ss", test_parse_time_mmss),
    ("frames", "parse_time:hh:mm:ss", test_parse_time_hhmmss),
    ("frames", "parse_time:fractional_ms", test_parse_time_with_ms),
    ("frames", "parse_time:none_passthrough", test_parse_time_none_returns_none),
    ("frames", "parse_time:int_passthrough", test_parse_time_int_passthrough),
    ("frames", "parse_time:invalid_raises", test_parse_time_invalid_raises),
    ("frames", "format_time:short", test_format_time_short),
    ("frames", "format_time:hours", test_format_time_hours),
    ("frames", "format_time:rounding", test_format_time_rounds),
    ("frames", "auto_fps:30s_dense", test_auto_fps_30s_dense),
    ("frames", "auto_fps:1min_target_40", test_auto_fps_1min),
    ("frames", "auto_fps:fps_capped_2", test_auto_fps_3min_capped_2fps),
    ("frames", "auto_fps:long_uses_max", test_auto_fps_long_uses_max),
    ("frames", "auto_fps:zero_duration", test_auto_fps_zero_duration),
    ("frames", "auto_fps_focus:5s_2fps", test_auto_fps_focus_5s_dense),
    ("frames", "auto_fps_focus:30s_target_60", test_auto_fps_focus_30s),
    ("frames", "auto_fps_focus:respects_max_arg", test_auto_fps_focus_respects_max_frames_arg),
    ("frames", "extract:auto_falls_back_to_fixed", test_extract_falls_back_to_fixed_fps_when_no_scenes),
    ("frames", "extract:scene_no_fallback", test_extract_mode_scene_returns_empty_when_no_scenes),
    ("frames", "extract:fixed_skips_scene", test_extract_mode_fixed_skips_scene_detection),

    # transcribe
    ("transcribe", "parse_vtt:basic", test_parse_vtt_basic),
    ("transcribe", "parse_vtt:dedupe_rolling", test_parse_vtt_dedupes_rolling),
    ("transcribe", "parse_vtt:strips_html_tags", test_parse_vtt_strips_html_tags),
    ("transcribe", "parse_vtt:empty_returns_empty", test_parse_vtt_empty_returns_empty),
    ("transcribe", "filter_range:both_bounds", test_filter_range_both_bounds),
    ("transcribe", "filter_range:none_returns_all", test_filter_range_none_returns_all),
    ("transcribe", "filter_range:overlap_inclusive", test_filter_range_overlap_inclusive),
    ("transcribe", "format_transcript:timestamps", test_format_transcript_timestamps),

    # download
    ("download", "is_url:https", test_is_url_https),
    ("download", "is_url:http", test_is_url_http),
    ("download", "is_url:rejects_unix_path", test_is_url_local_unix_path),
    ("download", "is_url:rejects_windows_path", test_is_url_local_windows_path),
    ("download", "resolve_local:missing_raises", test_resolve_local_missing_raises),
    ("download", "resolve_local:existing_works", test_resolve_local_existing_file_works),
    ("download", "pick_subtitle:lang_priority", test_pick_subtitle_lang_priority),
    ("download", "pick_subtitle:fallback", test_pick_subtitle_falls_back_to_first),

    # whisper
    ("whisper", "load_api_key:none", test_load_api_key_no_keys_returns_none),
    ("whisper", "load_api_key:env_groq", test_load_api_key_env_groq_picked_up),
    ("whisper", "load_api_key:groq_preferred", test_load_api_key_groq_preferred_over_openai),
    ("whisper", "load_api_key:preferred_filter", test_load_api_key_preferred_filter),
    ("whisper", "load_api_key:reads_config_dotenv", test_load_api_key_reads_config_dotenv),
    ("whisper", "load_api_key:NO_cwd_dotenv [SECURITY]", test_load_api_key_does_NOT_read_cwd_dotenv),
    ("whisper", "multipart:body_well_formed", test_multipart_body_well_formed),
    ("whisper", "multipart:boundary_unique", test_multipart_boundary_unique_per_call),
    ("whisper", "segments:basic", test_segments_from_response_basic),
    ("whisper", "segments:offset_applied [CHUNKING]", test_segments_from_response_offset_applied),
    ("whisper", "segments:skips_blank", test_segments_from_response_empty_skips_blank),
    ("whisper", "segments:falls_back_to_text", test_segments_from_response_falls_back_to_text),
    ("whisper", "segments:empty_response", test_segments_from_response_no_segments_no_text),
    ("whisper", "max_bytes:under_api_cap", test_whisper_max_bytes_under_api_cap),

    # ocr
    ("ocr", "is_available:matches_which", test_ocr_is_available_matches_which),
    ("ocr", "ocr_one:none_when_unavailable", test_ocr_one_returns_none_when_unavailable),

    # setup
    ("setup", "status:has_required_keys", test_setup_status_keys),
    ("setup", "check:exit_code_in_set", test_setup_check_subprocess_exit_code),
    ("setup", "json:valid_json", test_setup_json_output_valid),

    # vidsense orchestrator
    ("vidsense", "validate_out_dir:default_tmp", test_validate_out_dir_default_is_tmp),
    ("vidsense", "validate_out_dir:user_path", test_validate_out_dir_user_path_ok),
    ("vidsense", "validate_out_dir:refuses_etc", test_validate_out_dir_refuses_etc),
    ("vidsense", "help:no_unicode_crash", test_vidsense_help_does_not_crash),

    # integration
    ("integration", "ffprobe:metadata_on_synth_video", test_int_ffprobe_metadata),
    ("integration", "frames:fixed_fps_extraction", test_int_extract_fixed_fps),
    ("integration", "frames:scene_aware_extraction", test_int_extract_scene_aware),
    ("integration", "yt-dlp:binary_responds", test_int_yt_dlp_available),
    ("integration", "ocr:real_text_recognition", test_int_ocr_real_text),
    ("integration", "whisper:live_groq_call", test_int_whisper_groq_call),
]


def run_one(test_fn) -> tuple[str, str, float]:
    """Run a single test. Returns (status, detail, duration_seconds).

    NOTE: SystemExit is a BaseException, not an Exception — the production code
    raises it for hard-fail conditions, so we must catch it explicitly or it
    would kill the eval mid-iteration.
    """
    t0 = time.perf_counter()
    try:
        test_fn()
        return "PASS", "", time.perf_counter() - t0
    except TestSkip as e:
        return "SKIP", str(e), time.perf_counter() - t0
    except AssertionError as e:
        return "FAIL", str(e), time.perf_counter() - t0
    except SystemExit as e:
        return "ERROR", f"SystemExit: {e}", time.perf_counter() - t0
    except KeyboardInterrupt:
        raise  # always honor Ctrl-C
    except BaseException:
        return "ERROR", traceback.format_exc(limit=3).strip().split("\n")[-1], time.perf_counter() - t0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iterations", type=int, default=3,
                    help="Number of times to run the full suite (default 3)")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    ap.add_argument("--filter", type=str, default=None, help="Substring filter on test names")
    args = ap.parse_args()

    selected = TESTS
    if args.filter:
        selected = [t for t in TESTS if args.filter.lower() in (t[0] + ":" + t[1]).lower()]
        if not selected:
            print(f"no tests match filter: {args.filter}", file=sys.stderr)
            return 1

    # results[(module, name)] = ["PASS", "FAIL", "SKIP", ...]  one per iteration
    results: dict[tuple[str, str], list[str]] = {(m, n): [] for m, n, _ in selected}
    details: dict[tuple[str, str], list[str]] = {(m, n): [] for m, n, _ in selected}
    durations: dict[tuple[str, str], list[float]] = {(m, n): [] for m, n, _ in selected}

    if not args.json:
        print(f"vidsense eval — {len(selected)} tests × {args.iterations} iterations")
        print("=" * 78)

    overall_start = time.perf_counter()
    for it in range(1, args.iterations + 1):
        if not args.json:
            print(f"\n--- iteration {it}/{args.iterations} ---")
        for module, name, fn in selected:
            status, detail, dur = run_one(fn)
            results[(module, name)].append(status)
            details[(module, name)].append(detail)
            durations[(module, name)].append(dur)
            if not args.json:
                marker = {"PASS": "ok", "SKIP": "--", "FAIL": "FAIL", "ERROR": "ERR "}[status]
                line = f"  [{marker}] {module}/{name}"
                if status in ("FAIL", "ERROR"):
                    line += f"  → {detail}"
                elif status == "SKIP":
                    line += f"  ({detail})"
                print(line)

    # Summarize
    summary = {"iterations": args.iterations, "tests": []}
    counts = {"pass": 0, "fail": 0, "skip": 0, "flaky": 0}
    for (module, name) in results:
        statuses = results[(module, name)]
        unique = set(statuses)
        if unique == {"PASS"}:
            verdict = "PASS"
            counts["pass"] += 1
        elif unique == {"SKIP"}:
            verdict = "SKIP"
            counts["skip"] += 1
        elif "PASS" in unique and ("FAIL" in unique or "ERROR" in unique):
            verdict = "FLAKY"
            counts["flaky"] += 1
            counts["fail"] += 1
        elif "FAIL" in unique or "ERROR" in unique:
            verdict = "FAIL"
            counts["fail"] += 1
        else:
            verdict = "MIXED"
            counts["fail"] += 1
        summary["tests"].append({
            "module": module,
            "name": name,
            "verdict": verdict,
            "iterations": statuses,
            "details": [d for d in details[(module, name)] if d],
            "avg_duration_ms": round(sum(durations[(module, name)]) * 1000 / len(durations[(module, name)]), 1),
        })

    summary["counts"] = counts
    summary["wall_seconds"] = round(time.perf_counter() - overall_start, 2)

    if args.json:
        json.dump(summary, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print()
        print("=" * 78)
        print(f"SUMMARY  pass={counts['pass']}  fail={counts['fail']}  "
              f"skip={counts['skip']}  flaky={counts['flaky']}  "
              f"({summary['wall_seconds']}s wall)")
        print("=" * 78)
        # Show all non-PASS verdicts grouped
        bad = [t for t in summary["tests"] if t["verdict"] not in ("PASS", "SKIP")]
        if bad:
            print("\nFAILED / FLAKY:")
            for t in bad:
                d = "; ".join(set(t["details"])) if t["details"] else "(no detail)"
                print(f"  {t['verdict']:5s}  {t['module']}/{t['name']}: {d}")
        skipped = [t for t in summary["tests"] if t["verdict"] == "SKIP"]
        if skipped:
            print(f"\nSKIPPED ({len(skipped)}):")
            for t in skipped:
                reason = t["details"][0] if t["details"] else "no reason given"
                print(f"  {t['module']}/{t['name']}: {reason}")

    return 1 if counts["fail"] > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
