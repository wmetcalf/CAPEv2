#!/usr/bin/env bash
# sigma-rules-build/check-changed.sh — decide whether this cron run
# should produce a new cape-sigma-rules.deb or exit clean.
#
# Compares the latest Zircolite GitHub release tag against the version
# of cape-sigma-rules.deb currently in the apt repo's Packages.gz.  If
# they match, the deb on disk already matches upstream and a rebuild
# would publish a byte-identical (modulo metadata) deb that aptly's
# same-version-skip rejects anyway — so the entire build job can be
# skipped, sparing ~5 min CI per cron tick.
#
# Inputs (env):
#   APT_REPO_URL          required deb-release base URL (flat Packages.gz at
#                         its root; workflow points this at the dev release)
#   FORCE_REBUILD         if "true" → proceed regardless (manual/push paths)
#   GH_TOKEN              optional GitHub API token (avoids rate limit)
#
# Outputs (stdout, parsed as KEY=VALUE into $GITHUB_OUTPUT):
#   changed=true|false
#   upstream_tag=<v.x.y>
#
# Mirrors the design of suricata-rules-build/check-changed.sh + clamav-
# mirror-build/check-changed.sh — same input/output contract so the
# workflow can wire it up with the same `if: needs.check.outputs.changed
# == 'true'` gate.

set -euo pipefail

: "${APT_REPO_URL:?APT_REPO_URL required (for example, https://apt.example.com)}"
FORCE_REBUILD="${FORCE_REBUILD:-false}"

log() { echo "[$(date -Iseconds)] [sigma-rules-check] $*" >&2; }

UPSTREAM_TAG=$(
    curl -fsSL ${GH_TOKEN:+-H "Authorization: Bearer $GH_TOKEN"} \
        "https://api.github.com/repos/wagga40/Zircolite/releases/latest" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])'
)
[ -n "$UPSTREAM_TAG" ] || { log "::error::could not resolve upstream tag"; exit 1; }
log "Upstream Zircolite tag: $UPSTREAM_TAG"
echo "upstream_tag=$UPSTREAM_TAG"

# package.sh strips the leading 'v' for the deb Version field (Debian
# style — "Version: 2.10.0" not "Version: v2.10.0").  Mirror that
# transformation so the apt-repo comparison stays apples-to-apples.
UPSTREAM_VER="${UPSTREAM_TAG#v}"

if [ "$FORCE_REBUILD" = "true" ]; then
    log "FORCE_REBUILD=true → proceeding regardless of upstream state"
    echo "changed=true"
    exit 0
fi

# Pull the current cape-sigma-rules Version field from the deb release's
# flat Packages.gz (single flat index — no dev/prod channel sub-path; the
# workflow points APT_REPO_URL at the dev release).
pkg_url="${APT_REPO_URL%/}/Packages.gz"
log "Fetching $pkg_url"

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
if ! curl -fsSL "$pkg_url" | gunzip > "$tmp" 2>/dev/null; then
    # Packages.gz doesn't exist yet (very first publish on this
    # channel).  Falls through to "must build" — this is the
    # fail-safe direction.
    log "Could not fetch Packages.gz — assuming first publish, proceeding"
    echo "changed=true"
    exit 0
fi

CUR_VER=$(awk -v pkg="cape-sigma-rules" '
    /^Package: / { p=$2 }
    /^Version: / && p==pkg { print $2; exit }
' "$tmp")
log "Current cape-sigma-rules in apt: ${CUR_VER:-<missing>}"

if [ -n "$CUR_VER" ] && [ "$CUR_VER" = "$UPSTREAM_VER" ]; then
    log "Already at $UPSTREAM_VER — no change"
    echo "changed=false"
else
    log "Upstream $UPSTREAM_VER differs from apt ${CUR_VER:-<missing>} — rebuild needed"
    echo "changed=true"
fi
