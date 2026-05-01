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
zip -d "$OUT" \
  "vidsense/hooks/*" \
  "vidsense/commands/*" \
  "vidsense/.claude-plugin/*" \
  > /dev/null 2>&1 || true

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
