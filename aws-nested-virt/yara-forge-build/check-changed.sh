#!/usr/bin/env bash
# yara-forge-build/check-changed.sh — decide whether this cron run
# should produce a new cape-yara-forge.deb or exit clean.
#
# Compares the latest YARA Forge GitHub release tag against the version
# of cape-yara-forge.deb currently in the apt repo's Packages.gz.  If
# they match, the deb on disk already matches upstream and a rebuild
# would publish a byte-identical (modulo metadata) deb that aptly's
# same-version-skip rejects anyway — skipping the build job saves the
# ~5 min CI per cron tick.
#
# YARA Forge publishes three flavors per release: core / extended /
# full.  We bake one flavor (default "extended"); the apt repo only
# carries that flavor's deb.  The check only needs to compare the
# release tag, not the flavor.
#
# Inputs (env):
#   APT_REPO_URL          required deb-release base URL (flat Packages.gz at
#                         its root; workflow points this at the dev release)
#   FORCE_REBUILD         if "true" → proceed regardless (manual/push paths)
#   YARA_FORGE_TAG        optional pin (mirrors fetch.sh's input)
#   GH_TOKEN              optional GitHub API token (avoids rate limit)
#
# Outputs (stdout, parsed as KEY=VALUE into $GITHUB_OUTPUT):
#   changed=true|false
#   upstream_tag=<tag>
#
# Mirrors suricata-rules-build/check-changed.sh + sigma-rules-build/
# check-changed.sh — same contract so the workflow can use the same
# `if: needs.check.outputs.changed == 'true'` gate.

set -euo pipefail

: "${APT_REPO_URL:?APT_REPO_URL required (for example, https://apt.example.com)}"
FORCE_REBUILD="${FORCE_REBUILD:-false}"

log() { echo "[$(date -Iseconds)] [yara-forge-check] $*" >&2; }

if [ -n "${YARA_FORGE_TAG:-}" ]; then
    UPSTREAM_TAG="$YARA_FORGE_TAG"
    log "Using pinned YARA_FORGE_TAG: $UPSTREAM_TAG"
else
    UPSTREAM_TAG=$(
        curl -fsSL ${GH_TOKEN:+-H "Authorization: Bearer $GH_TOKEN"} \
            "https://api.github.com/repos/YARAHQ/yara-forge/releases/latest" \
            | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])'
    )
    [ -n "$UPSTREAM_TAG" ] || { log "::error::could not resolve upstream tag"; exit 1; }
    log "Upstream YARA Forge tag: $UPSTREAM_TAG"
fi
echo "upstream_tag=$UPSTREAM_TAG"

# package.sh uses the tag as the deb Version directly (no 'v' prefix
# stripping — YARA Forge tags are bare dates like "20260507", not
# "v20260507").  Defensive strip anyway in case upstream changes.
UPSTREAM_VER="${UPSTREAM_TAG#v}"

if [ "$FORCE_REBUILD" = "true" ]; then
    log "FORCE_REBUILD=true → proceeding regardless of upstream state"
    echo "changed=true"
    exit 0
fi

# Pull the current cape-yara-forge Version field from the deb release's
# flat Packages.gz (single flat index — no dev/prod channel sub-path; the
# workflow points APT_REPO_URL at the dev release).
pkg_url="${APT_REPO_URL%/}/Packages.gz"
log "Fetching $pkg_url"

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
if ! curl -fsSL "$pkg_url" | gunzip > "$tmp" 2>/dev/null; then
    log "Could not fetch Packages.gz — assuming first publish, proceeding"
    echo "changed=true"
    exit 0
fi

CUR_VER=$(awk -v pkg="cape-yara-forge" '
    /^Package: / { p=$2 }
    /^Version: / && p==pkg { print $2; exit }
' "$tmp")
log "Current cape-yara-forge in apt: ${CUR_VER:-<missing>}"

if [ -n "$CUR_VER" ] && [ "$CUR_VER" = "$UPSTREAM_VER" ]; then
    log "Already at $UPSTREAM_VER — no change"
    echo "changed=false"
else
    log "Upstream $UPSTREAM_VER differs from apt ${CUR_VER:-<missing>} — rebuild needed"
    echo "changed=true"
fi
