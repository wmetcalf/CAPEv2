#!/usr/bin/env bash
# cape-zircolite-build/package.sh — assemble cape-zircolite.deb.
#
# Ships the Zircolite sigma-detection engine to /opt/zircolite, owned by
# cape:cape (CAPE's community/modules/processing/sigma.py runs
# `sys.executable /opt/zircolite/zircolite.py ...` per analysis).
#
# Engine only — rule packs come from cape-sigma-rules, venv deps from
# cape-core's pyproject.  cape-zircolite is in the OS-affecting deb set
# (it's code, not threat content) so a bump triggers an AMI rebake, not
# the cape-threat-update.timer.
#
# Inputs:
#   PACKAGE_VERSION   required (Zircolite release tag, leading-v stripped)
#   WORK_DIR          default ./.work (where fetch.sh staged ./zircolite)
#   OUT_DIR           default dist
#   ARCH              default all (pure-python engine)

set -euo pipefail
log() { echo "[$(date -Iseconds)] [zircolite-pkg] $*"; }

: "${PACKAGE_VERSION:?PACKAGE_VERSION required}"
WORK_DIR="${WORK_DIR:-./.work}"
OUT_DIR="${OUT_DIR:-dist}"
ARCH="${ARCH:-all}"

SRC="$WORK_DIR/zircolite"
[ -f "$SRC/zircolite.py" ] || { log "::error::$SRC/zircolite.py not found — run fetch.sh first"; exit 1; }

mkdir -p "$OUT_DIR"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

install -d -m 0755 "$STAGE/opt/zircolite"
cp -a "$SRC/." "$STAGE/opt/zircolite/"
# Files ship root-owned in the deb; postinst chowns to cape:cape at
# install time (the cape user may not exist at dpkg-deb build time).
find "$STAGE/opt/zircolite" -type d -exec chmod 0755 {} \;
find "$STAGE/opt/zircolite" -type f -exec chmod 0644 {} \;
chmod 0755 "$STAGE/opt/zircolite/zircolite.py"

mkdir -p "$STAGE/DEBIAN"
INSTALLED_SIZE=$(du -sk "$STAGE" | awk '{print $1}')
cat > "$STAGE/DEBIAN/control" <<EOF
Package: cape-zircolite
Version: ${PACKAGE_VERSION}
Section: misc
Priority: optional
Architecture: ${ARCH}
Depends: cape-core
Maintainer: CAPEv2 AWS Nested-Virt <noreply@example.invalid>
Installed-Size: ${INSTALLED_SIZE}
Description: Zircolite sigma-detection engine for CAPE sandbox
 The Zircolite engine (github.com/wagga40/Zircolite) installed to
 /opt/zircolite.  CAPE's sigma processing module
 (community/modules/processing/sigma.py, shipped by cape-signatures)
 runs zircolite.py over the guest's collected EVTX + Sysmon logs to
 produce Sigma detections in the analysis report.
 .
 Engine only: the Sigma rule packs ship via cape-sigma-rules
 (/opt/CAPEv2/data/sigma/rules_*.json) and the Python runtime deps
 (pysigma + backends) via cape-core's venv.  Enable the integration
 with [sigma] enabled=yes in processing.conf (cape-host-config does
 this on managed hosts).
 .
 Built from Zircolite release ${PACKAGE_VERSION}.
EOF

cat > "$STAGE/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e
case "$1" in
    configure)
        # sigma.py runs zircolite as the cape user; it writes a small
        # state/cache under its own dir, so cape needs ownership.
        if id cape >/dev/null 2>&1; then
            chown -R cape:cape /opt/zircolite 2>/dev/null || true
        fi
        ;;
esac
exit 0
POSTINST
chmod 0755 "$STAGE/DEBIAN/postinst"

OUT_DEB="$OUT_DIR/cape-zircolite_${PACKAGE_VERSION}_${ARCH}.deb"
log "Building $OUT_DEB"
dpkg-deb --root-owner-group --build "$STAGE" "$OUT_DEB"
ls -la "$OUT_DEB"
log "package complete"
