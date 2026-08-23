#!/usr/bin/env bash
# qemu-build/build.sh — compile patched QEMU + SeaBIOS into DESTDIR.
#
# CAPEv2's analysis VMs need a QEMU build with anti-VM-detection patches
# applied at compile time (CPU brand strings, hypervisor signature,
# device descriptors, ACPI table identifiers, etc.). Upstream Ubuntu's
# qemu-system-x86 doesn't carry these patches — common sandbox-aware
# malware would trivially detect the host as a VM and refuse to detonate.
#
# Output: $DESTDIR populated under /usr/{bin,lib,share}, /etc/ matching
# the layout debian/cape-qemu.install expects. Companion package.sh
# wraps DESTDIR into cape-qemu_<ver>_amd64.deb.
#
# Required env:
#   DESTDIR              where to install
# Optional env:
#   QEMU_VERSION         upstream version (default: 9.2.4)
#   QEMU_SRC_URL         override download URL
#   BUILD_JOBS           parallelism (default: nproc)

set -euo pipefail

: "${DESTDIR:?DESTDIR required}"
QEMU_VERSION="${QEMU_VERSION:-9.2.4}"
QEMU_SRC_URL="${QEMU_SRC_URL:-https://download.qemu.org/qemu-${QEMU_VERSION}.tar.xz}"
BUILD_JOBS="${BUILD_JOBS:-$(nproc)}"

log() { echo "[$(date -Iseconds)] [qemu-build] $*"; }

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

mkdir -p "$DESTDIR"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# ---- fetch ---------------------------------------------------------------

cd "$WORK"
log "Fetching qemu-${QEMU_VERSION} from ${QEMU_SRC_URL}"
curl -fsSL --retry 3 -o qemu.tar.xz "$QEMU_SRC_URL"

log "Extracting"
tar xf qemu.tar.xz
QEMU_SRC="$WORK/qemu-${QEMU_VERSION}"
[ -d "$QEMU_SRC" ] || { echo "::error::expected $QEMU_SRC after extract"; exit 1; }

# ---- patch ---------------------------------------------------------------

# shellcheck source=patches.sh
source "$REPO_ROOT/qemu-build/patches.sh"

apply_qemu_patches "$QEMU_SRC"

# Bundled SeaBIOS lives under qemu/roms/seabios/. Patch its strings too
# so the BIOS info Windows reports via WMI doesn't out the host.
if [ -d "$QEMU_SRC/roms/seabios" ]; then
    apply_seabios_patches "$QEMU_SRC/roms/seabios"
fi

# ---- pre-build: BIOS roms (in qemu/roms/) ---------------------------------

# QEMU's roms/ Makefile builds bios.bin / vgabios*.bin from the bundled
# SeaBIOS. CAPE expects these at /usr/share/qemu/. Strip the Xen targets
# (we don't ship Xen) and force python3.
if [ -d "$QEMU_SRC/roms" ]; then
    cd "$QEMU_SRC/roms"
    [ -f config.seabios-microvm ] && sed -i 's/CONFIG_XEN=y/CONFIG_XEN=n/g' config.seabios-microvm || true
    [ -f config.seabios-128k ]    && sed -i 's/CONFIG_XEN=y/CONFIG_XEN=n/g' config.seabios-128k    || true
    [ -f seabios/Makefile ]       && sed -i 's/PYTHON=python/PYTHON=python3/g' seabios/Makefile    || true

    log "Building bios.bin"
    make -j"$BUILD_JOBS" bios

    log "Building vgabios"
    make -j"$BUILD_JOBS" vgabios
fi

# ---- configure + build qemu ----------------------------------------------

cd "$QEMU_SRC"

log "Configuring QEMU"
# Target list scoped to x86 only — we don't analyze ARM/MIPS samples
# (yet). Keeps the build to ~25 min instead of ~60.
#
# --enable-linux-aio + --enable-linux-io-uring:
#   libvirt clone domain XML (cape/scripts/clone-win11-vms.sh templates)
#   declares <driver aio="native"/> on each clone's qcow2 disk.  Without
#   --enable-linux-aio at configure time, qemu refuses every clone start:
#     "aio=native was specified, but is not supported in this build"
#   All 24 clones landed in `shut off` after the deb-baked AMI deploy
#   because of this — the failure didn't surface in cape-deb-e2e Layer 4
#   (host smoke) because L4 doesn't run virsh start.  Both flags are
#   explicit (not auto-detect) so configure ERRORS if libaio-dev /
#   liburing-dev are missing on the runner — qemu-build.yml must keep
#   the build-deps in sync.
./configure \
    --prefix=/usr \
    --libexecdir=/usr/lib/qemu \
    --localstatedir=/var \
    --bindir=/usr/bin \
    --libdir=/usr/lib/x86_64-linux-gnu \
    --target-list=i386-softmmu,x86_64-softmmu \
    --enable-kvm \
    --enable-vnc \
    --enable-tools \
    --enable-linux-aio \
    --enable-linux-io-uring \
    --disable-docs \
    --disable-werror

log "Compiling with $BUILD_JOBS jobs"
make -j"$BUILD_JOBS"

log "Installing into $DESTDIR"
make -j"$BUILD_JOBS" install DESTDIR="$DESTDIR"

# ---- sanity ---------------------------------------------------------------

log "Sanity checks"
test -x "$DESTDIR/usr/bin/qemu-system-x86_64" || { echo "::error::qemu-system-x86_64 missing"; exit 1; }
test -x "$DESTDIR/usr/bin/qemu-img"           || { echo "::error::qemu-img missing"; exit 1; }
test -f "$DESTDIR/usr/share/qemu/bios.bin"    || { echo "::error::bios.bin missing"; exit 1; }

# Confirm the anti-VM patches actually landed in the binary by grepping
# strings(1). KVMKVMKVM should be absent; GenuineIntel present.
if strings "$DESTDIR/usr/bin/qemu-system-x86_64" | grep -q 'KVMKVMKVM'; then
    echo "::error::patched qemu-system-x86_64 still contains 'KVMKVMKVM' — patches didn't take"
    exit 1
fi

log "Build complete: qemu-${QEMU_VERSION} installed under $DESTDIR"
du -sh "$DESTDIR"
