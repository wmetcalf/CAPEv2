#!/usr/bin/env bash
# libvirt-build/build.sh — compile libvirt from source into DESTDIR.
#
# Why we ship our own libvirt:
#   - Ubuntu 24.04 (noble) ships libvirt 10.0.0-2ubuntu8.13.
#   - libvirt 10.x is missing virStreamFinish (added in 11.x).
#   - CAPE's modules/auxiliary/QemuScreenshots uses virStreamFinish to
#     pull qcow2 screendumps from running VMs via the libvirt streaming
#     API. On noble it floods cape.service with:
#       libvirt: I/O Stream Utils error :
#       this function is not supported by the connection driver:
#       virStreamFinish
#     every screenshot interval (~0.5s) and short-circuits the in-VM
#     screenshots_windows path (the conf comment notes the two are
#     mutually exclusive). Net effect: zero screenshots captured.
#   - Legacy nestedvirt install (terraform/.../live-installer/kvm-qemu.sh
#     in the IaC repo) built libvirt 11.1.0 from source via meson+ninja
#     specifically to get virStreamFinish. The deb pipeline regressed
#     vs legacy when we settled for `apt install libvirt-daemon-system`.
#
# Output: $DESTDIR populated under /usr/{bin,sbin,lib,share} + /etc/libvirt/
# in the layout debian/cape-libvirt.install expects. Companion
# package.sh wraps DESTDIR into a cape-libvirt_<ver>_amd64.deb.
#
# Required env:
#   DESTDIR             where to install (e.g. /tmp/cape-libvirt-destdir)
# Optional env:
#   LIBVIRT_VERSION     upstream version (default: 11.1.0 — same as
#                       legacy kvm-qemu.sh)
#   LIBVIRT_SRC_URL     override URL
#   BUILD_JOBS          parallelism (default: nproc)
#
# Designed for `ubuntu:24.04` container with Build-Depends installed
# externally (the workflow handles that; this script just builds +
# installs).

set -euo pipefail

: "${DESTDIR:?DESTDIR required}"
LIBVIRT_VERSION="${LIBVIRT_VERSION:-11.1.0}"
LIBVIRT_SRC_URL="${LIBVIRT_SRC_URL:-https://download.libvirt.org/libvirt-${LIBVIRT_VERSION}.tar.xz}"
BUILD_JOBS="${BUILD_JOBS:-$(nproc)}"

log() { echo "[$(date -Iseconds)] [libvirt-build] $*"; }

mkdir -p "$DESTDIR"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# ---- fetch ---------------------------------------------------------------

cd "$WORK"
log "Fetching libvirt-${LIBVIRT_VERSION} from ${LIBVIRT_SRC_URL}"
curl -fsSL --retry 3 -o libvirt.tar.xz "$LIBVIRT_SRC_URL"

tar xf libvirt.tar.xz
cd "libvirt-${LIBVIRT_VERSION}"

# ---- configure -----------------------------------------------------------

# Meson options mirror legacy kvm-qemu.sh::install_libvirt:
#   - system=true            install system-wide (paths /etc, /var/run,
#                            /lib instead of /usr/local/* dev paths)
#   - driver_qemu=enabled    QEMU driver (the whole point — KVM/QEMU
#                            domains)
#   - driver_remote=enabled  libvirtd accepts remote API connections
#                            (libvirt-glib, virt-viewer over TCP/TLS)
#   - driver_libvirtd=enabled  build the libvirtd binary itself
#   - qemu_group=libvirt     qemu processes run as group libvirt
#                            (matches Ubuntu's apt package convention
#                            so existing groupadd entries still apply)
#   - qemu_user=root         qemu processes run as uid 0 (required for
#                            -device vfio-pci passthrough, raw socket
#                            tcpdump-style capture, KVM ioctl).
#                            Legacy choice; tighten later if/when we
#                            confirm cape doesn't need root qemu.
#   - secdriver_apparmor=enabled  apparmor security driver loaded; needed
#                            so the clamd apparmor exception in
#                            cape-host-config actually takes effect
#                            when libvirt confines qemu.
#   - apparmor_profiles=enabled   ship the per-domain apparmor profile
#                            templates under /etc/apparmor.d/abstractions/
#   - bash_completion=auto   ship virsh tab-completion if bash-completion
#                            is present at build time.
#   - libnl=enabled          netlink support for the in-tree VirNetwork
#                            implementation; libvirt-noble has this.
#
# Init system: libvirt's systemd unit support is `init_script` (set to
# `systemd` by default on Linux); the legacy `openrc=disabled` meson
# option was removed before 11.1.0 — meson rejects it as unknown.
log "Configuring libvirt ${LIBVIRT_VERSION} (meson)"
meson setup build \
    --prefix=/usr \
    --sysconfdir=/etc \
    --localstatedir=/var \
    --libdir=/usr/lib/x86_64-linux-gnu \
    -D system=true \
    -D driver_qemu=enabled \
    -D driver_remote=enabled \
    -D driver_libvirtd=enabled \
    -D qemu_group=libvirt \
    -D qemu_user=root \
    -D secdriver_apparmor=enabled \
    -D apparmor_profiles=enabled \
    -D bash_completion=auto

# ---- build + install to DESTDIR ------------------------------------------

log "Compiling with $BUILD_JOBS jobs"
ninja -C build -j"$BUILD_JOBS"

log "Installing into $DESTDIR"
DESTDIR="$DESTDIR" ninja -C build install

# ---- sanity --------------------------------------------------------------

log "Sanity checks"
test -x "$DESTDIR/usr/sbin/libvirtd"   || { echo "::error::libvirtd binary missing"; exit 1; }
test -x "$DESTDIR/usr/bin/virsh"       || { echo "::error::virsh missing"; exit 1; }
test -f "$DESTDIR/usr/lib/x86_64-linux-gnu/libvirt.so.0" \
    || test -L "$DESTDIR/usr/lib/x86_64-linux-gnu/libvirt.so.0" \
    || { echo "::error::libvirt.so.0 missing"; exit 1; }

# Confirm 11.x semver — the whole point is virStreamFinish + other
# 11.x-only APIs. If meson somehow built a 10.x tree (e.g. cached
# tarball mix-up), abort here.
LIBVIRT_BUILT_VERSION=$(
    LD_LIBRARY_PATH="$DESTDIR/usr/lib/x86_64-linux-gnu" \
    "$DESTDIR/usr/sbin/libvirtd" --version 2>/dev/null | awk '{print $NF}'
)
case "$LIBVIRT_BUILT_VERSION" in
    11.*) ;;
    *) echo "::error::built libvirtd reports version '$LIBVIRT_BUILT_VERSION' — expected 11.x"; exit 1 ;;
esac
log "libvirtd version: $LIBVIRT_BUILT_VERSION"

log "Build complete: libvirt $LIBVIRT_VERSION installed under $DESTDIR"
du -sh "$DESTDIR"
