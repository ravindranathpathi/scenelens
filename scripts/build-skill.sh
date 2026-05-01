#!/usr/bin/env bash
# build-skill.sh — package this repo as a claude.ai-upload-ready .skill file.
# Usage: bash scripts/build-skill.sh  (run from repo root)
#
# Produces dist/vidsense.skill, a zip with a single top-level `vidsense/`
# directory containing SKILL.md and the scripts/ runtime. claude.ai's skill
# upload has a 200-file cap.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "error: working tree is dirty; commit or stash before building" >&2
  exit 1
fi

mkdir -p dist
OUT="dist/vidsense.skill"
git archive --format=zip --prefix=vidsense/ --output="$OUT" HEAD

# Strip Claude-Code-only directories from the .skill bundle. They must stay
# in the git archive (Claude Code's /plugin install pulls the same tarball)
# but the claude.ai bundle should ship only SKILL.md + scripts/.
#
# Use Python's stdlib zipfile rather than the `zip` CLI — `zip` is missing
# from minimal Git-Bash and Alpine environments, and the previous `zip -d`
# call silently no-op'd when the binary was absent (allowed bloated bundles
# to ship). Python is already a runtime dependency for the skill itself.
PYTHON=$(command -v python3 || command -v python)
"$PYTHON" - "$OUT" <<'PY'
import shutil, sys, zipfile
src = sys.argv[1]
exclude_prefixes = (
    "vidsense/hooks/",
    "vidsense/commands/",
    "vidsense/.claude-plugin/",
)
tmp = src + ".tmp"
removed = 0
with zipfile.ZipFile(src, "r") as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
    for item in zin.infolist():
        if any(item.filename.startswith(p) for p in exclude_prefixes):
            removed += 1
            continue
        zout.writestr(item, zin.read(item.filename))
shutil.move(tmp, src)
print(f"stripped {removed} Claude-Code-only entries", file=sys.stderr)
PY

COUNT=$(unzip -l "$OUT" | tail -1 | awk '{print $2}')
SIZE=$(du -h "$OUT" | cut -f1)

if [ "$COUNT" -gt 200 ]; then
  echo "error: $COUNT files in zip, claude.ai's cap is 200" >&2
  exit 1
fi

SKILL_MD_COUNT=$(unzip -l "$OUT" | grep -c "SKILL.md" || true)
if [ "$SKILL_MD_COUNT" -ne 1 ]; then
  echo "error: expected exactly one SKILL.md, found $SKILL_MD_COUNT" >&2
  exit 1
fi

echo "built $OUT ($COUNT files, $SIZE)"
echo "upload via the claude.ai skill UI"
