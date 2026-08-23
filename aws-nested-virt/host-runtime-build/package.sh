#!/usr/bin/env bash
# host-runtime-build/package.sh — assemble cape-host-runtime.deb.
#
# cape-host-runtime is the "live-host overlay turned into a deb": it
# ships the host-level glue that the legacy nestedvirt deployment
# expected an rsync'd mirror of a hand-built CAPE host to provide.
# Without this deb, a deb-baked AMI boots but has no:
#
#   - VPN tunnel (openvpn-pia.service is missing)
#   - guest routing (cape-routing.service + setup-cape-routing.sh)
#   - firewall isolation (cape-firewall-isolation.service +
#     /etc/cape/firewall/load-rules.sh + operator-supplied rules-*.nft)
#   - Docker raw-table FakeNet exception (cape-fakenet-fix.service)
#   - libvirt cape-100 network XML + auto-restore on boot
#     (cape-libvirt-restore.service)
#   - operator helper scripts (reset-cape-analysis-state.sh)
#
# Splits the work along the same lines as cape-host-config:
#   cape-host-config   — apt-pointed daemon configs
#   cape-host-runtime  — cape-project services + scripts
#
# Inputs:
#   PACKAGE_VERSION   deb Version field
#   OUT_DIR           (optional, default ./dist)

set -euo pipefail

: "${PACKAGE_VERSION:?PACKAGE_VERSION required}"
OUT_DIR="${OUT_DIR:-./dist}"
ARCH=all

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FILES_DIR="$REPO_ROOT/host-runtime-build/files"

log() { echo "[$(date -Iseconds)] [host-runtime-package] $*"; }

[ -d "$FILES_DIR" ] || { log "::error::missing $FILES_DIR"; exit 1; }

mkdir -p "$OUT_DIR"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

# Mirror files/ tree into the deb staging root.
log "Staging runtime files from $FILES_DIR"
cp -a "$FILES_DIR/etc" "$STAGE/etc"
cp -a "$FILES_DIR/usr" "$STAGE/usr"
find "$STAGE/etc" "$STAGE/usr" -type d -exec chmod 0755 {} \;
find "$STAGE/etc" "$STAGE/usr" -type f -exec chmod 0644 {} \;

# Scripts must be executable.  Override the blanket 0644 above.
chmod 0755 \
    "$STAGE/usr/local/bin/setup-cape-routing.sh" \
    "$STAGE/usr/local/bin/restore-cape-libvirt-state.sh" \
    "$STAGE/usr/local/bin/reset-cape-analysis-state.sh" \
    "$STAGE/usr/local/bin/cape-post-deploy-smoke.sh" \
    "$STAGE/etc/cape/firewall/fakenet-raw-fix.sh" \
    "$STAGE/etc/cape/firewall/load-rules.sh" \
    "$STAGE/etc/openvpn/pia-cape-route-up.sh" \
    "$STAGE/etc/openvpn/pia-cape-down.sh"

# /etc/openvpn/pia/credentials is intentionally NOT shipped — it's a
# secret that lives in AWS Secrets Manager and gets staged onto the
# host at deploy time (or via the legacy mirror-live-host script).
# openvpn-pia.service has ConditionPathExists=/etc/openvpn/pia/
# credentials so the unit just no-ops until creds appear.

# ---- DEBIAN control ------------------------------------------------------
mkdir -p "$STAGE/DEBIAN"
INSTALLED_SIZE=$(du -sk "$STAGE" | awk '{print $1}')

cat > "$STAGE/DEBIAN/control" <<EOF
Package: cape-host-runtime
Version: ${PACKAGE_VERSION}
Section: misc
Priority: optional
Architecture: ${ARCH}
Depends: cape-core,
 iproute2,
 iptables,
 nftables,
 libvirt-clients,
 openvpn,
 suricata | cape-suricata,
 docker.io | docker-ce
Maintainer: CAPEv2 AWS Nested-Virt <noreply@example.invalid>
Installed-Size: ${INSTALLED_SIZE}
Description: Host-level services + helper scripts for the CAPE sandbox
 The legacy nestedvirt deployment relied on an rsync'd "live-host"
 overlay of a hand-built CAPE host to supply:
 .
 cape-routing.service + setup-cape-routing.sh
 cape-firewall-isolation.service + /etc/cape/firewall/load-rules.sh
   + rules-isolation.nft (host metadata / VPC / SMTP / BitTorrent /
   guest-to-guest / non-DHCP broadcast drops at mangle priority so
   CAPE's rooter.py can't overwrite them in the filter table)
 cape-fakenet-fix.service
 cape-libvirt-restore.service + restore-cape-libvirt-state.sh
 openvpn-pia.service + /etc/openvpn/pia-cape.conf + PIA certs
 /etc/libvirt/qemu/networks/cape-100.xml
 reset-cape-analysis-state.sh
 .
 None of those reached a deb-baked AMI before this deb; CAPE would
 boot but had no VPN, no guest routing, no firewall isolation, and
 no auto-started cape-100 libvirt network.  cape-host-runtime ships
 all of the above as a single dpkg-managed unit so the AMI bake
 reproduces the full host-side surface.
 .
 The PIA credentials file is intentionally NOT included — it's
 staged via AWS Secrets Manager (or the legacy mirror-live-host
 script) at deploy time.  openvpn-pia.service has
 ConditionPathExists=/etc/openvpn/pia/credentials so the unit
 no-ops until creds appear.
EOF

# Conffiles — operators may want to tweak the openvpn config, the
# libvirt network's MAC/DHCP layout, or the isolation ruleset per
# host.  Helper scripts in /usr/local/bin and /etc/cape/firewall/*.sh
# are NOT conffiles because /usr/local isn't policy-managed and the
# firewall .sh scripts are functional code rather than config.
cat > "$STAGE/DEBIAN/conffiles" <<EOF
/etc/libvirt/qemu/networks/cape-100.xml
/etc/openvpn/pia-cape.conf
/etc/openvpn/pia/ca.rsa.2048.crt
/etc/openvpn/pia/crl.rsa.2048.pem
/etc/cape/firewall/rules-isolation.nft
EOF

# Postinst:
#   - daemon-reload so new units register
#   - enable (but don't start) cape-routing/cape-firewall-isolation/
#     cape-libvirt-restore — they want libvirtd up first and on a
#     fresh install libvirtd's own postinst hasn't necessarily run yet
#   - cape-fakenet-fix is left disabled by default (Docker is only
#     installed on the FakeNet-equipped hosts)
#   - openvpn-pia.service is enabled; the ConditionPathExists guards
#     it from auth-looping when no credentials are staged
# dpkg triggers: cape-host-runtime is the interest holder for the
# cape-processor-reload trigger.  Threat-content debs (cape-yara-forge,
# cape-sigma-rules, cape-community) activate this trigger in their own
# DEBIAN/triggers files.  dpkg coalesces activations across a single
# transaction so cape.service / cape-processor.service / cape-web.service
# restart exactly once even when all three threat debs upgrade together
# (e.g. on the cape-threat-update.timer's daily apt run).
cat > "$STAGE/DEBIAN/triggers" <<'TRIGGERS'
interest cape-processor-reload
TRIGGERS

cat > "$STAGE/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e

case "$1" in
    configure)
        systemctl daemon-reload || true

        # Enable boot-time units so the host comes up fully configured
        # after a reboot.  --now is intentionally omitted: we don't
        # want to race libvirtd / openvpn during install on a host
        # where those services may still be starting.
        #
        # cape-firewall-isolation IS enabled by default now that we
        # ship rules-isolation.nft (this deb's own conffile).
        # load-rules.sh has something to load from t=0, so the unit
        # no longer loops a "no rule files found" warning on boot.
        for unit in cape-libvirt-restore.service \
                    cape-routing.service \
                    cape-firewall-isolation.service \
                    cape-guacd.service \
                    cape-post-deploy-smoke.service \
                    suricata.service \
                    openvpn-pia.service; do
            systemctl enable "$unit" 2>/dev/null || true
        done

        # suricata.service: start now if the binary + rules are present
        # (i.e. a re-bake / re-install on a host that already has
        # cape-suricata + a populated /etc/suricata/rules), so socket
        # mode is live without waiting for a reboot.  On a fresh AMI
        # bake the binary may not be installed yet at this postinst —
        # the unit is enabled above and starts on next boot, ordered
        # Before=cape-processor.service.  try-start is best-effort.
        if [ -x /usr/bin/suricata ]; then
            systemctl start suricata.service 2>/dev/null || \
                echo "host-runtime: suricata.service start deferred to boot \
(binary/rules may not be staged yet)" >&2
        fi

        # cape-guacd: start now so the docker pull happens at install
        # time (and operator gets a fast failure if the image isn't
        # reachable), not on the first guac browser request.  The
        # service is idempotent on re-install — ExecStartPre does
        # `docker rm -f guacd` to clear any stale container.
        if systemctl is-active docker.service >/dev/null 2>&1; then
            systemctl start cape-guacd.service 2>/dev/null || \
                echo "host-runtime: cape-guacd.service start failed — \
guac sessions will fail until 'systemctl start cape-guacd' succeeds" >&2
        fi
        ;;

    triggered)
        # $2 contains the space-separated list of triggers that fired.
        # Currently only cape-processor-reload is wired here, but we
        # match by name rather than assuming so future triggers don't
        # accidentally restart cape services.
        for trig in $2; do
            case "$trig" in
                cape-processor-reload)
                    # try-restart is a no-op when the unit isn't active,
                    # so this is safe on a fresh install where cape*
                    # haven't been started yet (the upgrade-only post-
                    # install timer can't trigger first-install paths).
                    #
                    # Order matters slightly: cape.service holds the
                    # scheduler; restarting cape-processor + cape-web
                    # first while the scheduler is still up means the
                    # scheduler buffers any pending tasks during the
                    # processor restart (~5-10s) rather than losing
                    # them.  cape.service last so its restart is the
                    # shortest interruption.
                    for svc in cape-processor.service cape-web.service cape.service; do
                        systemctl try-restart "$svc" 2>/dev/null || true
                    done
                    ;;
            esac
        done
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
        for unit in cape-libvirt-restore.service \
                    cape-routing.service \
                    cape-firewall-isolation.service \
                    cape-fakenet-fix.service \
                    cape-guacd.service \
                    cape-post-deploy-smoke.service \
                    suricata.service \
                    openvpn-pia.service; do
            systemctl disable --now "$unit" 2>/dev/null || true
        done
        systemctl daemon-reload || true
        ;;
esac
exit 0
POSTRM
chmod 0755 "$STAGE/DEBIAN/postrm"

OUT_DEB="$OUT_DIR/cape-host-runtime_${PACKAGE_VERSION}_${ARCH}.deb"
log "Building $OUT_DEB"
dpkg-deb --root-owner-group --build "$STAGE" "$OUT_DEB"
ls -la "$OUT_DEB"
log "package complete"
