#!/usr/bin/env bash
# clamav-mirror-build/check-changed.sh — decide whether to skip this
# cron cycle by comparing the composite manifest md5 we'd produce now
# against the manifest.md5 we stamped on the CDN at the last
# successful publish.
#
# This script DOESN'T actually re-pull all the upstreams to compute
# the new composite (that's the expensive part mirror.sh does). It
# uses a cheaper proxy: HTTP HEAD on each upstream URL, compose the
# Last-Modified + Content-Length headers into a fingerprint string.
# If THAT fingerprint matches the one we stashed last run, upstreams
# almost certainly haven't moved.
#
# Inputs:
#   THREAT_CONTENT_URL     required threat-content release base URL (single
#                          rolling release; flat, bare-named assets)
#   FORCE_REBUILD          if "true" → always proceed
#
# Outputs (printed in `KEY=VALUE` form for $GITHUB_OUTPUT consumption):
#   changed=true|false
#   upstream_fingerprint=<sha256-hex>

set -euo pipefail

: "${THREAT_CONTENT_URL:?THREAT_CONTENT_URL required (for example, https://github.com/OWNER/REPO/releases/download/threat-content)}"
FORCE_REBUILD="${FORCE_REBUILD:-false}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SOURCES_FILE="$REPO_ROOT/clamav-mirror-build/sources.txt"

log() { echo "[$(date -Iseconds)] [clamav-mirror-check] $*"; }

# Build a fingerprint string by HEADing each upstream URL and
# concatenating the relevant headers. Cheap; doesn't pull payloads.
FP_LINES=""
while IFS= read -r line; do
    line="${line%%#*}"
    line="$(echo "$line" | xargs)"
    [ -z "$line" ] && continue
    # HEAD with timeout; if a source is down the missing header is
    # naturally part of the fingerprint (different from "source up").
    hdrs=$(curl -fsSI --max-time 15 "$line" 2>/dev/null || echo "MISS")
    lm=$(echo "$hdrs" | grep -i '^last-modified:' | tr -d '\r' || echo "")
    cl=$(echo "$hdrs" | grep -i '^content-length:' | tr -d '\r' || echo "")
    et=$(echo "$hdrs" | grep -i '^etag:'          | tr -d '\r' || echo "")
    FP_LINES+="$line | $lm | $cl | $et"$'\n'
done < "$SOURCES_FILE"

NEW_FP=$(printf '%s' "$FP_LINES" | sha256sum | awk '{print $1}')
log "Composite upstream fingerprint: $NEW_FP"
echo "upstream_fingerprint=$NEW_FP"

if [ "$FORCE_REBUILD" = "true" ]; then
    log "FORCE_REBUILD=true → proceeding regardless"
    echo "changed=true"
    exit 0
fi

LAST_FP_URL="${THREAT_CONTENT_URL%/}/upstream-fingerprint.txt"
log "Fetching last-published fingerprint from $LAST_FP_URL"
LAST_FP=$(curl -fsS --max-time 10 "$LAST_FP_URL" 2>/dev/null | head -1 | awk '{print $1}' || echo "")
if [ -z "$LAST_FP" ]; then
    log "No last-published fingerprint — first publish; proceeding"
    echo "changed=true"
    exit 0
fi
log "Last-published fingerprint: $LAST_FP"

if [ "$NEW_FP" = "$LAST_FP" ]; then
    log "Upstream HEAD headers unchanged since last publish — skipping rebuild"
    echo "changed=false"
else
    log "Upstream HEAD headers changed → rebuilding"
    echo "changed=true"
fi
