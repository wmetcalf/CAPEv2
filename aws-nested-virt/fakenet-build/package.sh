#!/usr/bin/env bash
# fakenet-build/package.sh — assemble cape-fakenet.deb.
#
# Ships the FakeNet-NG containerized network-simulation stack for
# CAPE: the docker-compose source tree at /opt/CAPEv2/docker/fakenet-
# ng/, the cape-fakenet.service systemd unit that runs the container,
# and the networkd-dispatcher hook that wires iptables FORWARD between
# the CAPE VM bridge (virbr100) and the FakeNet bridge (br-fakenet).
#
# Image is NOT pre-built into the deb.  postinst runs
# `docker compose build` once at install time — first install on a
# fresh AMI takes ~5 min (pip-install fakenet-ng from github, apt
# install build-essential libnetfilter-queue-dev iptables), subsequent
# installs reuse cached layers and run in seconds.
#
# postinst also updates /etc/cape/routing.conf [inetsim] block in
# place so CAPE knows where to redirect VM traffic (server=172.28.
# 100.2, dnsport=53, interface=br-fakenet).  Mirrors the legacy
# bootstrap's setup-host.sh logic.
#
# Inputs:
#   PACKAGE_VERSION   deb Version field
#   OUT_DIR           default ./dist

set -euo pipefail

: "${PACKAGE_VERSION:?PACKAGE_VERSION required}"
OUT_DIR="${OUT_DIR:-./dist}"
ARCH=all

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FILES_DIR="$REPO_ROOT/fakenet-build/files"

log() { echo "[$(date -Iseconds)] [fakenet-package] $*"; }

[ -d "$FILES_DIR" ] || { log "::error::missing $FILES_DIR"; exit 1; }

mkdir -p "$OUT_DIR"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

log "Staging files from $FILES_DIR"
cp -a "$FILES_DIR/opt" "$STAGE/opt"
cp -a "$FILES_DIR/etc" "$STAGE/etc"
find "$STAGE" -type f -exec chmod 0644 {} \;
find "$STAGE" -type d -exec chmod 0755 {} \;

# Generate the interception CA for this package artifact. The portable source
# intentionally carries no reusable CA private key from the source environment.
command -v openssl >/dev/null 2>&1 || {
    log "::error::openssl is required to generate the FakeNet CA"
    exit 1
}
FAKENET_DIR="$STAGE/opt/CAPEv2/docker/fakenet-ng"
openssl req -x509 -newkey rsa:3072 -sha256 -nodes \
    -keyout "$FAKENET_DIR/fakenet_ca.key" \
    -out "$FAKENET_DIR/fakenet_ca.crt" \
    -days 3650 \
    -subj '/CN=CAPE FakeNet Package CA' >/dev/null 2>&1

# CA private key must be mode 0600 — Dockerfile copies it into the
# image and FakeNet's listeners load it with paramiko/cryptography
# which complain about world-readable private keys.
chmod 0600 "$FAKENET_DIR/fakenet_ca.key"

# networkd-dispatcher hooks must be executable.
chmod 0755 "$STAGE/etc/networkd-dispatcher/routable.d/50-cape-fakenet"

mkdir -p "$STAGE/DEBIAN"
INSTALLED_SIZE=$(du -sk "$STAGE" | awk '{print $1}')

cat > "$STAGE/DEBIAN/control" <<EOF
Package: cape-fakenet
Version: ${PACKAGE_VERSION}
Section: net
Priority: optional
Architecture: ${ARCH}
Depends: docker.io | docker-ce,
 docker-compose-v2 | docker-compose-plugin,
 iptables,
 networkd-dispatcher
Maintainer: CAPEv2 AWS Nested-Virt <noreply@example.invalid>
Installed-Size: ${INSTALLED_SIZE}
Description: FakeNet-NG containerized network simulation for CAPE
 Ships the FakeNet-NG (https://github.com/mandiant/flare-fakenet-ng)
 docker-compose stack used by CAPE's rooter.py inetsim_trap() to
 absorb analysis-VM network traffic when the operator submits with
 route=inetsim.  FakeNet runs in DivertTraffic=No (listen-only) mode
 — CAPE's rooter handles all iptables DNAT redirection.
 .
 At install time the postinst runs \`docker compose build\` in
 /opt/CAPEv2/docker/fakenet-ng/ to build the image locally
 (~5 min first install, fast cached rebuild after).  cape-fakenet.
 service then runs the container; the networkd-dispatcher hook at
 /etc/networkd-dispatcher/routable.d/50-cape-fakenet installs the
 iptables FORWARD rules between virbr100 and br-fakenet whenever
 virbr100 enters routable state (typically host boot after libvirt
 brings the cape-100 net up).
 .
 routing.conf [inetsim] is auto-populated in postinst with:
   enabled = yes
   server = 172.28.100.2
   dnsport = 53
   interface = br-fakenet
EOF

# Mark the fakenet config + CA pair as conffiles so dpkg tracks
# operator edits.  Dockerfile NOT a conffile — operators shouldn't
# edit the build recipe.
cat > "$STAGE/DEBIAN/conffiles" <<EOF
/opt/CAPEv2/docker/fakenet-ng/fakenet.ini
/opt/CAPEv2/docker/fakenet-ng/fakenet_ca.crt
/opt/CAPEv2/docker/fakenet-ng/fakenet_ca.key
EOF

cat > "$STAGE/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e
case "$1" in
    configure)
        systemctl daemon-reload || true

        # Build the FakeNet image up front so the first cape-fakenet.
        # service start doesn't block for ~5 min on the pip-install +
        # apt-install steps in the Dockerfile.  Subsequent installs
        # reuse cached layers and finish in seconds.
        if command -v docker >/dev/null 2>&1; then
            if systemctl is-active docker.service >/dev/null 2>&1; then
                echo "fakenet: building fakenet-ng image (this takes ~5 min on first install)..."
                cd /opt/CAPEv2/docker/fakenet-ng && \
                    docker compose build 2>&1 | tail -3 || \
                    echo "fakenet: docker compose build failed; cape-fakenet.service will retry on start" >&2
            else
                echo "fakenet: docker.service not active; skipping image build (cape-fakenet.service will build on start)" >&2
            fi
        else
            echo "fakenet: docker not installed; cape-fakenet.service will fail until docker.io lands" >&2
        fi

        # Enable + start the fakenet container service.
        systemctl enable --now cape-fakenet.service 2>/dev/null || \
            echo "fakenet: cape-fakenet.service enable/start failed (continuing)" >&2

        # Enable cape-fakenet-fix.service (shipped by cape-host-runtime,
        # earlier testing).  It fixes Docker's raw-table rule that otherwise
        # blocks FakeNet's NFQUEUE catch-all.
        if systemctl list-unit-files cape-fakenet-fix.service >/dev/null 2>&1; then
            systemctl enable --now cape-fakenet-fix.service 2>/dev/null || \
                echo "fakenet: cape-fakenet-fix.service enable failed (continuing)" >&2
        fi

        # Update /etc/cape/routing.conf [inetsim] block in place so CAPE
        # rooter knows where to redirect VM traffic.  Mirrors the
        # setup-host.sh logic from the legacy bootstrap.  Idempotent —
        # only flips the four keys, doesn't touch anything else in the
        # section.
        ROUTING_CONF=/etc/cape/routing.conf
        if [ -f "$ROUTING_CONF" ]; then
            python3 - "$ROUTING_CONF" <<'PYEOF' || \
                echo "fakenet: routing.conf [inetsim] update failed (continuing)" >&2
import re, sys

conf_path = sys.argv[1]
with open(conf_path, "r") as f:
    content = f.read()

# Only edit the four keys inside [inetsim]; leave the rest of the
# section (comments, ports=) alone.  re.DOTALL+MULTILINE lets the
# key=value pattern span lines inside the section block.
updates = {
    "enabled":   "yes",
    "server":    "172.28.100.2",
    "dnsport":   "53",
    "interface": "br-fakenet",
}
for key, val in updates.items():
    pattern = rf"(\[inetsim\].*?^{key}\s*=\s*)([^\n]*)"
    new_content = re.sub(pattern, rf"\g<1>{val}", content,
                         count=1, flags=re.MULTILINE | re.DOTALL)
    if new_content == content:
        # Section or key missing — log but don't fail.  Caller will
        # see the line in stdout and can fix manually.
        print(f"fakenet: routing.conf has no [inetsim] {key}=; not setting", file=sys.stderr)
    content = new_content

with open(conf_path, "w") as f:
    f.write(content)
PYEOF
        else
            echo "fakenet: $ROUTING_CONF missing; cape-core not installed yet?" >&2
        fi
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
        systemctl disable --now cape-fakenet.service 2>/dev/null || true
        # Stop + remove the container if it's still around — postinst's
        # docker compose down covers the unit-stop path but not all
        # operator hand-removals.
        docker compose -f /opt/CAPEv2/docker/fakenet-ng/docker-compose.yml down 2>/dev/null || true
        systemctl daemon-reload || true
        ;;
esac
exit 0
POSTRM
chmod 0755 "$STAGE/DEBIAN/postrm"

OUT_DEB="$OUT_DIR/cape-fakenet_${PACKAGE_VERSION}_${ARCH}.deb"
log "Building $OUT_DEB"
dpkg-deb --root-owner-group --build "$STAGE" "$OUT_DEB"
ls -la "$OUT_DEB"
