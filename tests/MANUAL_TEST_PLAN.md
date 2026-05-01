# Manual Test Plan — scenelens

Run this before pushing v0.1.0 to GitHub. The automated eval (`python tests/eval.py`) verifies the code logic. This plan verifies the **product** — that scenelens actually answers questions about real videos correctly, on a real machine, with real keys.

Budget: ~10–15 minutes wall time, < $0.10 in Groq API costs (assuming Whisper fires on 2 of the 7 tests).

---

## 0. Pre-flight gate

Don't proceed until these pass.

```bash
# 1. All four binaries on PATH
ffmpeg -version | head -1
ffprobe -version | head -1
yt-dlp --version
tesseract --version | head -1     # optional, but unlocks OCR

# 2. Groq key configured (rotate the one in chat, paste new one yourself)
cat ~/.config/scenelens/.env | grep GROQ_API_KEY
# or:  echo $GROQ_API_KEY

# 3. Automated eval green
python tests/eval.py --iterations 3
# Must show:  64 pass / 0 fail / 0 flaky
# (1 of the 2 expected skips disappears once a valid Groq key is set)

# 4. README placeholders replaced
grep -r "ravindranathpathi\|YOUR_NAME" --exclude-dir=.git
# Must return zero matches.
```

If any of those fail, fix before continuing.

---

## 1. The Showcase Test — scene-aware frame selection

**What it validates**: scene-aware frame selection (the headline feature).

**Pick a video**: a YouTube tutorial that's mostly someone's face for the first half, then switches to a screen demo / slide deck for the second half. Tech-creator content (Fireship, Theo, Primeagen, MKBHD, etc.) usually fits. 5-10 minutes ideal.

```
/scenelens <URL> what visual content does the second half of the video show?
```

**Pass criteria**:
- Report header shows `Frames: N via scene detection (threshold 0.30, max 80)` — confirms scene mode triggered
- Claude's answer references **specific visual elements** from the demo (UI labels, code snippets, slide titles), not just the speaker
- Claude cites **timestamps** that fall in the demo half, not the talking-head half

**Screenshot opportunity**: the frame-strip section of the report is the cleanest visual proof of scene-aware sampling. Frames cluster naturally around the demo's UI changes — open one of the JPEGs from a clustered region and you'll see a real UI moment, not a duplicate of the speaker. Crop a horizontal strip of 8-12 thumbnails for the launch thread.

---

## 2. Pure Talking-Head Test — fallback validation

**What it validates**: when no scene changes are detected, scenelens auto-falls back to fixed-fps instead of returning zero frames.

**Pick a video**: a single-take vlog or "talking to camera" clip. 1–3 minutes. No cuts. Some podcasters' YouTube uploads work (single static camera).

```
/scenelens <URL> summarize the main argument
```

**Pass criteria**:
- Report shows `Frames: N @ X.XXX fps fixed (max 80) — fallback: <8 scene changes detected`
- Claude provides a coherent summary (proves transcript pipeline worked even with sparse visual signal)

If you see `Frames: 0` with no fallback message, the auto-mode dispatch is broken. Open an issue.

---

## 3. Long-Form Test — chunking + warning + focused mode

**What it validates**: Whisper auto-chunking, the long-video warning, and focused-mode density.

**Pick a video**: a 60+ minute podcast or lecture on YouTube. (If it has captions, this won't exercise chunking — for that, pick something caption-less, see Test 6.)

```bash
# Step 3a — full-length sparse pass
/scenelens <URL> what's the main argument here?
```

**Pass criteria for 3a**:
- Report shows the warning: `> Note: This is a N-minute video...`
- Report shows ~100 frames at low fps (sparse but workable)
- Claude's answer is reasonably accurate but acknowledges limitations or gives a high-level summary

```bash
# Step 3b — focused on a 30-second window
/scenelens <URL> --start 25:00 --end 25:30 what's being said and shown right here?
```

**Pass criteria for 3b**:
- Report shows `Focus range: 25:00 → 25:30 (30.0s)` and far denser frames in that window
- Transcript is filtered to that range only (no segments outside it)
- Claude's answer is markedly more detailed than 3a's at that timestamp

---

## 4. Local Screen Recording — OCR validation

**What it validates**: tesseract integration on real on-screen text.

**Get a recording**: capture 30-60 seconds of your own screen showing code, a terminal, slides, or a dashboard. Use Win+G (Windows Game Bar), QuickTime (Mac), or OBS. Save as `~/test-screen.mp4`.

```bash
/scenelens ~/test-screen.mp4 what code/text is visible on screen?
```

**Pass criteria**:
- Report header shows `OCR: N/M frames have text (X chars total)` with N > 0
- At least one frame in the report has an `**OCR:**` line below it
- Claude's answer includes verbatim strings from your recording (proving OCR text reached Claude)
- The verbatim strings are spelled correctly within reason — Tesseract isn't perfect on small fonts, but headings/buttons should match

**If OCR finds nothing**: try `--resolution 1024` to bump frame width (default 512 may be too small for tiny code).

---

## 5. Non-YouTube Source — TikTok / X / Instagram

**What it validates**: yt-dlp covers more than YouTube; auto-Whisper triggers on caption-less sources.

**Pick a video**: a TikTok or X (Twitter) video without auto-captions. 30-90 seconds.

```bash
/scenelens <TikTok-URL> what's happening in this video?
```

**Pass criteria**:
- yt-dlp downloads successfully (no "no video file produced" error)
- Report shows `Transcript: N segments (via whisper (groq))` — confirms Whisper fallback fired
- Claude's answer references both visual and audio content correctly

**Common failure**: regional restrictions or the platform updated their API. If yt-dlp fails, that's not a scenelens bug — try a different source.

---

## 6. Caption-less YouTube — explicit Whisper path

**What it validates**: native captions take priority; Whisper kicks in only when missing.

**Pick a video**: a YouTube upload from a channel that doesn't enable auto-captions (rare but exists), OR force the path with `--no-whisper` first to confirm captions aren't there, then drop the flag.

```bash
# Confirm no captions (should output "Transcript: none available")
/scenelens <URL> --no-whisper anything

# Now let Whisper handle it
/scenelens <URL> what was said?
```

**Pass criteria**:
- First run: `Transcript: none available`
- Second run: `Transcript: N segments (via whisper (groq))`
- Claude's answer in the second run includes spoken content that matches the video

---

## 7. Follow-up Question — context efficiency

**What it validates**: the SKILL.md guidance that says Claude should NOT re-run the script when you ask a follow-up about a video already in context. This is a real cost-saver in practice.

**Pick a video**: any video used in Tests 1-6 that you've already watched in this session.

**Run, then immediately ask a follow-up**:
```
/scenelens <URL> what is this video about?
# Wait for Claude's answer, then in the SAME conversation:
what specifically happens around the 1:00 mark?
```

**Pass criteria**:
- The follow-up answer arrives **without** scenelens re-running. No new "downloading via yt-dlp", no new working directory printed.
- The answer is grounded in the frames + transcript already in context — Claude cites the right timestamp and visual content from memory of the original report.

**Why this matters**: each scenelens run costs API tokens and latency. Reusing context for follow-ups is the difference between a $0.05 conversation and a $0.20 one.

---

## Scorecard

Fill in as you go. Take screenshots of any failures.

| # | Test | Pass / Fail | Notes |
|---|------|-------------|-------|
| 0 | Pre-flight gate (eval green, deps, key, no placeholders) | | |
| 1 | Showcase (talking-head + demo, scene mode triggered) | | |
| 2 | Pure talking-head (fallback to fixed-fps) | | |
| 3a | Long-form full pass (warning printed) | | |
| 3b | Long-form focused (`--start`/`--end` denser) | | |
| 4 | Local screen recording (OCR finds text) | | |
| 5 | Non-YouTube (TikTok/X, Whisper fires) | | |
| 6 | Caption-less YouTube (Whisper fires correctly) | | |
| 7 | Follow-up question reuses context (no re-run) | | |

---

## Publish gate

You're ready to push when:

- [ ] Pre-flight gate green
- [ ] All 7 manual tests pass (or any failures investigated and explained)
- [ ] At least one good scenelens report screenshot captured (Test 1's frame strip, Test 4's OCR, or Test 6's transcript section)
- [ ] README placeholders all replaced (`YOUR_NAME`, `ravindranathpathi`)
- [ ] CHANGELOG.md dated (today's date)
- [ ] LICENSE owner is you, not the placeholder
- [ ] `git status` is clean (or only intentional untracked files)

Then:

```bash
cd /path/to/scenelens

git init
git add .
git commit -m "Initial release: scene-aware frames, OCR, chunked Whisper, eval suite"

git remote add origin git@github.com:ravindranathpathi/scenelens.git
git push -u origin main

# Trigger the release workflow
git tag v0.1.0
git push --tags
```

The GitHub Actions workflow at `.github/workflows/release.yml` builds `dist/scenelens.skill` and attaches it to the release within ~2 minutes. Verify the release page shows the `.skill` artifact before tweeting.

---

## After publishing

- [ ] Verify `/plugin marketplace add YOUR_HANDLE/scenelens` works on a fresh Claude Code install
- [ ] Verify the `.skill` upload path on claude.ai (Settings → Capabilities → Skills → +)
- [ ] Post the Twitter thread with one or two scenelens screenshots (frame strip from Test 1; OCR-rich report from Test 4)
- [ ] Cross-post to relevant communities (r/ClaudeAI, r/LocalLLaMA, Hacker News if it gets traction)

---

## If something fails

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `--check` exit 4 | binaries or key missing | Run `python scripts/setup.py` |
| `Frames: 0` on auto mode | scene detection broken AND fallback dispatch broken | Re-run `python tests/eval.py --filter extract:` to localize |
| `OCR: unavailable` | tesseract not on PATH | `winget install UB-Mannheim.TesseractOCR`; restart shell |
| `Whisper request failed: HTTP Error 401` | invalid Groq/OpenAI key | Rotate at console; update `~/.config/scenelens/.env` |
| Whisper hangs > 30s on small audio | network or Groq region issue | Try `--whisper openai` |
| `yt-dlp did not produce a video file` | URL geo-locked, age-gated, or yt-dlp out of date | `pip install --user --upgrade yt-dlp` |
| Em-dashes / arrows crash on Windows | UTF-8 reconfig didn't apply | Verify Python ≥ 3.7 (`python --version`) |
