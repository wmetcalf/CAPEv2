#!/usr/bin/env bash
# suricata-build/package.sh — assemble cape-suricata_<ver>_amd64.deb
# from a populated $DESTDIR.
#
# Reuses debian/cape-suricata.install as the file-list manifest (we
# only ship files matching its globs; everything else stays out of
# the deb). Reuses debian/cape-suricata.postinst verbatim.
#
# Required env:
#   DESTDIR          DESTDIR populated by build.sh
#   PACKAGE_VERSION  e.g. "7.0.13+cape.1" — embedded in the deb's
#                    Package-Version field and the filename
#   OUT_DIR          where to write the .deb (default: ./dist)
# Optional env:
#   CAPE_CORE_MIN_VERSION   value for the Depends: cape-core (>= X) line.
#                           If unset, the dependency line is omitted —
#                           useful for a first build before cape-core is
#                           even tagged.

set -euo pipefail

: "${DESTDIR:?DESTDIR required}"
: "${PACKAGE_VERSION:?PACKAGE_VERSION required}"
OUT_DIR="${OUT_DIR:-./dist}"
ARCH="${ARCH:-amd64}"
CAPE_CORE_MIN_VERSION="${CAPE_CORE_MIN_VERSION:-}"

# Repo root (assumes script lives at <repo>/suricata-build/).
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_MANIFEST="$REPO_ROOT/debian/cape-suricata.install"
POSTINST_SRC="$REPO_ROOT/debian/cape-suricata.postinst"

[ -f "$INSTALL_MANIFEST" ] || { echo "::error::missing $INSTALL_MANIFEST"; exit 1; }
[ -f "$POSTINST_SRC" ]    || { echo "::error::missing $POSTINST_SRC";    exit 1; }

mkdir -p "$OUT_DIR"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

log() { echo "[$(date -Iseconds)] [suricata-package] $*"; }

# ---- copy only the files in the install manifest ------------------------

log "Staging files from $DESTDIR per manifest"
while read -r line; do
    line="${line%%#*}"  # strip comments
    line="${line## }"
    line="${line%% }"
    [ -z "$line" ] && continue

    # Each manifest line is a glob relative to DESTDIR. Resolve and copy.
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

# Relax /etc/suricata config-file perms from upstream's 0600 root-only
# to 0644 world-readable.  CAPE's modules/processing/suricata.py
# spawns /usr/bin/suricata as the cape user (uid != 0) and passes
# -c /etc/suricata/suricata.yaml; with 0600 root the cape user gets
#   Error: conf-yaml-loader: failed to open file:
#   /etc/suricata/suricata.yaml: Permission denied
# and the processor records zero suricata.alerts / .http / .dns /
# .tls events in report.json even on a multi-megabyte dump.pcap.
# These files contain no secrets — rule defs, classification,
# threshold, capability config only.  Caught on a validation deploy 2026-05-19.
if [ -d "$STAGE/etc/suricata" ]; then
    find "$STAGE/etc/suricata" -type f \( -name '*.yaml' -o -name '*.config' \) \
        -exec chmod 0644 {} \;
    find "$STAGE/etc/suricata" -type d -exec chmod 0755 {} \;
fi

# ---- write DEBIAN control + postinst -------------------------------------

mkdir -p "$STAGE/DEBIAN"

INSTALLED_SIZE=$(du -sk "$STAGE" | awk '{print $1}')

# Runtime libs the suricata binary links against.  build.sh configures
# --enable-nfqueue so libnetfilter_queue is required; suricata always
# uses libnet for raw packet construction.  Hand-maintained because
# package.sh doesn't use dh_shlibdeps — missing entries surface only
# at runtime as "error while loading shared libraries: libX.so.N".
# Caught on a validation deploy off a baked AMI when ldd showed
# libnet.so.1 and libnetfilter_queue.so.1 not found.
DEPENDS_LINE="libc6, libpcre2-8-0, libyaml-0-2, libjansson4, libcap-ng0, libpcap0.8, libnss3, libnspr4, libmagic1, libnet1, libnetfilter-queue1"
if [ -n "$CAPE_CORE_MIN_VERSION" ]; then
    DEPENDS_LINE="$DEPENDS_LINE, cape-core (>= $CAPE_CORE_MIN_VERSION)"
fi

cat > "$STAGE/DEBIAN/control" <<EOF
Package: cape-suricata
Version: $PACKAGE_VERSION
Section: net
Priority: optional
Architecture: $ARCH
Depends: $DEPENDS_LINE
Conflicts: suricata, suricata-update
Replaces: suricata, suricata-update
Provides: suricata, suricata-update
Maintainer: CAPEv2 AWS Nested-Virt <noreply@example.invalid>
Installed-Size: $INSTALLED_SIZE
Description: Suricata IDS/IPS built with --enable-unix-socket for CAPE
 Patched Suricata build that enables unix-socket mode. CAPE drives
 suricata over a unix socket per analysis task; the upstream Ubuntu
 package omits this configure flag.
EOF

cp "$POSTINST_SRC" "$STAGE/DEBIAN/postinst"
chmod 755 "$STAGE/DEBIAN/postinst"

# ---- build the deb -------------------------------------------------------

OUT_DEB="$OUT_DIR/cape-suricata_${PACKAGE_VERSION}_${ARCH}.deb"
log "Building $OUT_DEB"
dpkg-deb --root-owner-group --build "$STAGE" "$OUT_DEB"

ls -la "$OUT_DEB"
log "package complete"
