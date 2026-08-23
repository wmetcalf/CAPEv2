#!/usr/bin/env bash
# suricata-rules-build/fetch.sh — pull Emerging Threats Open + verify
# the upstream tarball md5, extract into $WORK_DIR/et-open/.
#
# Uses the suricata-update protocol convention: <tarball>.md5 hosted
# alongside <tarball>, content is just the md5 hex string. Verifying
# this gives us the same change-detection guarantee suricata-update
# uses (we re-build only when ET's tarball md5 changes).
#
# Inputs:
#   WORK_DIR              required, staging dir
#   SURICATA_VERSION      default 7.0.13 — must match cape-suricata
#   ET_OPEN_BASE_URL      default https://rules.emergingthreats.net/open
#
# Outputs:
#   $WORK_DIR/dl/emerging.rules.tar.gz
#   $WORK_DIR/dl/emerging.rules.tar.gz.md5
#   $WORK_DIR/et-open/rules/*.rules + classification.config + reference.config
#   $WORK_DIR/et-open.md5    — the upstream md5 hex (lets package.sh embed for traceability)

set -euo pipefail

: "${WORK_DIR:?WORK_DIR required}"
SURICATA_VERSION="${SURICATA_VERSION:-7.0.13}"
ET_OPEN_BASE_URL="${ET_OPEN_BASE_URL:-https://rules.emergingthreats.net/open}"

log() { echo "[$(date -Iseconds)] [suricata-rules-fetch] $*"; }

mkdir -p "$WORK_DIR/dl" "$WORK_DIR/et-open"
BASE_URL="${ET_OPEN_BASE_URL}/suricata-${SURICATA_VERSION}"
TARBALL=emerging.rules.tar.gz
MD5=${TARBALL}.md5

log "Downloading $BASE_URL/$TARBALL"
curl -fsSL -o "$WORK_DIR/dl/$TARBALL" "$BASE_URL/$TARBALL"
log "Downloading $BASE_URL/$MD5"
curl -fsSL -o "$WORK_DIR/dl/$MD5" "$BASE_URL/$MD5"

expected_md5=$(awk '{print $1}' "$WORK_DIR/dl/$MD5" | head -1)
actual_md5=$(md5sum "$WORK_DIR/dl/$TARBALL" | awk '{print $1}')
[ "$expected_md5" = "$actual_md5" ] || {
    log "::error::ET Open tarball md5 mismatch (expected=$expected_md5 actual=$actual_md5)"
    exit 1
}
log "md5 verified: $actual_md5"
echo "$actual_md5" > "$WORK_DIR/et-open.md5"

log "Extracting tarball"
tar -xzf "$WORK_DIR/dl/$TARBALL" -C "$WORK_DIR/et-open"

# ET Open tarball layout: rules/*.rules + rules/classification.config etc.
ET_RULES_DIR="$WORK_DIR/et-open/rules"
[ -d "$ET_RULES_DIR" ] || { log "::error::no rules/ subdir in extracted tarball"; ls -la "$WORK_DIR/et-open"; exit 1; }

RULE_FILE_COUNT=$(find "$ET_RULES_DIR" -name '*.rules' -type f | wc -l | tr -d ' ')
log "Found $RULE_FILE_COUNT ET Open .rules files"
[ "$RULE_FILE_COUNT" -ge 30 ] || { log "::error::implausibly few rule files ($RULE_FILE_COUNT)"; exit 1; }

log "fetch complete"
