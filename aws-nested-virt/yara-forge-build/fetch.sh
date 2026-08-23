#!/usr/bin/env bash
# yara-forge-build/fetch.sh — pull the latest YARA Forge release from
# github.com/YARAHQ/yara-forge and stage the rule bundle into $WORK_DIR.
#
# YARA Forge publishes three flavors per release: core / extended / full.
#  - core     ~3k rules, hand-curated, low FP
#  - extended ~25k rules, broader coverage, some FP noise — our default
#  - full     ~45k+ rules, full firehose
#
# Override with YARA_FORGE_FLAVOR=core|extended|full.
#
# Outputs:
#   $WORK_DIR/rules/yara-rules-<flavor>.yar
#   $WORK_DIR/release-tag  (e.g. "20260507")  — read by package.sh
#
# Inputs:
#   YARA_FORGE_FLAVOR (optional, default extended)
#   YARA_FORGE_TAG    (optional, override "latest" — for reproducible builds)
#   WORK_DIR          (required) where to stage download + extracted rules
#   GH_TOKEN          (recommended) github API token for rate limits

set -euo pipefail

: "${WORK_DIR:?WORK_DIR required}"
FLAVOR="${YARA_FORGE_FLAVOR:-extended}"

log() { echo "[$(date -Iseconds)] [yara-forge-fetch] $*"; }

case "$FLAVOR" in
    core|extended|full) ;;
    *) echo "::error::invalid YARA_FORGE_FLAVOR=$FLAVOR (must be core|extended|full)"; exit 2 ;;
esac

mkdir -p "$WORK_DIR/rules" "$WORK_DIR/dl"

# Resolve the release tag. "latest" → ask GitHub API for the newest release.
if [ -n "${YARA_FORGE_TAG:-}" ]; then
    TAG="$YARA_FORGE_TAG"
    log "Using pinned tag: $TAG"
else
    log "Resolving latest YARA Forge release tag from GitHub API"
    TAG=$(gh api repos/YARAHQ/yara-forge/releases/latest --jq '.tag_name' 2>/dev/null \
       || curl -fsSL "https://api.github.com/repos/YARAHQ/yara-forge/releases/latest" \
          | python3 -c 'import json,sys;print(json.load(sys.stdin)["tag_name"])')
    [ -n "$TAG" ] || { echo "::error::failed to resolve latest yara-forge tag"; exit 1; }
    log "Latest tag: $TAG"
fi

echo "$TAG" > "$WORK_DIR/release-tag"

# YARA Forge release artifact naming convention:
#   yara-forge-rules-<flavor>.zip   (the rule zip — note the "forge-" prefix)
# Each zip's tree:
#   packages/<flavor>/yara-rules-<flavor>.yar   (note: NO "forge-" prefix inside)
#   packages/<flavor>/statistics-yara-rules.txt (not present in all releases)
ZIP_NAME="yara-forge-rules-${FLAVOR}.zip"
log "Downloading $ZIP_NAME from release $TAG"
if command -v gh >/dev/null 2>&1; then
    gh release download "$TAG" --repo YARAHQ/yara-forge --pattern "$ZIP_NAME" --dir "$WORK_DIR/dl"
else
    URL="https://github.com/YARAHQ/yara-forge/releases/download/${TAG}/${ZIP_NAME}"
    curl -fsSL -o "$WORK_DIR/dl/$ZIP_NAME" "$URL"
fi

log "Extracting $ZIP_NAME"
unzip -q "$WORK_DIR/dl/$ZIP_NAME" -d "$WORK_DIR/dl/"

# Locate the rules .yar (yara-forge nests it under packages/<flavor>/)
RULES_YAR=$(find "$WORK_DIR/dl" -name "yara-rules-${FLAVOR}.yar" -type f | head -1)
[ -n "$RULES_YAR" ] || { echo "::error::yara-rules-${FLAVOR}.yar not found in zip"; ls -lR "$WORK_DIR/dl"; exit 1; }

cp "$RULES_YAR" "$WORK_DIR/rules/yara-rules-${FLAVOR}.yar"

# Also stash the upstream statistics file for traceability.
STATS=$(find "$WORK_DIR/dl" -name "statistics-yara-rules.txt" -type f | head -1)
[ -n "$STATS" ] && cp "$STATS" "$WORK_DIR/rules/statistics-yara-rules-${FLAVOR}.txt"

# Sanity: confirm the .yar parses by counting `rule <name>` tokens.
RULE_COUNT=$(grep -cE '^rule [A-Za-z0-9_]+' "$WORK_DIR/rules/yara-rules-${FLAVOR}.yar" || echo 0)
log "Staged $RULE_COUNT rules at $WORK_DIR/rules/yara-rules-${FLAVOR}.yar"
[ "$RULE_COUNT" -gt 100 ] || { echo "::error::implausibly few rules ($RULE_COUNT) — refusing to ship"; exit 1; }

log "fetch complete"
