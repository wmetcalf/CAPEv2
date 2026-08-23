#!/usr/bin/env bash
# suricata-rules-build/package.sh — emit the CDN tree from
# $WORK_DIR/compiled/.
#
# CDN-ONLY by design. Suricata rules update multiple times per day and
# don't belong in apt-managed deb churn. The native tool for that
# cadence is suricata-update; we serve our content through its
# protocol (.tar.gz + .tar.gz.md5 sidecar + index.yaml) and let the
# operator host pull on whatever schedule they prefer.
#
# Output tree:
#   $OUT_DIR/cdn/suricata/cape/rules/
#     cape-rules.tar.gz       — bundled ET Open + cape-project
#     cape-rules.tar.gz.md5   — change-detection sidecar
#     index.yaml                      — suricata-update sources manifest
#
# apt-publish.yml fans this artifact in and uploads its files as
# bare-named assets to the threat-content GitHub Release. Operators
# point suricata-update at the resulting release-asset URL.
#
# Inputs:
#   WORK_DIR             from fetch.sh + compile.sh
#   PACKAGE_VERSION      stamp for the bundle (deb-style version string —
#                        used in index.yaml's `version:` field for audit)
#   THREAT_CONTENT_URL   required threat-content release base URL; the
#                        index.yaml `url:` points at the bare tarball asset
#                        served flat under it
#   OUT_DIR              default ./dist

set -euo pipefail

: "${WORK_DIR:?WORK_DIR required}"
: "${PACKAGE_VERSION:?PACKAGE_VERSION required}"
: "${THREAT_CONTENT_URL:?THREAT_CONTENT_URL required (for example, https://github.com/OWNER/REPO/releases/download/threat-content)}"
THREAT_CONTENT_URL="${THREAT_CONTENT_URL%/}"
OUT_DIR="${OUT_DIR:-./dist}"
COMPILED="$WORK_DIR/compiled"

log() { echo "[$(date -Iseconds)] [suricata-rules-package] $*"; }

[ -f "$COMPILED/suricata.rules" ] || { log "::error::missing $COMPILED/suricata.rules — run compile.sh first"; exit 1; }

CDN_DIR="$OUT_DIR/cdn/suricata/cape/rules"
mkdir -p "$CDN_DIR"

# Drop the upstream ET md5 marker so check-changed.sh on the NEXT run
# can compare against this run's upstream and decide whether to skip.
# The marker is the unprocessed upstream md5, not the post-merge
# tarball md5 — suricata-update output may not be byte-stable so
# tracking the input is more reliable.
if [ -n "${ET_OPEN_MD5:-}" ]; then
    printf '%s\n' "$ET_OPEN_MD5" > "$CDN_DIR/upstream-et-md5.txt"
    log "Stamped upstream-et-md5.txt = $ET_OPEN_MD5"
elif [ -f "$WORK_DIR/et-open.md5" ]; then
    cp "$WORK_DIR/et-open.md5" "$CDN_DIR/upstream-et-md5.txt"
    log "Stamped upstream-et-md5.txt from $WORK_DIR/et-open.md5"
fi

# Bundle compiled tree as a tarball. Layout inside the tarball mirrors
# /etc/suricata/rules/ so an operator can extract on top of an existing
# Suricata install without surprises (suricata-update handles this
# automatically via its index.yaml + extract logic).
CDN_STAGE=$(mktemp -d)
trap 'rm -rf "$CDN_STAGE"' EXIT
mkdir -p "$CDN_STAGE/rules"
install -m 0644 "$COMPILED/suricata.rules" "$CDN_STAGE/rules/"
for cfg in classification.config reference.config; do
    [ -f "$COMPILED/$cfg" ] && install -m 0644 "$COMPILED/$cfg" "$CDN_STAGE/rules/"
done
[ -f "$COMPILED/build-meta.txt" ] && install -m 0644 "$COMPILED/build-meta.txt" "$CDN_STAGE/"

TARBALL="$CDN_DIR/cape-rules.tar.gz"
log "Building $TARBALL"
tar -C "$CDN_STAGE" -czf "$TARBALL" .

# suricata-update protocol: hex md5 in a sidecar.  Client fetches .md5
# first; only re-downloads the .tar.gz if it differs from cached value.
md5sum "$TARBALL" | awk '{print $1}' > "${TARBALL}.md5"
log "$(basename "$TARBALL").md5 = $(cat "${TARBALL}.md5")"

# suricata-update sources index (the index.yaml shape suricata-update
# expects under `update-sources`).
INDEX="$CDN_DIR/index.yaml"
cat > "$INDEX" <<EOF
version: 1.0
sources:
  cape/rules:
    summary: "Curated Suricata rules for CAPE sandbox"
    description: |
      Emerging Threats Open + cape-project Suricata rules, packaged
      and served via the apt CDN. Bundle is rebuilt against the
      latest ET Open .tar.gz.md5 cadence (multiple updates/day on the
      ET side); cape-project additions land separately as commits to
      suricata-rules/sources/cape/ in the sandbox-code repo.

      Delivered via the suricata-update protocol (.tar.gz + .tar.gz.md5
      sidecar) rather than apt-managed debs because Suricata rule
      churn doesn't suit deb cadence — the native tool is the right
      consumer.
    license: "GPL-2.0 (ET Open) + cape-project (additions)"
    url: ${THREAT_CONTENT_URL}/cape-rules.tar.gz
    min-version: 5.0.0
    vendor: CAPEv2 AWS Nested-Virt
    version: ${PACKAGE_VERSION}
EOF
log "Wrote index.yaml"

log "CDN artifacts under $CDN_DIR:"
ls -la "$CDN_DIR"
log "package complete"
