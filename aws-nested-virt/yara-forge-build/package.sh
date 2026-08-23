#!/usr/bin/env bash
# yara-forge-build/package.sh — assemble cape-yara-forge_<ver>_all.deb
# from the staging dir fetch.sh populated.
#
# Ships:
#   /opt/CAPEv2/data/yara/binaries/yara-forge-<flavor>.yar
#
# Why /opt/CAPEv2/data/yara/binaries/? CAPE's YARA scanner auto-loads
# every .yar under the binaries/ subdir for binary samples; dropping
# yara-forge there is the lowest-friction integration (no CAPE config
# change required).
#
# Inputs:
#   WORK_DIR            staging dir from fetch.sh
#   PACKAGE_VERSION     deb Version field (e.g. "20260507" — yara-forge tag)
#   YARA_FORGE_FLAVOR   matches fetch.sh; default "extended"
#   OUT_DIR             (optional, default ./dist)

set -euo pipefail

: "${WORK_DIR:?WORK_DIR required}"
: "${PACKAGE_VERSION:?PACKAGE_VERSION required}"
FLAVOR="${YARA_FORGE_FLAVOR:-extended}"
OUT_DIR="${OUT_DIR:-./dist}"
ARCH=all

log() { echo "[$(date -Iseconds)] [yara-forge-package] $*"; }

YAR_FILE="$WORK_DIR/rules/yara-rules-${FLAVOR}.yar"
[ -f "$YAR_FILE" ] || { echo "::error::missing $YAR_FILE — run fetch.sh first"; exit 1; }

mkdir -p "$OUT_DIR"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

# ---- stage the rule file -------------------------------------------------
INSTALL_DIR="$STAGE/opt/CAPEv2/data/yara/binaries"
install -d -m 0755 "$INSTALL_DIR"
install -m 0644 "$YAR_FILE" "$INSTALL_DIR/yara-forge-${FLAVOR}.yar"

# Stats file for operator traceability — not loaded by CAPE.
STATS_FILE="$WORK_DIR/rules/statistics-yara-rules-${FLAVOR}.txt"
if [ -f "$STATS_FILE" ]; then
    DOC_DIR="$STAGE/usr/share/doc/cape-yara-forge"
    install -d -m 0755 "$DOC_DIR"
    install -m 0644 "$STATS_FILE" "$DOC_DIR/statistics.txt"
fi

# ---- DEBIAN control ------------------------------------------------------
mkdir -p "$STAGE/DEBIAN"
INSTALLED_SIZE=$(du -sk "$STAGE" | awk '{print $1}')

cat > "$STAGE/DEBIAN/control" <<EOF
Package: cape-yara-forge
Version: ${PACKAGE_VERSION}
Section: misc
Priority: optional
Architecture: ${ARCH}
Depends: cape-core
Maintainer: CAPEv2 AWS Nested-Virt <noreply@example.invalid>
Installed-Size: ${INSTALLED_SIZE}
Description: YARA Forge ${FLAVOR} ruleset for CAPE sandbox
 YARA rules from github.com/YARAHQ/yara-forge (${FLAVOR} flavor),
 packaged for managed apt delivery. Drops at
 /opt/CAPEv2/data/yara/binaries/yara-forge-${FLAVOR}.yar where CAPE's
 binary-category YARA scanner picks them up on next service restart.
 .
 Built from YARA Forge release ${PACKAGE_VERSION}.
EOF

# Activate cape-host-runtime's `cape-processor-reload` dpkg trigger on
# install/upgrade so cape.service / cape-processor.service / cape-web.
# service restart and pick up the new rules without operator action.
#
# Trigger (vs running systemctl directly in postinst) so multi-deb apt
# transactions — e.g. the cape-threat-update.timer that may upgrade
# cape-yara-forge + cape-sigma-rules + cape-community in the same
# `apt install --only-upgrade` run — coalesce into a single restart at
# end-of-transaction rather than 3 sequential restarts.  The interest
# holder + restart logic live in cape-host-runtime's DEBIAN/postinst.
cat > "$STAGE/DEBIAN/triggers" <<'TRIGGERS'
activate cape-processor-reload
TRIGGERS

# ---- build deb -----------------------------------------------------------
OUT_DEB="$OUT_DIR/cape-yara-forge_${PACKAGE_VERSION}_${ARCH}.deb"
log "Building $OUT_DEB"
dpkg-deb --root-owner-group --build "$STAGE" "$OUT_DEB"
ls -la "$OUT_DEB"
log "package complete"
