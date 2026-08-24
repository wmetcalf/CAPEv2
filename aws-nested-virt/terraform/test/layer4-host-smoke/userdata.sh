#!/usr/bin/env bash
# Layer 4 — host smoke test bootstrap.
#
# Installs the cape-* debs at the pinned versions from the dev apt
# channel, runs structural assertions, writes a result marker file
# the driver script polls for via SSM. NO nested-virt, NO VM clones —
# this layer only validates the package install path.

set -uo pipefail
# Note: NOT `set -e`. We want EVERY failure (curl fail, apt-update,
# apt-install of pinned versions) to be recorded in /var/log/layer4-result.json
# rather than abort silently before the marker gets written. The driver
# polls for the marker over SSM and infers timeout-without-marker as
# "something exploded so badly userdata couldn't even tell us." Set -e
# turns that into the only failure mode; we'd rather get a structured
# JSON the driver can surface.

RESULT=/var/log/layer4-result.json

# Always write a failure marker on exit unless explicitly overwritten
# below. Captures `set -u` derefs of unbound vars too.
write_failure_marker() {
    local rc=$?
    if [ ! -f "$RESULT" ]; then
        cat > "$RESULT" <<EOF
{
  "timestamp": "$(date -Iseconds 2>/dev/null || echo unknown)",
  "fail": 1,
  "stage": "userdata-aborted-before-assertions",
  "exit_code": $rc,
  "log_tail": $(tail -50 /var/log/layer4-userdata.log 2>/dev/null | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo '"unable to capture log_tail"')
}
EOF
    fi
}
trap write_failure_marker EXIT

exec > >(tee /var/log/layer4-userdata.log | logger -t layer4-userdata -s 2>/dev/console) 2>&1

echo "[$(date -Iseconds)] Layer 4 host smoke test starting"

export DEBIAN_FRONTEND=noninteractive

# 1. Install the apt repo + keyring. Public fork: the repo is a flat
# GitHub Release (no suite/component subtree), fetched unauthenticated
# — no api-key header to configure.
mkdir -p /usr/share/keyrings /etc/apt/sources.list.d /etc/apt/apt.conf.d
curl -fsSL "${apt_repo_keyring_url}" \
    | gpg --dearmor -o /usr/share/keyrings/cape-rules.gpg

cat > /etc/apt/sources.list.d/cape-rules.list <<EOF_SOURCES
deb [signed-by=/usr/share/keyrings/cape-rules.gpg] ${apt_repo_url}/ ./
EOF_SOURCES

apt-get update -qq

# --force-confnew tells dpkg to take the maintainer's new version
# whenever a conffile collides with one already on disk (e.g.
# clamav-freshclam ships /etc/clamav/freshclam.conf, then
# cape-host-config wants to overwrite it with the CDN
# pointed version).  Without this, dpkg ASKS at the conffile
# prompt regardless of DEBIAN_FRONTEND=noninteractive — debconf
# noninteractive only governs debconf-managed prompts, not the
# built-in conffile diff dialog — and noninteractive runs read
# end-of-file on stdin and bail.  A cape-deb-e2e run
# caught this on cape-host-config's freshclam.conf prompt.
APT_NONINTERACTIVE=(
    -o "Dpkg::Options::=--force-confnew"
)

# 2. Install pinned versions. apt's `pkg=version` syntax fails the
#    install (rather than picking a different version) if the dev
#    channel doesn't have the version yet — that's the test signal.
apt-get install -y -qq --no-install-recommends "$${APT_NONINTERACTIVE[@]}" \
    cape-core="${cape_core_version}" \
    cape-signatures="${cape_signatures_version}" \
    cape-qemu="${cape_qemu_version}" \
    cape-suricata="${cape_suricata_version}"

# 2b. Install cape-yara-forge + cape-sigma-rules unpinned. These
#     ruleset packages release on a different cadence than cape-*
#     core; treating them as "always pull latest available" is the
#     simplest contract. Missing-package is recorded as a fail below
#     rather than an apt-get hard error.
apt-get install -y -qq --no-install-recommends "$${APT_NONINTERACTIVE[@]}" cape-yara-forge || true
apt-get install -y -qq --no-install-recommends "$${APT_NONINTERACTIVE[@]}" cape-sigma-rules || true
apt-get install -y -qq --no-install-recommends "$${APT_NONINTERACTIVE[@]}" cape-host-config || true

# 2c. Suricata rules don't have a deb — they're CDN-only (high-cadence
#     content suits suricata-update, not apt). The Layer 4 assertion
#     for that surface is a CDN reachability check below, not an apt
#     install here.

# 3. Structural assertions — write pass/fail to a known marker.
RESULT=/var/log/layer4-result.json
fail=0
{
    echo "{"
    echo "  \"timestamp\": \"$(date -Iseconds)\","
    echo "  \"checks\": ["

    declare -A expect=(
        [cape-core]="${cape_core_version}"
        [cape-signatures]="${cape_signatures_version}"
        [cape-qemu]="${cape_qemu_version}"
        [cape-suricata]="${cape_suricata_version}"
    )

    sep=""
    for pkg in cape-core cape-signatures cape-qemu cape-suricata; do
        installed=$(dpkg-query -W -f='$${Version}' "$pkg" 2>/dev/null || echo "")
        if [[ "$installed" == "$${expect[$pkg]}" ]]; then
            echo "    $${sep}{\"name\": \"$pkg-version\", \"pass\": true, \"actual\": \"$installed\"}"
        else
            echo "    $${sep}{\"name\": \"$pkg-version\", \"pass\": false, \"actual\": \"$installed\", \"expected\": \"$${expect[$pkg]}\"}"
            fail=1
        fi
        sep=","
    done

    # dpkg -V verifies installed file checksums match the package manifest.
    for pkg in cape-core cape-signatures cape-qemu cape-suricata; do
        if dpkg -V "$pkg" >/dev/null 2>&1; then
            echo "    ,{\"name\": \"dpkg-V-$pkg\", \"pass\": true}"
        else
            verr=$(dpkg -V "$pkg" 2>&1 | head -3 | tr '\n' '; ' || true)
            echo "    ,{\"name\": \"dpkg-V-$pkg\", \"pass\": false, \"err\": \"$verr\"}"
            fail=1
        fi
    done

    # systemd unit files registered (don't START them — Layer 4
    # doesn't run the actual service stack)
    for unit in cape cape-web cape-rooter cape-processor guac-web; do
        if systemctl cat "$unit.service" >/dev/null 2>&1; then
            echo "    ,{\"name\": \"unit-$unit\", \"pass\": true}"
        else
            echo "    ,{\"name\": \"unit-$unit\", \"pass\": false}"
            fail=1
        fi
    done

    # cape-yara-forge installed + ruleset landed at the expected path.
    # Pin-less install: just check the package is present and the .yar
    # is non-trivial in size.
    yforge_installed=$(dpkg-query -W -f='$${Version}' cape-yara-forge 2>/dev/null || echo "")
    if [[ -n "$yforge_installed" ]]; then
        echo "    ,{\"name\": \"cape-yara-forge-installed\", \"pass\": true, \"actual\": \"$yforge_installed\"}"
    else
        echo "    ,{\"name\": \"cape-yara-forge-installed\", \"pass\": false, \"actual\": \"\"}"
        fail=1
    fi
    yforge_path=/opt/CAPEv2/data/yara/binaries/yara-forge-extended.yar
    if [[ -s "$yforge_path" ]]; then
        yforge_size=$(stat -c %s "$yforge_path" 2>/dev/null || echo 0)
        if [[ "$yforge_size" -gt 100000 ]]; then
            echo "    ,{\"name\": \"yara-forge-rules\", \"pass\": true, \"size\": $yforge_size}"
        else
            echo "    ,{\"name\": \"yara-forge-rules\", \"pass\": false, \"size\": $yforge_size}"
            fail=1
        fi
    else
        echo "    ,{\"name\": \"yara-forge-rules\", \"pass\": false, \"size\": 0}"
        fail=1
    fi

    # cape-sigma-rules deb + content
    sigma_installed=$(dpkg-query -W -f='$${Version}' cape-sigma-rules 2>/dev/null || echo "")
    if [[ -n "$sigma_installed" ]]; then
        echo "    ,{\"name\": \"cape-sigma-rules-installed\", \"pass\": true, \"actual\": \"$sigma_installed\"}"
    else
        echo "    ,{\"name\": \"cape-sigma-rules-installed\", \"pass\": false, \"actual\": \"\"}"
        fail=1
    fi
    # rules_windows_merged.json is the biggest pack — the canonical
    # "did the deb actually drop the content" probe.
    sigma_path=/opt/CAPEv2/data/sigma/rules_windows_merged.json
    if [[ -s "$sigma_path" ]]; then
        sigma_size=$(stat -c %s "$sigma_path" 2>/dev/null || echo 0)
        if [[ "$sigma_size" -gt 1000000 ]]; then
            echo "    ,{\"name\": \"sigma-rules\", \"pass\": true, \"size\": $sigma_size}"
        else
            echo "    ,{\"name\": \"sigma-rules\", \"pass\": false, \"size\": $sigma_size}"
            fail=1
        fi
    else
        echo "    ,{\"name\": \"sigma-rules\", \"pass\": false, \"size\": 0}"
        fail=1
    fi

    # Suricata rules reachability via the .tar.gz.md5 sidecar on the
    # threat-content release (the same sidecar suricata-update fetches
    # first on a real client refresh). Bare-named asset — no path segments.
    srules_md5_url="${threat_content_url}/cape-rules.tar.gz.md5"
    srules_md5=$(curl -fsSL "$${srules_md5_url}" 2>/dev/null || echo "")
    if [[ -n "$srules_md5" && "$${#srules_md5}" -ge 32 ]]; then
        echo "    ,{\"name\": \"suricata-rules-cdn\", \"pass\": true, \"md5\": \"$srules_md5\"}"
    else
        echo "    ,{\"name\": \"suricata-rules-cdn\", \"pass\": false, \"md5\": \"\"}"
        fail=1
    fi

    # cape-host-config installed + threat-content-pointed config files landed.
    hconf_installed=$(dpkg-query -W -f='$${Version}' cape-host-config 2>/dev/null || echo "")
    if [[ -n "$hconf_installed" ]]; then
        echo "    ,{\"name\": \"cape-host-config-installed\", \"pass\": true, \"actual\": \"$hconf_installed\"}"
    else
        echo "    ,{\"name\": \"cape-host-config-installed\", \"pass\": false, \"actual\": \"\"}"
        fail=1
    fi
    # freshclam.conf now points at the threat-content release for the
    # 3rd-party feeds (bare-named assets, e.g. <url>/junk.ndb).
    if grep -Fq "${threat_content_url}/junk.ndb" /etc/clamav/freshclam.conf 2>/dev/null; then
        echo "    ,{\"name\": \"freshclam-conf-repo\", \"pass\": true}"
    else
        echo "    ,{\"name\": \"freshclam-conf-repo\", \"pass\": false}"
        fail=1
    fi
    # suricata-update yaml points at the threat-content release's bare
    # sources index (<url>/index.yaml).
    if grep -Fq "${threat_content_url}/index.yaml" /etc/suricata/update.yaml 2>/dev/null; then
        echo "    ,{\"name\": \"suricata-update-conf-repo\", \"pass\": true}"
    else
        echo "    ,{\"name\": \"suricata-update-conf-repo\", \"pass\": false}"
        fail=1
    fi

    # ClamAV 3rd-party feeds: probe one of the well-known files our
    # mirror serves (junk.ndb is from sanesecurity). HEAD gives us a
    # cheap "is this URL reachable + non-empty" signal without
    # downloading the body. Bare-named asset on the threat-content
    # release (3rd-party-only; Cisco's standard CVDs stay on
    # database.clamav.net, not mirrored).
    clamav_url="${threat_content_url}/junk.ndb"
    clamav_size=$(curl -fsSIL "$${clamav_url}" 2>/dev/null \
        | awk 'tolower($0) ~ /^content-length:/ {gsub(/\r/,""); v=$2} END{print v}')
    if [[ -n "$clamav_size" && "$clamav_size" -gt 1000 ]]; then
        echo "    ,{\"name\": \"clamav-extra-mirror-cdn\", \"pass\": true, \"size\": $clamav_size}"
    else
        echo "    ,{\"name\": \"clamav-extra-mirror-cdn\", \"pass\": false, \"size\": \"$${clamav_size:-0}\"}"
        fail=1
    fi

    # Bundled venv resolves CAPE imports
    # The venv is at /opt/CAPEv2/ (root), not /opt/CAPEv2/.venv —
    # dh_virtualenv with --install-suffix=CAPEv2 puts the venv directly
    # at $DH_VIRTUALENV_INSTALL_ROOT/$INSTALL_SUFFIX = /opt/CAPEv2.
    # Earlier we wrote the .pth at site-packages/cape.pth so /opt/CAPEv2
    # is on sys.path; this check verifies the real CAPE source files
    # (shipped via cape-core.install at /opt/CAPEv2/{lib,web,modules})
    # actually resolve as Python imports.
    if /opt/CAPEv2/bin/python -c "import lib.cuckoo, web, modules" 2>/dev/null; then
        echo "    ,{\"name\": \"venv-imports\", \"pass\": true}"
    else
        echo "    ,{\"name\": \"venv-imports\", \"pass\": false}"
        fail=1
    fi

    # libvirt-python binding importable in venv.
    # lib/cuckoo/core/startup.py:331 does `import libvirt` during
    # init_modules()/check_snapshot_state() — cape-processor and
    # cape.service (cuckoo.py) both crash at startup if this fails.
    # First missed because L4 only ran `systemctl cat` against units,
    # never exercised an actual python import that the services do —
    # a deploy on the reference host caught it in production via
    # cape-processor's CuckooStartupError("'libvirt-python' library
    # is required for KVM/QEMU machinery but could not be imported").
    if /opt/CAPEv2/bin/python -c "import libvirt" 2>/dev/null; then
        echo "    ,{\"name\": \"venv-import-libvirt\", \"pass\": true}"
    else
        echo "    ,{\"name\": \"venv-import-libvirt\", \"pass\": false}"
        fail=1
    fi

    # cape user can mkdir under /opt/CAPEv2/data/.
    # cuckoo.py creates /opt/CAPEv2/data/feeds at startup; without a
    # postinst chown, /opt/CAPEv2/data is root:root and the runtime
    # user can't create subdirs — cape.service exits 1 with
    # "CuckooStartupError: Unable to create folder:
    # /opt/CAPEv2/data/feeds".  Probe the same operation here so L4
    # fails before the deb gets promoted.  .l4_smoke is a sentinel
    # name (leading dot keeps it out of legitimate enum loops in
    # data/).  Idempotent: removed immediately after the probe.
    if sudo -u cape -H bash -c '
            set -e
            mkdir -p /opt/CAPEv2/data/.l4_smoke
            rmdir /opt/CAPEv2/data/.l4_smoke
        ' 2>/dev/null; then
        echo "    ,{\"name\": \"cape-writable-data-dir\", \"pass\": true}"
    else
        echo "    ,{\"name\": \"cape-writable-data-dir\", \"pass\": false}"
        fail=1
    fi

    # qemu native AIO support + all shared libs resolve.
    # cape-qemu must be compiled with --enable-linux-aio so libvirt
    # clone XML's <driver aio="native"/> works.  Without it, virsh
    # start errors with "aio=native was specified, but is not
    # supported in this build" — leaving all 24 clones unstartable.
    # Additionally, every .so the binary links against has to be
    # installed on the runtime host; cape-qemu's hand-rolled deb
    # (qemu-build/package.sh, no dh_shlibdeps) enumerates Depends:
    # manually so a missing entry surfaces only at runtime as
    # "error while loading shared libraries: <lib>: cannot open
    # shared object file".  ami-bake Phase 2 at sha 62385fe3f caught
    # exactly that for liburing.so.2 because --enable-linux-io-uring
    # got added without the corresponding liburing2 dep.
    #
    # Probe both invariants:
    #   1. libaio is one of the linked deps (proxy for --enable-linux-aio)
    #   2. ldd reports no "not found" lines (every linked lib is
    #      actually present on the runtime host)
    qemu_ldd=$(ldd /usr/bin/qemu-system-x86_64 2>/dev/null || true)
    qemu_missing=$(echo "$qemu_ldd" | grep 'not found' || true)
    qemu_has_libaio=$(echo "$qemu_ldd" | grep libaio || true)
    if [[ -n "$qemu_has_libaio" && -z "$qemu_missing" ]]; then
        echo "    ,{\"name\": \"qemu-linux-aio\", \"pass\": true}"
    else
        qemu_missing_json=$(echo "$qemu_missing" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo '""')
        echo "    ,{\"name\": \"qemu-linux-aio\", \"pass\": false, \"missing\": $qemu_missing_json, \"libaio_linked\": \"$qemu_has_libaio\"}"
        fail=1
    fi

    # cape-qemu deb shipped en-us keymap.  Win11 template XML uses
    # <graphics type='vnc'/> — qemu errors at attach with
    #   -vnc 127.0.0.1:0: could not read keymap file: 'en-us'
    # when /usr/share/qemu/keymaps/en-us is absent.  Caught on a
    # deploy on the reference host: cape-qemu.install enumerated
    # bios/vgabios/efi/pxe/etc but not keymaps/, so qemu-build's
    # `make install` populated DESTDIR/usr/share/qemu/keymaps/ but
    # package.sh never copied it into the deb.  Same class as the
    # libaio/liburing missing from Depends — hand-rolled manifest
    # missed a file the build produced.
    if [[ -f /usr/share/qemu/keymaps/en-us ]]; then
        echo "    ,{\"name\": \"qemu-keymap-en-us\", \"pass\": true}"
    else
        echo "    ,{\"name\": \"qemu-keymap-en-us\", \"pass\": false, \"path\": \"/usr/share/qemu/keymaps/en-us\"}"
        fail=1
    fi

    echo "  ],"
    echo "  \"fail\": $fail"
    echo "}"
# Write to a temp file then atomically rename onto $RESULT.
# The driver polls $RESULT via `sudo cat` over SSM every 30s; without
# the rename it could land mid-write of the `{ ... } > $RESULT`
# redirection above, see partial JSON, fail to parse, and report
# spurious bootstrap-failed.  A cape-deb-e2e run caught
# the race — all 24 asserts reported pass=true but the driver saw a
# truncated JSON during one poll iteration and exited 1 before the
# next poll saw the full file.  `mv` on the same filesystem is
# atomic (rename(2)) so the driver either sees no file or the
# complete file.
} > "$${RESULT}.tmp"
mv -f "$${RESULT}.tmp" "$RESULT"

cat "$RESULT"

if [[ $fail -ne 0 ]]; then
    echo "[$(date -Iseconds)] Layer 4 FAILED — see $RESULT"
    exit 1
fi

echo "[$(date -Iseconds)] Layer 4 PASSED"
