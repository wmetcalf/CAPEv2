#!/usr/bin/env bash
# host-config-build/package.sh — assemble cape-host-config.deb.
#
# Ships host-side config files for the threat-content consumer
# daemons on a CAPE host:
#
#   /etc/clamav/freshclam.conf
#     DatabaseMirror = Cisco (unchanged) +
#     DatabaseCustomURL entries pointing at
#     https://apt.example.invalid/<file>
#
#   /etc/suricata/update.yaml
#     sources = cape/rules, update-sources points at the cape/rules CDN
#     index.yaml URL.
#
#   /etc/systemd/system/cape-suricata-update.{service,timer}
#     runs `suricata-update` every 6h and reloads suricata.service.
#     (ClamAV equivalent is the stock clamav-freshclam.service which
#     comes from the clamav-freshclam apt package — no need for us
#     to ship a timer.)
#
# Inputs:
#   PACKAGE_VERSION       deb Version field
#   THREAT_CONTENT_URL    required threat-content release base URL; the
#                         ClamAV/Suricata refresh assets are served flat
#                         (bare filenames) under it
#   OUT_DIR               default ./dist

set -euo pipefail

: "${PACKAGE_VERSION:?PACKAGE_VERSION required}"
: "${THREAT_CONTENT_URL:?THREAT_CONTENT_URL required (for example, https://github.com/OWNER/REPO/releases/download/threat-content)}"
THREAT_CONTENT_URL="${THREAT_CONTENT_URL%/}"
OUT_DIR="${OUT_DIR:-./dist}"
ARCH=all

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FILES_DIR="$REPO_ROOT/host-config-build/files"

log() { echo "[$(date -Iseconds)] [host-config-package] $*"; }

[ -d "$FILES_DIR" ] || { log "::error::missing $FILES_DIR"; exit 1; }

mkdir -p "$OUT_DIR"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

# Mirror files/ tree as-is into the deb's staging root.
log "Staging config files from $FILES_DIR"
cp -a "$FILES_DIR/etc" "$STAGE/etc"
find "$STAGE/etc" -type f -exec chmod 0644 {} \;
find "$STAGE/etc" -type d -exec chmod 0755 {} \;

# sudoers files must be mode 0440 root:root or sudo refuses to load
# them ("sudoers is world-writable").  Override the blanket 0644 above.
# --root-owner-group on dpkg-deb fixes ownership; chmod fixes mode.
if [ -f "$STAGE/etc/sudoers.d/90-cape-admin" ]; then
    chmod 0440 "$STAGE/etc/sudoers.d/90-cape-admin"
fi

# DEBIAN metadata
mkdir -p "$STAGE/DEBIAN"
INSTALLED_SIZE=$(du -sk "$STAGE" | awk '{print $1}')

cat > "$STAGE/DEBIAN/control" <<EOF
Package: cape-host-config
Version: ${PACKAGE_VERSION}
Section: misc
Priority: optional
Architecture: ${ARCH}
Depends: cape-core, clamav-daemon, clamav-freshclam, suricata-update, suricata | cape-suricata, cape-zircolite, apparmor
Replaces: clamav-freshclam, suricata-update
Breaks: clamav-freshclam (<< 1.0.0~), suricata-update (<< 0~)
Maintainer: CAPEv2 AWS Nested-Virt <noreply@example.invalid>
Installed-Size: ${INSTALLED_SIZE}
Description: Host-side config for the CAPE sandbox
 Wires the host's threat-content daemons (freshclam, suricata-update)
 to the the ruleset CDN instead of public upstreams, and ships the
 small set of host-level policy bits CAPE needs to be functional
 out of the box on a fresh deb-baked AMI:
 .
 freshclam.conf — DatabaseMirror remains Cisco (database.clamav.net,
 the canonical mirror network); DatabaseCustomURL entries point at
 https://apt.example.invalid/ for the 3rd-party
 signature feeds (SaneSecurity, URLhaus, twinclams, wmetcalf
 clam-punch).
 .
 update.yaml — suricata-update sources pinned to cape/rules (the
 ET Open + cape-project ruleset, built via suricata-rules-build
 and served at https://apt.example.invalid/).
 .
 apparmor.d/local/usr.sbin.clamd — read-access exception for
 /opt/CAPEv2/storage/** so clamd can scan the samples CAPE stages
 under storage/binaries/.
 .
 sudoers.d/90-cape-admin — passwordless sudo for the cape service
 account so operator SSH-as-cape sessions can run systemctl/virsh
 without a second account.
 .
 Includes a systemd timer (cape-suricata-update.timer) that fires
 suricata-update every 6h. The matching ClamAV refresh is the stock
 clamav-freshclam.service from the clamav-freshclam apt package.
EOF

# Declare config files as conffiles so dpkg tracks operator edits.
#
# /etc/apparmor.d/local/usr.sbin.clamd is intentionally NOT listed here:
# clamav-daemon (in Depends:) ships its own empty template at the same
# path and Packer's --force-confnew flag lets clamav-daemon's empty file
# silently overwrite ours when it's the later-installed package on the
# dependency graph.  Result: target.file.clamav came back [] on the
# reference host after the file landed as zero bytes.  Instead, postinst
# below writes the rule into whatever file already exists at that path
# (idempotent: skip if our marker line is already there) — survives any
# conffile race because we're writing at the running system, not at
# dpkg install time.
cat > "$STAGE/DEBIAN/conffiles" <<EOF
/etc/clamav/freshclam.conf
/etc/suricata/update.yaml
/etc/sudoers.d/90-cape-admin
EOF

# Postinst: reload systemd, enable the suricata-update timer, prod
# the daemons so they pick up the new config.
cat > "$STAGE/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e
case "$1" in
    configure)
        systemctl daemon-reload || true

        # Enable + start the periodic suricata-update timer.
        systemctl enable --now cape-suricata-update.timer 2>/dev/null || true

        # Enable the daily threat-content deb refresh timer.  Fires
        # 04:30 UTC + random ≤10 min jitter, runs `apt install --only-
        # upgrade cape-yara-forge cape-sigma-rules cape-community`.
        # Service restarts coalesce via cape-host-runtime's
        # cape-processor-reload dpkg trigger.  Not started --now: we
        # don't want a deb-install to immediately bounce cape services
        # on the AMI bake host (the bake's cape.service isn't even up
        # yet at this postinst).  First fire happens on the next 04:30
        # UTC tick after deploy, or on boot if Persistent=true caught
        # a missed run.
        systemctl enable cape-threat-update.timer 2>/dev/null || true

        # Enable + (re)start freshclam. The AMI bake installs clamav-freshclam
        # but leaves the service DISABLED (packer builds don't auto-enable
        # services), and a bare try-restart is a no-op on a stopped service — so
        # without the explicit enable the ClamAV DB silently never refreshes
        # (frozen at bake time). restart also picks up the freshclam.conf we ship.
        systemctl enable clamav-freshclam.service 2>/dev/null || true
        systemctl restart clamav-freshclam.service 2>/dev/null || true

        # Add clamav user to the cape group so clamd can traverse
        # /opt/CAPEv2/storage/ (chmod 750, group=cape) to reach the
        # sample binaries CAPE stages under storage/binaries/.  Even
        # with the apparmor exception below, clamd would still get
        # EACCES on the dir traversal without group membership.
        # Caught on the reference host 2026-05-19 where target.file.clamav came
        # back [] for EICAR until both apparmor AND group landed.
        # Idempotent: usermod -aG no-ops if already a member.
        if getent passwd clamav >/dev/null 2>&1 && getent group cape >/dev/null 2>&1; then
            usermod -a -G cape clamav || \
                echo "host-config: usermod failed adding clamav to cape group" >&2
        fi

        # Append the /opt/CAPEv2/storage/** apparmor exception to
        # whatever /etc/apparmor.d/local/usr.sbin.clamd already exists
        # (clamav-daemon ships an empty template there; we used to
        # conffile-ship our content but Packer's --force-confnew let
        # clamav-daemon's empty template win the dpkg race, leaving
        # the file zero bytes on the baked AMI).  Writing at postinst
        # time survives that race: idempotent via the marker grep.
        APPARMOR_LOCAL=/etc/apparmor.d/local/usr.sbin.clamd
        if [ -d "$(dirname "$APPARMOR_LOCAL")" ] \
            && ! grep -qF '/opt/CAPEv2/storage/**' "$APPARMOR_LOCAL" 2>/dev/null; then
            cat >> "$APPARMOR_LOCAL" <<'APPARMOR_RULE'

# managed via cape-host-config.deb postinst.
# clamav-daemon ships /etc/apparmor.d/usr.sbin.clamd which #include's
# this local override.  Without read access to /opt/CAPEv2/storage/**,
# CAPE's clamav integration (lib/cuckoo/common/integrations/clamav.py)
# gets EACCES from clamd when scanning sample binaries CAPE has saved
# under storage/binaries/ -- the analysis report comes back with zero
# clamav matches even when the sample is a known signature hit.
/opt/CAPEv2/storage/** r,
APPARMOR_RULE
            echo "host-config: appended /opt/CAPEv2/storage rule to $APPARMOR_LOCAL"
        fi

        # Reload the clamd apparmor profile so the local override
        # (read access to /opt/CAPEv2/storage/**) takes effect without
        # a service restart.  -r is "replace" — equivalent to an
        # apparmor_parser --load against an already-loaded profile.
        if [ -f /etc/apparmor.d/usr.sbin.clamd ] && command -v apparmor_parser >/dev/null 2>&1; then
            apparmor_parser -r /etc/apparmor.d/usr.sbin.clamd 2>/dev/null || \
                echo "host-config: apparmor_parser -r failed; clamav may EACCES on /opt/CAPEv2/storage" >&2
        fi

        # Restart clamav-daemon to pick up the new group membership
        # (clamd reads its uid/gid at startup, not per-scan).
        systemctl try-restart clamav-daemon.service 2>/dev/null || true

        # Add the tun0 routing table to /etc/iproute2/rt_tables so
        # CAPE's init_routing() can validate [vpn0] rt_table=tun0
        # at cape.service startup.  Without it, cape.service crashes
        # at boot with:
        #   CuckooStartupError: The routing table that has been
        #   configured for VPN vpn0 is not available
        # Legacy live host had this baked in; deb-baked AMI didn't,
        # so any nestedvirt config that enables [vpn] in routing.conf
        # fails at startup. Idempotent: skip if already present.
        if [ -f /etc/iproute2/rt_tables ] && ! grep -qE '^[0-9]+[[:space:]]+tun0$' /etc/iproute2/rt_tables; then
            echo "200 tun0" >> /etc/iproute2/rt_tables
        fi

        # Register the cape/rules suricata-update source with a direct
        # tarball URL.  The threat-content release serves the bundle
        # unauthenticated as a bare-named asset (public fork — the
        # source URL carries no request auth of any kind).
        #
        # Why not the update.yaml + update-sources / sources model:
        # suricata-update's `update-sources` subcommand is hardcoded
        # to fetch from OISF's index.yaml regardless of update-sources:
        # entries in update.yaml, so the cape/rules name never gets
        # resolved.  `add-source <name> <url>` registers a direct
        # source that's used by the next `suricata-update` invocation
        # without any index lookup.
        #
        # Idempotent via remove-source-first: if the source is already
        # registered with a stale URL (e.g. the release base moved), the
        # remove+add cycles to the current URL.
        # Caught on the reference host 2026-05-21: without this, suricata
        # loaded only 426 protocol-event rules (suricata's own
        # bundled rules) and analysis suricata.alerts was always 0,
        # even on chrome→icanhazip.com which has 230 ET POLICY
        # matching rules in the full ET Open ruleset.
        SU_SRC_NAME=cape/rules
        SU_SRC_URL_BASE=https://apt.example.invalid/cape-rules.tar.gz
        if command -v suricata-update >/dev/null 2>&1; then
            # remove-source no-ops if not registered; that's fine.
            suricata-update remove-source "$SU_SRC_NAME" >/dev/null 2>&1 || true
            suricata-update add-source "$SU_SRC_NAME" "$SU_SRC_URL_BASE" >/dev/null 2>&1 \
                || echo "host-config: failed to register suricata source $SU_SRC_NAME" >&2
        fi

        # Bridge /etc/suricata/rules/ ↔ /var/lib/suricata/rules/.
        # cape-suricata.deb chmod's /etc/suricata/rules to 755 at
        # build time but the upstream `make install` produces dir
        # mode 750 root-only that survives dpkg's --root-owner-group
        # handling; either way, suricata.yaml's `default-rule-path:
        # /var/lib/suricata/rules` (suricata's compile-time default)
        # doesn't see anything when the rules actually live under
        # /etc/suricata/rules.  Symlink + chmod 755 to handle both.
        # Without this fix: per-task suricata invocations log
        #   Warning: detect: opening rule file /var/lib/suricata/rules/
        #   suricata.rules: Permission denied.
        # and report 0 alerts.
        #
        # Unconditional now (was previously gated on `[ -d /etc/suricata/
        # rules ]`).  The gate failed on fresh AMI bakes where this
        # postinst runs BEFORE suricata-update creates /etc/suricata/
        # rules — the gate skipped the block, suricata-update ran later,
        # the symlink was never created, and per-task suricata
        # invocations loaded 0 rules.  Caught on the reference host 2026-05-22
        # — test analysis chrome→icanhazip
        # produced 0 alerts despite the apt repo carrying 50,195 rules
        # because /var/lib/suricata/rules/suricata.rules was missing.
        #
        # All three operations are idempotent and safe to run before
        # the target paths exist:
        #   - mkdir -p          creates intermediate dirs, no-op if present
        #   - chmod 755         on a missing dir is a no-op (|| true)
        #   - ln -sf            creates a symlink to a target that may not
        #                       exist yet; resolves correctly once
        #                       suricata-update writes the file below
        mkdir -p /var/lib/suricata/rules
        chmod 755 /var/lib/suricata /var/lib/suricata/rules || true
        chmod 755 /etc/suricata/rules 2>/dev/null || true
        ln -sf /etc/suricata/rules/suricata.rules \
               /var/lib/suricata/rules/suricata.rules
        # classification.config too — suricata reads it from default-
        # rule-path alongside the rules file.  Without this the per-
        # task suricata invocation warns about missing classification
        # categories and falls back to numeric priorities only.
        ln -sf /etc/suricata/rules/classification.config \
               /var/lib/suricata/rules/classification.config

        # Kick suricata-update now so the new source produces a
        # populated /etc/suricata/rules/suricata.rules immediately.
        # Without this the rules stay at whatever the previous
        # cape-suricata-update.timer fired produced (often empty on
        # a fresh AMI).  After this runs, the symlinks above point at
        # a populated target.
        if command -v suricata-update >/dev/null 2>&1; then
            suricata-update --no-test 2>&1 | tail -3 || \
                echo "host-config: suricata-update post-install run failed (continuing)" >&2
        fi

        # Re-apply the chmod on /etc/suricata/rules AFTER suricata-
        # update, because suricata-update's `make install`-style
        # recreate of the directory drops it back to mode 750 root-
        # only, masking the symlink's permissions for the cape user.
        chmod 755 /etc/suricata/rules 2>/dev/null || true

        # Validate the sudoers drop-in BEFORE sudo notices it — a syntax
        # error in /etc/sudoers.d/* breaks `sudo` host-wide.  visudo -cf
        # exits 0 only if the file parses; non-zero means we shipped a
        # bad file and the operator should consider the postinst failed.
        if [ -f /etc/sudoers.d/90-cape-admin ]; then
            if ! visudo -cf /etc/sudoers.d/90-cape-admin >/dev/null 2>&1; then
                echo "host-config: /etc/sudoers.d/90-cape-admin FAILED visudo -cf — removing to keep sudo functional" >&2
                rm -f /etc/sudoers.d/90-cape-admin
                exit 1
            fi
        fi

        # CAPE conffile section enables.  Flips upstream `enabled = no`
        # to `enabled = yes` for the set of sections that the deployment's
        # deployment policy turns on by default.
        #
        # Moved from terraform/nestedvirt-ami/userdata.sh on 2026-05-22
        # per the TODO(structural) comment at userdata.sh:328 — the
        # sed-list had been growing in two places (userdata + here)
        # and was missing new display_* sections on the web side, so
        # column features (ETW, CAPE YARA, ET/PT portal links, sigma
        # signatures, authenticode, submitter) silently failed to
        # render on freshly-baked AMIs.
        #
        # Runs at every cape-host-config install (i.e. at every
        # ami-bake), so cape-core's /etc/cape/*.conf conffiles get
        # the same toggles whether the deploy is fresh or a re-bake.
        # cape-core listed in Depends above so /etc/cape/ exists by
        # the time this runs.
        flip_section_enabled() {
            local conf="$1" section="$2"
            [ -f "$conf" ] || return 0
            sed -i "/^\\[$section\\]/,/^\\[/{s/^enabled = no$/enabled = yes/;}" "$conf" || true
        }
        # auxiliary.conf's [auxiliary_modules] block is NOT the
        # section/enabled=yes pattern — each in-VM collector is a
        # sub-key like `curtain = yes`, `evtx = yes`, etc.
        flip_aux_module() {
            local key="$1"
            [ -f /etc/cape/auxiliary.conf ] || return 0
            sed -i "/^\\[auxiliary_modules\\]/,/^\\[/{s/^$key = no$/$key = yes/;}" /etc/cape/auxiliary.conf || true
        }
        # Inverse of flip_section_enabled: turn a section OFF (enabled
        # yes -> no).  Used to disable VirusTotal (ships keyless in our
        # deploy and errors every task).
        disable_section_enabled() {
            local conf="$1" section="$2"
            [ -f "$conf" ] || return 0
            sed -i "/^\\[$section\\]/,/^\\[/{s/^enabled = yes$/enabled = no/;}" "$conf" || true
        }
        # Inverse of flip_aux_module: turn an [auxiliary_modules] sub-key
        # OFF (yes -> no).  Used for the QEMU-only screenshot policy below.
        disable_aux_module() {
            local key="$1"
            [ -f /etc/cape/auxiliary.conf ] || return 0
            sed -i "/^\\[auxiliary_modules\\]/,/^\\[/{s/^$key = yes$/$key = no/;}" /etc/cape/auxiliary.conf || true
        }

        # cuckoo.conf — core engine settings.  cape-core ships the
        # upstream default (conf/default/cuckoo.conf.default ->
        # /etc/cape/cuckoo.conf) and the section-toggle helpers above
        # never touch it, so the operational values that actually make
        # behavioral analysis stream back on this fleet were silently
        # dropped by the deb pipeline (the retired mirror-live-host
        # mechanism used to carry them).  Restore them here.
        CUCKOO_CONF=/etc/cape/cuckoo.conf
        if [ -f "$CUCKOO_CONF" ]; then
            # [resultserver] ip -> 0.0.0.0 (bind ALL interfaces).  The
            # upstream default 192.168.1.1 is a foreign/absent subnet on
            # the nestedvirt host: the ResultServer binds to a dead
            # address, the guest analyzer can't reach it to stream its
            # behavioral logs, and the task dies with "Agent is likely
            # unresponsive" (hard timeout, ZERO captured — no procs, no
            # dump.pcap, no screenshots; a static/file task still works,
            # so it looks like a network bug but isn't).  0.0.0.0 makes
            # the per-VM resultserver_ip=192.168.100.1 entries that
            # clone-win11-vms.sh writes into kvm.conf reachable; the
            # upstream comment in the file mandates exactly this pairing.
            sed -i "/^\\[resultserver\\]/,/^\\[/{s/^ip = 192\\.168\\.1\\.1$/ip = 0.0.0.0/;}" \
                "$CUCKOO_CONF" || true

            # [resultserver] multiworker -> yes: run one ResultServer per
            # analysis VM, each bound to that VM's own resultserver_port
            # (21NN) as generated into kvm.conf.  REQUIRED with the
            # per-machine distinct ports — without it a single ResultServer
            # listens only on the global :2042 and every guest told to
            # reach :21NN hangs.  Upstream ships the key as
            # `multiworker = no`, so flip it in place; append only as a
            # fallback if a future default drops the key entirely.
            sed -i "/^\\[resultserver\\]/,/^\\[/{s/^multiworker = no$/multiworker = yes/;}" \
                "$CUCKOO_CONF" || true
            if ! grep -qE '^multiworker = ' "$CUCKOO_CONF"; then
                sed -i "/^\\[resultserver\\]/a multiworker = yes" "$CUCKOO_CONF" || true
            fi

            # [cuckoo] max_machines_count -> 24: concurrent-analysis cap =
            # the default single-node fleet size (24 linked clones).
            # Upstream default 10 would leave 14 of the 24 VMs idle.
            sed -i "/^\\[cuckoo\\]/,/^\\[/{s/^max_machines_count = 10$/max_machines_count = 24/;}" \
                "$CUCKOO_CONF" || true

            # [timeouts] default -> 180: default per-analysis timeout
            # (seconds) the reference host runs.
            sed -i "/^\\[timeouts\\]/,/^\\[/{s/^default = 200$/default = 180/;}" \
                "$CUCKOO_CONF" || true

            # [processing] dns_over_https -> on: resolve post-analysis DNS
            # via DoH (dns.google) instead of the system resolver.  Key is
            # absent from the upstream default, so append it under the
            # [processing] header (idempotent).
            if ! grep -qE '^dns_over_https = ' "$CUCKOO_CONF"; then
                sed -i "/^\\[processing\\]/a dns_over_https = on" "$CUCKOO_CONF" || true
            fi
        fi

        # web.conf — UI/feature toggles.  display_* sections drive the
        # analysis-list column visibility (ETW, CAPE YARA, ET/PT portal,
        # sigma signatures, authenticode, submitter) + the per-task
        # tab visibility on the report page.  All ship enabled=no
        # upstream; deployment policy turns them on.
        #
        # web_auth + guacamole + url_analysis + amsidump + the *_script
        # blocks gate feature visibility on the submission form.
        # expanded_dashboard + evtx_download are the new-style dashboard
        # widgets + the per-task EVTX download link.
        #
        # NOT flipped (intentionally hidden):
        #   - display_browser_martians + display_office_martians: the
        #     "Martians" columns add visual noise to the analyses-list
        #     table without surfacing analyst-actionable data.  Removed
        #     from the flip list 2026-05-22 after operator feedback on
        #     the reference host.  Re-enable case-by-case via the conffile if a
        #     tenant wants them.
        for s in url_analysis amsidump zipped_download pre_script \
                 during_script guacamole web_auth \
                 display_task_tags expanded_dashboard \
                 evtx_download display_etw display_cape_yara \
                 display_et_portal display_pt_portal \
                 display_authenticode display_submitter; do
            flip_section_enabled /etc/cape/web.conf "$s"
        done

        # api.conf — REST endpoint toggles for the file/URL/report
        # submission and search endpoints.  Default-off upstream so
        # the API surface is empty until an operator opts in.
        for s in download_file filereport staticextraction filecreate \
                 urlcreate fileview web_search; do
            flip_section_enabled /etc/cape/api.conf "$s"
        done

        # reporting.conf — analysis report backends.  mongodb is the
        # primary report sink; mitre + bingraph + jsondump add the
        # ATT&CK mapping, binary-graph visualizations, and the JSON
        # report sink consumed by the broker pipeline.
        for s in mongodb mitre bingraph jsondump; do
            flip_section_enabled /etc/cape/reporting.conf "$s"
        done

        # processing.conf — per-task analyzer modules.  amsi_etw +
        # network_etw drive the ETW-based script + network attribution
        # that the new web columns surface; decryptpcap renders dumped
        # TLS streams in reports; the others are core analysis
        # processors that ship `enabled = no` upstream but are required
        # for a useful report.
        #
        # sigma: runs Zircolite over the guest EVTX/Sysmon logs.  The
        # [sigma] section + producer module ship via cape-core/
        # cape-signatures; the engine via cape-zircolite (Depends:
        # above); the rule packs via cape-sigma-rules.  evtx (the aux
        # collector sigma needs) is already flipped on in the
        # [auxiliary_modules] block below.
        for s in suricata curtain sysmon analysisinfo decompression \
                 dumptls amsi behavior amsi_etw network_etw decryptpcap \
                 sigma deduplication; do
            flip_section_enabled /etc/cape/processing.conf "$s"
        done

        # processing.conf sub-keys the section-enable loop above can't
        # reach (they're not `enabled = no` — they're named keys inside
        # already-enabled sections).  [detections] clamav surfaces ClamAV
        # hits in the report's detections summary (the section is on but
        # clamav ships off); [behavior] network_map + [network]
        # process_map build the process<->network correlation the
        # reference host reports.  All pure-Python, no external dependency.
        if [ -f /etc/cape/processing.conf ]; then
            sed -i "/^\\[detections\\]/,/^\\[/{s/^clamav = no$/clamav = yes/;}" /etc/cape/processing.conf || true
            sed -i "/^\\[behavior\\]/,/^\\[/{s/^network_map = no$/network_map = yes/;}" /etc/cape/processing.conf || true
            sed -i "/^\\[network\\]/,/^\\[/{s/^process_map = no$/process_map = yes/;}" /etc/cape/processing.conf || true
        fi

        # Suricata: socket mode (not cli).  Our cape-suricata binary is
        # built --enable-unix-socket and cape-host-runtime ships+enables
        # the suricata.service daemon; CAPE talks to it over the unix
        # socket so the ~50K-rule ruleset loads ONCE instead of being
        # recompiled per task.  Upstream default is runmode=cli, which
        # the deb-baked AMI silently ran (caught 2026-06-03 vs the
        # the reference build host, which runs socket mode).  Pin the socket
        # path to the daemon's default (suricata --localstatedir=/var ->
        # /var/run/suricata/suricata-command.socket); the upstream
        # default socket_file is /tmp/... which would never match.
        if [ -f /etc/cape/processing.conf ]; then
            sed -i "/^\\[suricata\\]/,/^\\[/{s/^runmode = cli$/runmode = socket/;}" /etc/cape/processing.conf || true
            sed -i "/^\\[suricata\\]/,/^\\[/{s#^socket_file = .*#socket_file = /var/run/suricata/suricata-command.socket#;}" /etc/cape/processing.conf || true
        fi

        # VirusTotal: disable.  Ships enabled=yes upstream but our deploy
        # has no VT API key, so every task logs "VT: Request failed" and
        # the lookups are dead weight.  The reference host disables it
        # too.  Re-enable + supply a key per-tenant if VT enrichment is
        # wanted.
        disable_section_enabled /etc/cape/processing.conf virustotal

        # integrations.conf — per-format extractors.  flare_capa runs
        # Mandiant's capability detection over PE/ELF binaries; the
        # *_extract modules pull files out of installer / batch / VBE /
        # auto-IT / RarSFX / UPX archives.
        for s in msi_extract kixtart_extract vbe_extract batch_extract \
                 UnAutoIt_extract RarSFX_extract UPX_unpack flare_capa; do
            flip_section_enabled /etc/cape/integrations.conf "$s"
        done

        # auxiliary.conf — host-side sniffer + proxy sections.  Mitmdump
        # / PolarProxy / SSLProxy ship `enabled = no` upstream and need
        # explicit opt-in.  Mitmdump is the default per-task TLS
        # interception mechanism used by sslkeylogfile.
        for s in Mitmdump PolarProxy SSLProxy; do
            flip_section_enabled /etc/cape/auxiliary.conf "$s"
        done

        # auxiliary.conf [auxiliary_modules] — in-VM collector toggles.
        # These flags drive what runs INSIDE the analysis VM:
        #   curtain     — PowerShell script-block logging
        #   evtx        — Windows event-log forwarding
        #   sslkeylogfile — TLS premaster-secret capture for decryptpcap
        #   wmi_etw / dns_etw / amsi_etw / network_etw — ETW providers
        # wmi_etw + dns_etw require pywintrace in the guest; they
        # no-op cleanly if it's missing so flipping them is safe.
        for k in curtain evtx sslkeylogfile wmi_etw dns_etw amsi_etw \
                 network_etw; do
            flip_aux_module "$k"
        done

        # Screenshots: QEMU-only policy.  CAPE has two screenshot
        # mechanisms and we deliberately pick the host-side one:
        #
        #   in-guest  ([auxiliary_modules] screenshots_windows/_linux):
        #     the analyzer agent screenshots from INSIDE the VM via the
        #     OS API.  Detectable + blockable + spoofable by malware,
        #     dies if the agent is killed, misses pre-agent boot and
        #     post-crash/BSOD frames.
        #   host-side ([QemuScreenshots]): the cape host grabs the
        #     framebuffer via the libvirt domain-screenshot API
        #     (dom.screenshot(), NOT the raw qemu monitor — works
        #     natively under our libvirt-managed QEMU).  Invisible to
        #     and untamperable by the guest; same 1s change-detected
        #     cadence as the in-guest module.
        #
        # For a malware sandbox the host-side capture is the higher-
        # integrity source, so we disable the in-guest agent screenshots
        # and enable QemuScreenshots.  This is also the config-comment-
        # blessed combination ("screenshots_linux and screenshots_windows
        # must be disabled" to use QemuScreenshots).
        #
        # The post-deploy smoke gate's URL task exercises this — verify a
        # .png lands in storage/analyses/<id>/shots/ after deploy; if the
        # libvirt screenshot API ever regresses, shots/ goes empty and
        # the gap is visible on the next analysis.
        disable_aux_module screenshots_windows
        disable_aux_module screenshots_linux
        flip_section_enabled /etc/cape/auxiliary.conf QemuScreenshots

        # Sniffer interface — upstream default `virbr1` doesn't match
        # our nestedvirt cape-100 / virbr100 network.  Per-VM kvm.conf
        # entries already override for actual captures but the global
        # default should still be correct so tooling that reads it as
        # the authoritative source (e.g. cape/utils/sniffer.py for
        # ad-hoc captures) picks up the right interface.
        sed -i 's|^interface = virbr1$|interface = virbr100|' \
            /etc/cape/auxiliary.conf 2>/dev/null || true

        # Fleet policy: API rate-limiting OFF on every CAPE host.
        # CAPE's DRF SubscriptionRateThrottle is hardwired in web/settings.py
        # with no config switch (the [api] ratelimit flag is vestigial), so we
        # drop it from REST_FRAMEWORK via the documented local_settings.py
        # override seam (settings.py does `from .local_settings import *` after
        # REST_FRAMEWORK is built). Marker-guarded + append-only so it coexists
        # with any deploy-specific local_settings content (e.g. dev-bootstrap's
        # OIDC bits); removed again by postrm on purge.
        LS=/opt/CAPEv2/web/web/local_settings.py
        if [ -d /opt/CAPEv2/web/web ] && ! grep -q CAPE-HOST-CONFIG-THROTTLE-OFF "$LS" 2>/dev/null; then
            cat >> "$LS" <<'LSBLK'

# CAPE-HOST-CONFIG-THROTTLE-OFF-BEGIN
import sys as _throttle_sys
_throttle_s = _throttle_sys.modules.get("web.settings")
if _throttle_s is not None and getattr(_throttle_s, "REST_FRAMEWORK", None):
    _throttle_s.REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"] = []
# CAPE-HOST-CONFIG-THROTTLE-OFF-END
LSBLK
            chown cape:cape "$LS" 2>/dev/null || true
            chmod 0644 "$LS"
            echo "host-config: API throttling disabled via local_settings.py"
        fi

        echo "host-config: CAPE conffile section enables applied"
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
        systemctl disable --now cape-suricata-update.timer 2>/dev/null || true
        systemctl disable --now cape-threat-update.timer    2>/dev/null || true
        systemctl daemon-reload || true
        # Strip the throttle-off block we appended to local_settings.py.
        LS=/opt/CAPEv2/web/web/local_settings.py
        [ -f "$LS" ] && sed -i '/# CAPE-HOST-CONFIG-THROTTLE-OFF-BEGIN/,/# CAPE-HOST-CONFIG-THROTTLE-OFF-END/d' "$LS" 2>/dev/null || true
        ;;
esac
exit 0
POSTRM
chmod 0755 "$STAGE/DEBIAN/postrm"

# Source templates intentionally carry a non-routable placeholder for the
# threat-content base. Resolve it to THREAT_CONTENT_URL only in the staged
# package so this repository stays environment-neutral. Assets are flat, so
# the templated URLs are already bare (no CDN sub-path segments).
find "$STAGE" -type f -exec sed -i "s#https://apt.example.invalid#${THREAT_CONTENT_URL}#g" {} +
if grep -RqsF 'https://apt.example.invalid' "$STAGE"; then
    log "::error::unresolved apt repository placeholder in staged package"
    exit 1
fi

OUT_DEB="$OUT_DIR/cape-host-config_${PACKAGE_VERSION}_${ARCH}.deb"
log "Building $OUT_DEB"
dpkg-deb --root-owner-group --build "$STAGE" "$OUT_DEB"
ls -la "$OUT_DEB"
log "package complete"
