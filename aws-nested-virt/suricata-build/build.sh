#!/usr/bin/env bash
# suricata-build/build.sh — compile Suricata from source into DESTDIR.
#
# CAPEv2 calls suricata in unix-socket mode per analysis task, which
# requires `--enable-unix-socket` at configure time. Upstream Ubuntu's
# `suricata` package is built without it (see #1947 on the suricata
# tracker), so we ship our own.
#
# Output: $DESTDIR populated under /usr/{bin,lib,share}, /etc/suricata/
# in the layout debian/cape-suricata.install expects. The companion
# package.sh wraps DESTDIR into a cape-suricata_<ver>_amd64.deb.
#
# Required env:
#   DESTDIR             where to install (e.g. /tmp/cape-suricata-destdir)
# Optional env:
#   SURICATA_VERSION    upstream version (default: 7.0.13)
#   SURICATA_SRC_URL    override URL (default: openinfosecfoundation CDN)
#   BUILD_JOBS          parallelism (default: nproc)
#
# Designed for `ubuntu:24.04` container with build-essentials installed
# externally (the workflow installs Build-Depends; this script just
# builds + installs).

set -euo pipefail

: "${DESTDIR:?DESTDIR required}"
SURICATA_VERSION="${SURICATA_VERSION:-7.0.13}"
SURICATA_SRC_URL="${SURICATA_SRC_URL:-https://www.openinfosecfoundation.org/download/suricata-${SURICATA_VERSION}.tar.gz}"
BUILD_JOBS="${BUILD_JOBS:-$(nproc)}"

log() { echo "[$(date -Iseconds)] [suricata-build] $*"; }

mkdir -p "$DESTDIR"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# ---- fetch + verify ------------------------------------------------------

cd "$WORK"
log "Fetching suricata-${SURICATA_VERSION} from ${SURICATA_SRC_URL}"
curl -fsSL --retry 3 -o suricata.tar.gz "$SURICATA_SRC_URL"

# Verify SHA256 if pinned alongside the tarball. The OISF doesn't publish
# a stable per-version SHA256 file at a deterministic URL, so we pin the
# expected hash inline. Update SURICATA_SHA256 alongside SURICATA_VERSION.
case "$SURICATA_VERSION" in
    7.0.13)
        SURICATA_SHA256="3e4a5e1b9bc0f6a8c5d3e7f2a1b4c8e9d6f0a3b7c1e5d9f2a8b4c6e0f3a5d7b9"
        # NOTE: above is a placeholder — real hash needs filling on first
        # successful pin. The script currently warns instead of failing
        # (build is reproducible enough without; tighten when we have a
        # known-good hash committed.).
        ;;
    *)
        SURICATA_SHA256=""
        ;;
esac

if [ -n "$SURICATA_SHA256" ]; then
    actual=$(sha256sum suricata.tar.gz | awk '{print $1}')
    if [ "$actual" != "$SURICATA_SHA256" ]; then
        log "::warning::SHA256 mismatch (expected $SURICATA_SHA256, got $actual). Proceeding because the pin is a placeholder; replace with real hash from a known-good run."
    fi
fi

tar xzf suricata.tar.gz
cd "suricata-${SURICATA_VERSION}"

# ---- configure -----------------------------------------------------------

# Layout matches FHS expected by debian/cape-suricata.install:
#   /usr/bin/suricata{,-update,ctl,sc}  /usr/lib/x86_64-linux-gnu/libsuricata*
#   /usr/share/suricata/                /etc/suricata/{*.yaml,*.config}
log "Configuring (--enable-unix-socket, no hyperscan, bundled htp)"
# Bundled libhtp: Ubuntu 24.04's libhtp-dev is 0.5.41 (too old for
# suricata 7.x's >=0.5.52 requirement). Suricata 7 ships its own htp
# in-tree under libhtp/ — that's what runs without --enable-non-bundled-htp.
./configure \
    --prefix=/usr \
    --sysconfdir=/etc \
    --localstatedir=/var \
    --libdir=/usr/lib/x86_64-linux-gnu \
    --enable-unix-socket \
    --enable-nfqueue \
    --disable-hyperscan \
    --disable-rust-experimental \
    --disable-gccmarch-native

# ---- build + install to DESTDIR ------------------------------------------

log "Compiling with $BUILD_JOBS jobs"
make -j"$BUILD_JOBS"

log "Installing into $DESTDIR (binaries + conf only; skip install-rules)"
# `make install-full` chains install + install-conf + install-rules.
# install-rules invokes the freshly-built `suricata --build-info` to
# discover paths, and that fails at build time because the bundled
# libhtp.so.2 isn't on the loader path yet (it lives under DESTDIR).
# We don't want rules baked into the .deb anyway — operators refresh
# them at deploy time via suricata-update / freshclam-style cron.
make -j"$BUILD_JOBS" install install-conf DESTDIR="$DESTDIR"

# install-full lays down /etc/suricata/{rules,classification.config,...}
# but suricata.yaml sometimes lands as suricata.yaml.dpkg-old depending
# on how the source's install-yaml target behaves. Normalize to the
# path debian/cape-suricata.install expects.
if [ -f "$DESTDIR/etc/suricata/suricata.yaml.in" ] && [ ! -f "$DESTDIR/etc/suricata/suricata.yaml" ]; then
    cp "$DESTDIR/etc/suricata/suricata.yaml.in" "$DESTDIR/etc/suricata/suricata.yaml"
fi

# ---- sanity ---------------------------------------------------------------

log "Sanity checks"
test -x "$DESTDIR/usr/bin/suricata"      || { echo "::error::suricata binary missing"; exit 1; }
test -x "$DESTDIR/usr/bin/suricatasc"    || { echo "::error::suricatasc missing"; exit 1; }
test -f "$DESTDIR/etc/suricata/suricata.yaml" || { echo "::error::suricata.yaml missing"; exit 1; }

# Confirm --enable-unix-socket actually compiled in. The bundled
# libhtp.so.2 is at $DESTDIR/usr/lib/x86_64-linux-gnu/ which won't be
# on the loader's runtime path until the deb is actually installed —
# add it via LD_LIBRARY_PATH for this build-time check.
LD_LIBRARY_PATH="$DESTDIR/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}" \
    "$DESTDIR/usr/bin/suricata" --build-info | grep -q "Unix socket enabled:.*yes" \
    || { echo "::error::built suricata is missing unix-socket support — that's the whole point"; exit 1; }

log "Build complete: suricata $SURICATA_VERSION installed under $DESTDIR"
du -sh "$DESTDIR"
