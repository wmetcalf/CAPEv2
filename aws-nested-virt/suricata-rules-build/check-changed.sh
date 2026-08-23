#!/usr/bin/env bash
# suricata-rules-build/check-changed.sh — decide whether this run
# should produce a new artifact or skip cleanly.
#
# Inputs:
#   SURICATA_VERSION       default 7.0.13
#   ET_OPEN_BASE_URL       default https://rules.emergingthreats.net/open
#   THREAT_CONTENT_URL     required threat-content release base URL (single
#                          rolling release; flat, bare-named assets)
#   FORCE_REBUILD          if "true" → always proceed (manual dispatch / push)
#
# Outputs:
#   prints "changed=true|false" to $GITHUB_OUTPUT (via the calling step)
#   prints "et_md5=<hex>" with the upstream md5 we just looked up
#
# Logic:
#   1. Fetch upstream ET Open .tar.gz.md5 (multiple-per-day cadence).
#   2. Fetch the last-published "upstream-et-md5.txt" from the
#      threat-content release (dropped alongside the tarball on every publish).
#   3. If upstream md5 == last-published md5 → no change, skip.
#   4. FORCE_REBUILD=true bypasses the comparison (manual + push paths
#      always proceed regardless).
#
# Note: this only checks the ET Open side. Cape-project addition edits
# trigger the workflow via the `push:` event with FORCE_REBUILD set,
# so they don't need to participate in the cadence comparison.

set -euo pipefail

SURICATA_VERSION="${SURICATA_VERSION:-7.0.13}"
ET_OPEN_BASE_URL="${ET_OPEN_BASE_URL:-https://rules.emergingthreats.net/open}"
: "${THREAT_CONTENT_URL:?THREAT_CONTENT_URL required (for example, https://github.com/OWNER/REPO/releases/download/threat-content)}"
FORCE_REBUILD="${FORCE_REBUILD:-false}"

log() { echo "[$(date -Iseconds)] [suricata-rules-check] $*"; }

ET_MD5_URL="${ET_OPEN_BASE_URL}/suricata-${SURICATA_VERSION}/emerging.rules.tar.gz.md5"
log "Fetching upstream md5 from $ET_MD5_URL"
ET_NEW_MD5=$(curl -fsS "$ET_MD5_URL" | awk '{print $1}' | head -1)
[ -n "$ET_NEW_MD5" ] || { log "::error::could not resolve upstream ET md5"; exit 1; }
log "Upstream md5: $ET_NEW_MD5"

# Always export the new md5 so package.sh can stamp it on the marker
# file for the next run.
echo "et_md5=$ET_NEW_MD5"

if [ "$FORCE_REBUILD" = "true" ]; then
    log "FORCE_REBUILD=true → proceeding regardless of upstream state"
    echo "changed=true"
    exit 0
fi

LAST_MD5_URL="${THREAT_CONTENT_URL%/}/upstream-et-md5.txt"
log "Fetching last-published marker from $LAST_MD5_URL"
ET_LAST_MD5=$(curl -fsS "$LAST_MD5_URL" 2>/dev/null | awk '{print $1}' | head -1 || echo "")
if [ -z "$ET_LAST_MD5" ]; then
    log "No last-published marker — first publish; proceeding"
    echo "changed=true"
    exit 0
fi
log "Last-published md5: $ET_LAST_MD5"

if [ "$ET_NEW_MD5" = "$ET_LAST_MD5" ]; then
    log "Upstream unchanged since last publish — skipping rebuild"
    echo "changed=false"
else
    log "Upstream changed ($ET_LAST_MD5 → $ET_NEW_MD5) — rebuilding"
    echo "changed=true"
fi
