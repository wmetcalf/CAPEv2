#!/usr/bin/env bash
# sigma-rules-build/package.sh — assemble cape-sigma-rules.deb
# from the rule packs fetch.sh staged.
#
# Ships:
#   /opt/CAPEv2/data/sigma/rules_*.json
#
# CAPE's sigma processing module discovers rule packs under
# data/sigma/. The on-host cape-sigma-update.timer (which used to
# fetch + convert at runtime via Zircolite) becomes redundant once
# this deb is installed; postinst masks it. Operators who prefer
# the runtime-refresh path can manually `systemctl unmask` it.
#
# Inputs:
#   WORK_DIR             from fetch.sh
#   PACKAGE_VERSION      deb Version (typically Zircolite release tag)
#   OUT_DIR              default ./dist

set -euo pipefail

: "${WORK_DIR:?WORK_DIR required}"
: "${PACKAGE_VERSION:?PACKAGE_VERSION required}"
OUT_DIR="${OUT_DIR:-./dist}"
ARCH=all

log() { echo "[$(date -Iseconds)] [sigma-rules-package] $*"; }

[ -d "$WORK_DIR/rules" ] || { log "::error::missing $WORK_DIR/rules — run fetch.sh"; exit 1; }
ls "$WORK_DIR/rules"/rules_*.json >/dev/null 2>&1 || { log "::error::no rule-pack files in $WORK_DIR/rules"; exit 1; }

mkdir -p "$OUT_DIR"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

# Stage rule packs
INSTALL_DIR="$STAGE/opt/CAPEv2/data/sigma"
install -d -m 0755 "$INSTALL_DIR"
for f in "$WORK_DIR/rules"/rules_*.json; do
    install -m 0644 "$f" "$INSTALL_DIR/"
done
pack_count=$(ls "$INSTALL_DIR"/rules_*.json | wc -l | tr -d ' ')
log "Staged $pack_count rule packs under $INSTALL_DIR"

# DEBIAN control + postinst
mkdir -p "$STAGE/DEBIAN"
INSTALLED_SIZE=$(du -sk "$STAGE" | awk '{print $1}')
cat > "$STAGE/DEBIAN/control" <<EOF
Package: cape-sigma-rules
Version: ${PACKAGE_VERSION}
Section: misc
Priority: optional
Architecture: ${ARCH}
Depends: cape-core
Maintainer: CAPEv2 AWS Nested-Virt <noreply@example.invalid>
Installed-Size: ${INSTALLED_SIZE}
Description: Sigma (Zircolite) rule packs for CAPE sandbox
 Pre-converted Sigma rule packs from Zircolite
 (github.com/wagga40/Zircolite), packaged for managed apt
 delivery. Ships to /opt/CAPEv2/data/sigma/rules_*.json where CAPE's
 sigma processing module discovers them on next service restart.
 .
 Pinned to Zircolite release ${PACKAGE_VERSION}. Replaces the
 on-host cape-sigma-update.timer/.service which used to invoke
 \`zircolite.py --update-rules\` at runtime (which reached GitHub
 from the analysis host — undesirable in enterprise deployments).
EOF

# cape-processor / cape / cape-web reload via cape-host-runtime's
# dpkg trigger.  See cape-yara-forge for the rationale (coalesce
# multi-deb transaction restarts to one).  Postinst still handles
# the cape-sigma-update.timer mask (idempotent, unrelated to reload).
cat > "$STAGE/DEBIAN/triggers" <<'TRIGGERS'
activate cape-processor-reload
TRIGGERS

cat > "$STAGE/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e
case "$1" in
    configure)
        # Mask the runtime updater — its job is now done at build
        # time. Operators who want runtime refresh can unmask manually.
        # Mask is idempotent + safe even if the unit doesn't exist
        # (older AMIs).
        systemctl mask cape-sigma-update.timer    2>/dev/null || true
        systemctl mask cape-sigma-update.service  2>/dev/null || true
        ;;
esac
exit 0
POSTINST
chmod 0755 "$STAGE/DEBIAN/postinst"

OUT_DEB="$OUT_DIR/cape-sigma-rules_${PACKAGE_VERSION}_${ARCH}.deb"
log "Building $OUT_DEB"
dpkg-deb --root-owner-group --build "$STAGE" "$OUT_DEB"
ls -la "$OUT_DEB"
log "package complete"
