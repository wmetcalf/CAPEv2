#!/usr/bin/env bash
# libvirt-build/package.sh — assemble cape-libvirt_<ver>_amd64.deb
# from a populated $DESTDIR.
#
# Mirrors suricata-build/package.sh: reads debian/cape-libvirt.install
# as the file-list manifest, reuses debian/cape-libvirt.postinst.
#
# Required env:
#   DESTDIR          DESTDIR populated by build.sh
#   PACKAGE_VERSION  e.g. "11.1.0+cape.1" — embedded in the deb's
#                    Package-Version field and the filename
#   OUT_DIR          where to write the .deb (default: ./dist)

set -euo pipefail

: "${DESTDIR:?DESTDIR required}"
: "${PACKAGE_VERSION:?PACKAGE_VERSION required}"
OUT_DIR="${OUT_DIR:-./dist}"
ARCH="${ARCH:-amd64}"

# Repo root (assumes script lives at <repo>/libvirt-build/).
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_MANIFEST="$REPO_ROOT/debian/cape-libvirt.install"
POSTINST_SRC="$REPO_ROOT/debian/cape-libvirt.postinst"

[ -f "$INSTALL_MANIFEST" ] || { echo "::error::missing $INSTALL_MANIFEST"; exit 1; }
[ -f "$POSTINST_SRC" ]    || { echo "::error::missing $POSTINST_SRC";    exit 1; }

mkdir -p "$OUT_DIR"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

log() { echo "[$(date -Iseconds)] [libvirt-package] $*"; }

# ---- copy only the files in the install manifest ------------------------

log "Staging files from $DESTDIR per manifest"
while read -r line; do
    line="${line%%#*}"  # strip comments
    line="${line## }"
    line="${line%% }"
    [ -z "$line" ] && continue

    cd "$DESTDIR"
    matches=( $line )  # globbing
    if [ ! -e "${matches[0]}" ] && [ "${matches[0]}" = "$line" ]; then
        echo "::warning::manifest entry '$line' has no matches; skipping"
        continue
    fi
    for m in "${matches[@]}"; do
        [ -e "$m" ] || continue
        target="$STAGE/$m"
        mkdir -p "$(dirname "$target")"
        cp -a "$m" "$target"
    done
    cd - >/dev/null
done < "$INSTALL_MANIFEST"

# ---- write DEBIAN control + postinst -------------------------------------

mkdir -p "$STAGE/DEBIAN"

INSTALLED_SIZE=$(du -sk "$STAGE" | awk '{print $1}')

# Runtime libs libvirtd and libvirt.so link against.  Hand-maintained
# because package.sh doesn't use dh_shlibdeps — missing entries surface
# only at runtime as "error while loading shared libraries: libX.so.N".
# Set against ldd output on a 24.04 build host. Bump alongside
# LIBVIRT_VERSION in build.sh if upstream adds new direct deps.
DEPENDS_LINE="libc6, libxml2, libgnutls30, libnl-3-200, libnl-route-3-200, libyajl2, libdevmapper1.02.1, libpciaccess0, libcurl4, libnuma1, libsasl2-2, libxml2-utils, libcap-ng0, libreadline8, libtirpc3t64, libosinfo-1.0-0, libjansson4, libapparmor1, dnsmasq-base, iptables, qemu-utils, polkitd, bridge-utils, ebtables, netcat-openbsd"

cat > "$STAGE/DEBIAN/control" <<EOF
Package: cape-libvirt
Version: $PACKAGE_VERSION
Section: admin
Priority: optional
Architecture: $ARCH
Depends: $DEPENDS_LINE
Conflicts: libvirt-daemon, libvirt-daemon-system, libvirt-clients, libvirt0, libvirt-glib-1.0-0
Replaces: libvirt-daemon, libvirt-daemon-system, libvirt-clients, libvirt0, libvirt-glib-1.0-0
Provides: libvirt-daemon, libvirt-daemon-system, libvirt-clients, libvirt0
Maintainer: CAPEv2 AWS Nested-Virt <noreply@example.invalid>
Installed-Size: $INSTALLED_SIZE
Description: libvirt $PACKAGE_VERSION built from source for CAPEv2
 libvirt 11.x built from upstream source. Replaces Ubuntu noble's
 libvirt 10.0.0-2ubuntu8.13. Required so CAPE's
 modules/auxiliary/QemuScreenshots host-side screendump module has
 the virStreamFinish API it depends on. virStreamFinish was added
 in libvirt 11.x; noble's 10.0.0 lacks it and QemuScreenshots floods
 cape.service with "function is not supported by the connection
 driver: virStreamFinish" errors every screenshot interval while
 silently disabling the in-VM screenshots_windows fallback (the two
 are mutually exclusive per the auxiliary.conf comment), so the
 host produces zero screenshots until QemuScreenshots is force-
 disabled.
 .
 Mirrors the libvirt-from-source step in legacy nestedvirt's
 kvm-qemu.sh::install_libvirt that built libvirt 11.1.0 the same
 way before the deb pipeline existed.
EOF

cp "$POSTINST_SRC" "$STAGE/DEBIAN/postinst"
chmod 755 "$STAGE/DEBIAN/postinst"

# ---- build the deb -------------------------------------------------------

OUT_DEB="$OUT_DIR/cape-libvirt_${PACKAGE_VERSION}_${ARCH}.deb"
log "Building $OUT_DEB"
dpkg-deb --root-owner-group --build "$STAGE" "$OUT_DEB"

ls -la "$OUT_DEB"
log "package complete"
