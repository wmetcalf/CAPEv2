#!/usr/bin/env bash
# mongodb-build/package.sh — assemble cape-mongodb.deb.
#
# cape-mongodb is a thin wrapper around mongodb-org-server. It exists so
# that:
#
#   1. CAPE on a deb-baked AMI has a guaranteed mongod available without
#      operators having to add the upstream mongodb-8.0 apt source by
#      hand. The packer provisioner (packer/provisioners/00-install-cape.sh)
#      sets up the source once at bake time so `apt-get install
#      cape-mongodb` resolves mongodb-org-server transitively.
#
#   2. cape-mongodb owns a tuned systemd unit (NUMA interleave,
#      tcmallocReleaseRate, GLIBC_TUNABLES) that mirrors what the legacy
#      nestedvirt install-nestedvirt.sh::install_mongodb function used
#      to write at first boot. The deb form makes that unit reproducible
#      across rebakes instead of being a free-floating file the legacy
#      bootstrap regenerated each time.
#
#   3. The deb's postinst masks /lib/systemd/system/mongod.service from
#      mongodb-org-server (with a symlink-to-/dev/null in /etc/systemd/
#      system) so the upstream and cape-tuned units don't race for port
#      27017.
#
# Inputs:
#   PACKAGE_VERSION   deb Version field
#   OUT_DIR           (optional, default ./dist)

set -euo pipefail

: "${PACKAGE_VERSION:?PACKAGE_VERSION required}"
OUT_DIR="${OUT_DIR:-./dist}"
ARCH=all

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FILES_DIR="$REPO_ROOT/mongodb-build/files"

log() { echo "[$(date -Iseconds)] [mongodb-package] $*"; }

[ -d "$FILES_DIR" ] || { log "::error::missing $FILES_DIR"; exit 1; }

mkdir -p "$OUT_DIR"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

log "Staging files from $FILES_DIR"
cp -a "$FILES_DIR/lib" "$STAGE/lib"
find "$STAGE/lib" -type f -exec chmod 0644 {} \;
find "$STAGE/lib" -type d -exec chmod 0755 {} \;

# ---- DEBIAN control ------------------------------------------------------
mkdir -p "$STAGE/DEBIAN"
INSTALLED_SIZE=$(du -sk "$STAGE" | awk '{print $1}')

cat > "$STAGE/DEBIAN/control" <<EOF
Package: cape-mongodb
Version: ${PACKAGE_VERSION}
Section: database
Priority: optional
Architecture: ${ARCH}
Depends: mongodb-org-server, mongodb-org-shell, numactl
Conflicts: mongodb-org (<< 0~)
Maintainer: CAPEv2 AWS Nested-Virt <noreply@example.invalid>
Installed-Size: ${INSTALLED_SIZE}
Description: CAPE-tuned MongoDB unit for the sandbox host
 Ships a tuned /lib/systemd/system/cape-mongodb.service that wraps
 mongodb-org-server (8.0) with NUMA interleave + tcmallocReleaseRate=5.0
 + GLIBC_TUNABLES=glibc.pthread.rseq=0 — the same knobs the legacy
 nestedvirt install-nestedvirt.sh used to plant by hand at first boot.
 .
 Postinst masks the stock /lib/systemd/system/mongod.service so the two
 units don't race for port 27017, then enables cape-mongodb.service
 and bootstraps /data/db + /data/configdb (mongodb:mongodb).
EOF

# Postinst: mask upstream mongod.service, enable cape-mongodb.service,
# bootstrap data dirs.
cat > "$STAGE/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e

case "$1" in
    configure)
        # mongodb-org-server expects /data/db to exist; legacy install-
        # nestedvirt used /data/db + /data/configdb instead of the upstream
        # default /var/lib/mongodb so existing CAPE deployments aren't
        # silently migrated when this deb lands.  Match that layout.
        install -d -m 0755 /data/db /data/configdb
        chown -R mongodb:mongodb /data 2>/dev/null || true

        # Mask the upstream mongod.service so cape-mongodb wins port
        # 27017.  Masking via /etc/systemd/system symlink is persistent
        # across mongodb-org-server upgrades (the upstream package can't
        # touch files in /etc).
        if [ ! -L /etc/systemd/system/mongod.service ]; then
            ln -sf /dev/null /etc/systemd/system/mongod.service
        fi

        systemctl daemon-reload || true

        # Stop the upstream unit if mongodb-org-server's own postinst
        # auto-started it during apt install.  The mask above prevents
        # it from coming back on reboot, but we still need to free
        # 27017 before enabling cape-mongodb.
        systemctl stop mongod.service 2>/dev/null || true

        systemctl enable cape-mongodb.service 2>/dev/null || true
        systemctl start cape-mongodb.service 2>/dev/null || \
            echo "cape-mongodb.service start failed — check journalctl -u cape-mongodb" >&2
        ;;
esac
exit 0
POSTINST
chmod 0755 "$STAGE/DEBIAN/postinst"

cat > "$STAGE/DEBIAN/postrm" <<'POSTRM'
#!/bin/sh
set -e

case "$1" in
    remove|purge)
        systemctl disable --now cape-mongodb.service 2>/dev/null || true

        # Unmask the upstream unit so the system has a path back to a
        # working mongod if cape-mongodb is removed without a
        # replacement.
        if [ -L /etc/systemd/system/mongod.service ] && \
           [ "$(readlink /etc/systemd/system/mongod.service)" = "/dev/null" ]; then
            rm -f /etc/systemd/system/mongod.service
        fi
        systemctl daemon-reload || true
        ;;
esac
exit 0
POSTRM
chmod 0755 "$STAGE/DEBIAN/postrm"

OUT_DEB="$OUT_DIR/cape-mongodb_${PACKAGE_VERSION}_${ARCH}.deb"
log "Building $OUT_DEB"
dpkg-deb --root-owner-group --build "$STAGE" "$OUT_DEB"
ls -la "$OUT_DEB"
log "package complete"
