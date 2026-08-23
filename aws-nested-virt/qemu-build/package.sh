#!/usr/bin/env bash
# qemu-build/package.sh — assemble cape-qemu_<ver>_amd64.deb from a
# populated $DESTDIR.
#
# Mirrors suricata-build/package.sh: reuses debian/cape-qemu.{install,postinst}
# as the file-list manifest + postinst, drives dpkg-deb --build directly
# (skipping debhelper machinery).
#
# Required env:
#   DESTDIR             DESTDIR populated by build.sh
#   PACKAGE_VERSION     e.g. "9.2.4+cape.1"
#   OUT_DIR             where to write the .deb (default: ./dist)

set -euo pipefail

: "${DESTDIR:?DESTDIR required}"
: "${PACKAGE_VERSION:?PACKAGE_VERSION required}"
OUT_DIR="${OUT_DIR:-./dist}"
ARCH="${ARCH:-amd64}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_MANIFEST="$REPO_ROOT/debian/cape-qemu.install"
POSTINST_SRC="$REPO_ROOT/debian/cape-qemu.postinst"

[ -f "$INSTALL_MANIFEST" ] || { echo "::error::missing $INSTALL_MANIFEST"; exit 1; }
[ -f "$POSTINST_SRC" ]    || { echo "::error::missing $POSTINST_SRC";    exit 1; }

mkdir -p "$OUT_DIR"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

log() { echo "[$(date -Iseconds)] [qemu-package] $*"; }

# ---- copy only the files in the install manifest ------------------------

log "Staging files from $DESTDIR per manifest"
while IFS= read -r line; do
    line="${line%%#*}"
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

# cape-qemu provides Ubuntu's qemu-system-x86 / qemu-utils / qemu-kvm /
# seabios via Conflicts: + Provides: so apt's dependency resolver sees
# our fork as a drop-in replacement when cape-core's
# `Depends: ... | cape-qemu` alternation prefers it.
#
# Depends is hand-maintained because this deb is hand-rolled (dpkg-deb
# directly, no debhelper / dh_shlibdeps).  Every shared lib qemu links
# against has to be enumerated here; missing entries surface only at
# runtime when the binary first dlopen()s a lib that isn't installed.
# libaio1 + liburing2 are required because qemu-build/build.sh passes
# --enable-linux-aio (mandatory for libvirt <driver aio="native"/> on
# clone disks) and --enable-linux-io-uring (future-proofing).  ami-bake
# Phase 2 caught the liburing miss when qemu-img refused to clone with
# "error while loading shared libraries: liburing.so.2: cannot open
# shared object file"; libaio1 added preemptively for the same reason.
cat > "$STAGE/DEBIAN/control" <<EOF
Package: cape-qemu
Version: $PACKAGE_VERSION
Section: misc
Priority: optional
Architecture: $ARCH
Depends: libc6, libglib2.0-0, libpixman-1-0, libcap-ng0, libslirp0, libusb-1.0-0, libnettle8, libgnutls30, libssh-4, libgio-2.0-0, libcurl3-gnutls, libnuma1, libsasl2-2, libsdl2-2.0-0, libsndio7.0, libudev1, libusbredirparser1, libsnappy1v5, libnbd0, libaio1t64, liburing2
Conflicts: qemu-system-x86, qemu-utils, qemu-kvm, seabios
Replaces: qemu-system-x86, qemu-utils, qemu-kvm, seabios
Provides: qemu-system-x86, qemu-utils, qemu-kvm, seabios
Maintainer: CAPEv2 AWS Nested-Virt <noreply@example.invalid>
Installed-Size: $INSTALLED_SIZE
Description: Patched QEMU + SeaBIOS for CAPE sandbox use
 QEMU and SeaBIOS built from source with anti-VM-detection patches:
 fake CPU brand strings, KVMKVMKVM hypervisor signature replaced with
 GenuineIntel, QEMU device strings replaced with consumer-hardware
 names, ACPI Bochs/BXPC identifiers neutralized. Replaces the Ubuntu
 archive qemu-system-x86 + qemu-utils + qemu-kvm + seabios.
EOF

cp "$POSTINST_SRC" "$STAGE/DEBIAN/postinst"
chmod 755 "$STAGE/DEBIAN/postinst"

# ---- build the deb -------------------------------------------------------

OUT_DEB="$OUT_DIR/cape-qemu_${PACKAGE_VERSION}_${ARCH}.deb"
log "Building $OUT_DEB"
dpkg-deb --root-owner-group --build "$STAGE" "$OUT_DEB"

ls -la "$OUT_DEB"
log "package complete"
