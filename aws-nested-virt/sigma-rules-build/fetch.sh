#!/usr/bin/env bash
# sigma-rules-build/fetch.sh — pull pre-converted sigma rule packs
# from Zircolite (github.com/wagga40/Zircolite).
#
# CAPE consumes sigma via Zircolite (evtx-tuned JSON pack format, not
# raw SigmaHQ YAML). The cape-sigma-update.service on the host used
# to invoke `zircolite.py --update-rules` at runtime; we shift that
# to build time and ship the result as cape-sigma-rules.deb so
# enterprise tenants don't reach GitHub from analysis hosts.
#
# Inputs:
#   WORK_DIR             required, staging dir
#   ZIRCOLITE_TAG        optional, pin a specific release tag (default = latest)
#   GH_TOKEN             recommended, GitHub API rate-limit token
#
# Outputs:
#   $WORK_DIR/rules/rules_*.json  — copied from upstream rules/
#   $WORK_DIR/release-tag         — pinned tag for package.sh version stamp

set -euo pipefail

: "${WORK_DIR:?WORK_DIR required}"

log() { echo "[$(date -Iseconds)] [sigma-rules-fetch] $*"; }

mkdir -p "$WORK_DIR/rules" "$WORK_DIR/dl"

if [ -n "${ZIRCOLITE_TAG:-}" ]; then
    TAG="$ZIRCOLITE_TAG"
    log "Using pinned tag: $TAG"
else
    log "Resolving latest Zircolite release tag"
    TAG=$(gh api repos/wagga40/Zircolite/releases/latest --jq '.tag_name' 2>/dev/null \
       || curl -fsSL "https://api.github.com/repos/wagga40/Zircolite/releases/latest" \
          | python3 -c 'import json,sys;print(json.load(sys.stdin)["tag_name"])')
    [ -n "$TAG" ] || { log "::error::could not resolve latest tag"; exit 1; }
    log "Latest tag: $TAG"
fi
echo "$TAG" > "$WORK_DIR/release-tag"

# Zircolite's rules/ subdir holds pre-converted JSON packs (one large
# file per OS/severity slice; rules_*.json). They're committed to the
# repo, so a shallow clone at the tag is the cleanest pull — no need
# to invoke zircolite itself.
log "Shallow-cloning Zircolite at $TAG"
git clone --depth=1 --branch "$TAG" \
    https://github.com/wagga40/Zircolite.git "$WORK_DIR/dl/Zircolite" >/dev/null 2>&1

SRC_RULES="$WORK_DIR/dl/Zircolite/rules"
[ -d "$SRC_RULES" ] || { log "::error::no rules/ subdir at $SRC_RULES"; exit 1; }

# Copy all rules_*.json into our staging area. We ship every flavor
# (windows_generic / windows_merged / windows_sysmon / linux at all
# severity slices); CAPE's sigma module discovers them all under
# /opt/CAPEv2/data/sigma/.
count=0
for f in "$SRC_RULES"/rules_*.json; do
    [ -f "$f" ] || continue
    install -m 0644 "$f" "$WORK_DIR/rules/$(basename "$f")"
    count=$((count + 1))
done
log "Copied $count rule-pack files to $WORK_DIR/rules/"

[ "$count" -ge 4 ] || { log "::error::implausibly few rule packs ($count)"; exit 1; }

# Quick sanity: each file should be valid JSON.
for f in "$WORK_DIR/rules"/rules_*.json; do
    python3 -c "import json,sys; json.load(open('$f'))" 2>/dev/null \
        || { log "::error::invalid JSON in $(basename "$f")"; exit 1; }
done
log "JSON validation passed for all $count files"

log "fetch complete"
