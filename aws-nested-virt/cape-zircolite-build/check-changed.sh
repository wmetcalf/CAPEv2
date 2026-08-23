#!/usr/bin/env bash
# cape-zircolite-build/check-changed.sh — skip the build if the apt repo
# already carries a cape-zircolite deb at the PINNED tag.
# Mirrors yara-forge-build/check-changed.sh.
#
# We deliberately do NOT track upstream "latest": Zircolite runs inside
# cape's shared venv and its requirements.txt drifts/tightens per release
# (e.g. 3.7.x's chardet>=5 is unsatisfiable vs cape's chardet==4.0.0), so
# the version is pinned in the sibling `pinned-tag` file — the SAME source
# fetch.sh builds from.  Comparing against that pin (not latest) is what
# makes the monthly cron a true no-op until someone deliberately bumps the
# pin; comparing against latest made every run report "changed" and rebuild
# the already-published pinned version.
#
# Inputs (env):
#   APT_REPO_URL          required deb-release base URL (flat Packages.gz at
#                         its root; workflow points this at the dev release)
#   FORCE_REBUILD         "true" → proceed regardless (manual/push paths)
#   ZIRCOLITE_TAG         optional override of the pinned tag (mirrors fetch.sh)
#
# Outputs (stdout, parsed into $GITHUB_OUTPUT):
#   changed=true|false
#   upstream_tag=<tag>   (the pinned/target tag this would build)

set -euo pipefail
: "${APT_REPO_URL:?APT_REPO_URL required (for example, https://apt.example.com)}"
FORCE_REBUILD="${FORCE_REBUILD:-false}"
log() { echo "[$(date -Iseconds)] [zircolite-check] $*" >&2; }

# Target tag = ZIRCOLITE_TAG override, else the pinned-tag file (shared with
# fetch.sh — the single source of truth, NOT upstream "latest").
TARGET_TAG="${ZIRCOLITE_TAG:-$(cat "$(dirname "${BASH_SOURCE[0]}")/pinned-tag")}"
[ -n "$TARGET_TAG" ] || { log "::error::could not resolve pinned tag"; exit 1; }
log "Pinned Zircolite tag: $TARGET_TAG"
echo "upstream_tag=$TARGET_TAG"

# Deb Version uses the tag with any leading 'v' stripped.
UPSTREAM_VER="${TARGET_TAG#v}"

if [ "$FORCE_REBUILD" = "true" ]; then
    log "FORCE_REBUILD=true → proceeding"
    echo "changed=true"; exit 0
fi

# Single flat Packages.gz — no dev/prod channel sub-path; the workflow
# points APT_REPO_URL at the dev release.
pkg_url="${APT_REPO_URL%/}/Packages.gz"
log "Fetching $pkg_url"
tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
if ! curl -fsSL "$pkg_url" | gunzip > "$tmp" 2>/dev/null; then
    log "Could not fetch Packages.gz — assuming first publish, proceeding"
    echo "changed=true"; exit 0
fi
CUR_VER=$(awk -v pkg="cape-zircolite" '/^Package: /{p=$2} /^Version: /&&p==pkg{print $2; exit}' "$tmp")
log "Current cape-zircolite in apt: ${CUR_VER:-<missing>}"
if [ -n "$CUR_VER" ] && [ "$CUR_VER" = "$UPSTREAM_VER" ]; then
    log "Already at $UPSTREAM_VER — no change"; echo "changed=false"
else
    log "Pinned $UPSTREAM_VER differs from apt ${CUR_VER:-<missing>} — rebuild"; echo "changed=true"
fi
