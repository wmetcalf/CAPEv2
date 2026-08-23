#!/usr/bin/env bash
# clamav-mirror-build/mirror.sh — pull every URL listed in sources.txt
# and stage the files under $WORK_DIR/cdn/clamav-extra/.
#
# Mirrors only the 3rd-party / non-mainline ClamAV feeds — SaneSecurity,
# URLhaus, twinclams, wmetcalf clam-punch.  Cisco-Talos's standard
# CVDs (main.cvd, daily.cvd, bytecode.cvd) stay on the public
# database.clamav.net mirror network — there's no value in
# re-distributing those through the local mirror infra.
#
# Published layout after apt-publish flattens the fanned-in CDN tree:
#   <threat-content release>/<file>               ← THIS workflow's feeds,
#                                                   flat bare-named assets
#                                                   (no bucket, no path segment)
#   (untouched) database.clamav.net               ← Cisco
#
# Operator host's freshclam.conf will then be:
#   DatabaseMirror database.clamav.net            # Cisco standard
#   DatabaseCustomURL https://apt.example.invalid/junk.ndb
#   DatabaseCustomURL https://apt.example.invalid/urlhaus.ndb
#   ...
#
# Inputs:
#   WORK_DIR             required, staging dir
#
# Outputs:
#   $WORK_DIR/cdn/clamav-extra/<basename>    one file per sources.txt entry
#   $WORK_DIR/cdn/clamav-extra/manifest.txt  composite hash + per-file md5s

set -euo pipefail

: "${WORK_DIR:?WORK_DIR required}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SOURCES_FILE="$REPO_ROOT/clamav-mirror-build/sources.txt"

log() { echo "[$(date -Iseconds)] [clamav-mirror] $*"; }

[ -f "$SOURCES_FILE" ] || { log "::error::missing $SOURCES_FILE"; exit 1; }

OUT_DIR="$WORK_DIR/cdn/clamav-extra"
mkdir -p "$OUT_DIR"

ok=0
fail=0
while IFS= read -r line; do
    # strip comments + whitespace; skip blanks
    line="${line%%#*}"
    line="$(echo "$line" | xargs)"
    [ -z "$line" ] && continue

    fname=$(basename "$line")
    log "Fetching $line → $fname"
    if curl -fsSL --max-time 60 -o "$OUT_DIR/$fname.tmp" "$line"; then
        # Sanity: every ClamAV signature DB is at least a few hundred bytes.
        # Empty / tiny files almost always indicate an error page returned
        # as 200 (some sources do that on transient outages).
        sz=$(stat -c %s "$OUT_DIR/$fname.tmp" 2>/dev/null || echo 0)
        if [ "$sz" -lt 100 ]; then
            log "::warning::$fname is only $sz bytes — likely an error page, skipping"
            rm -f "$OUT_DIR/$fname.tmp"
            fail=$((fail + 1))
            continue
        fi
        mv "$OUT_DIR/$fname.tmp" "$OUT_DIR/$fname"
        ok=$((ok + 1))
    else
        log "::warning::failed to fetch $line"
        rm -f "$OUT_DIR/$fname.tmp"
        fail=$((fail + 1))
    fi
done < "$SOURCES_FILE"

log "Fetched $ok files, $fail failures"
[ "$ok" -gt 0 ] || { log "::error::no files fetched"; exit 1; }

# Manifest: per-file md5 + a composite hash over the whole set. The
# composite is what check-changed.sh will compare on subsequent runs
# to decide whether the CDN tree needs a re-publish.
log "Generating manifest"
{
    echo "# cape-rules clamav-extra mirror manifest"
    echo "# Generated: $(date -Iseconds)"
    echo "# Format: <md5> <basename>"
    echo ""
    cd "$OUT_DIR" && md5sum *.ndb *.ldb *.hdb *.ftm *.ign2 2>/dev/null | sort
} > "$OUT_DIR/manifest.txt"

# Composite hash: md5 of the sorted manifest (per-file md5 list).
# Changes when ANY upstream file's bytes change.
composite=$(grep -v '^#' "$OUT_DIR/manifest.txt" | grep -v '^$' | md5sum | awk '{print $1}')
echo "$composite" > "$OUT_DIR/manifest.md5"
log "Composite manifest md5: $composite"

# Drop the upstream-fingerprint marker so check-changed.sh on the next
# cron run can decide whether to skip. The fingerprint comes from the
# workflow's check step (HEAD-derived, doesn't re-download payloads);
# package.sh passes it in via env.
if [ -n "${UPSTREAM_FINGERPRINT:-}" ]; then
    printf '%s\n' "$UPSTREAM_FINGERPRINT" > "$OUT_DIR/upstream-fingerprint.txt"
    log "Stamped upstream-fingerprint.txt = $UPSTREAM_FINGERPRINT"
fi

log "Staged $ok files under $OUT_DIR:"
ls -la "$OUT_DIR" | head -25
TOTAL_KB=$(du -sk "$OUT_DIR" | awk '{print $1}')
log "Total mirror size: ${TOTAL_KB} KB"
log "mirror complete"
