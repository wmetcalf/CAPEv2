#!/usr/bin/env bash
# 00-install-cape.sh — Add the prod apt channel + install pinned cape-* debs.
#
# Mirrors terraform/test/layer4-host-smoke/userdata.sh's apt setup so the
# bake exercises the same unauthenticated, flat GitHub-Releases repo
# operators use.
#
# Required env (passed in by Packer):
#   APT_REPO_URL              flat GitHub-Releases apt repo base
#   APT_KEYRING_URL           ASCII-armored signing key URL
#   CAPE_CORE_VERSION         pinned version string (matches deb filename)
#   CAPE_SIGNATURES_VERSION
#   CAPE_QEMU_VERSION
#   CAPE_SURICATA_VERSION

set -euo pipefail

echo "[$(date -Iseconds)] 00-install-cape: starting"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

# Upgrade OS packages from the Ubuntu archive before installing
# cape-*.  The base AMI we boot from is whatever Canonical published
# at some past date — without this step every bake ships the kernel,
# openssl, libc, sshd, etc. that Canonical happened to ship then.
# For a malware-analysis sandbox host this is unacceptable: a host
# compromise via an unpatched OpenSSL or kernel privesc lets samples
# escape the analysis VMs.
#
# Flags:
#   -y                                yes-to-prompts (we're unattended)
#   -o Dpkg::Options::=--force-confdef
#   -o Dpkg::Options::=--force-confold
#                                     when an upgraded package wants
#                                     to change a conffile, keep the
#                                     existing one if we modified it,
#                                     otherwise take the maintainer's
#                                     new version.  DEBIAN_FRONTEND
#                                     handles debconf prompts; these
#                                     handle the separate dpkg
#                                     conffile diff dialog.
#
# `upgrade` (not `dist-upgrade`): conservative — won't install/remove
# packages, only updates versions of what's already there.  Kernel
# transitions that need a fresh package (e.g. linux-image-6.x → 6.y)
# don't apply on the deb-baked path because cape-host-runtime pins
# the kernel ABI; let the AMI base bump pick those up.
apt-get -y \
    -o Dpkg::Options::=--force-confdef \
    -o Dpkg::Options::=--force-confold \
    upgrade

# curl + gnupg are usually present on stock Ubuntu but we need the
# package install to be deterministic across base AMI updates.
#
# libsndio7.0: cape-qemu's qemu-system-x86_64 binary is linked against
# libsndio.so.7 but the cape-qemu .deb's hand-curated `Depends:` (in
# qemu-build/package.sh) historically omitted it. virsh define probes
# the emulator, fails with "cannot open shared object file" without
# libsndio7.0 present. Install it explicitly here as a belt-and-
# suspenders. Once a cape-qemu .deb built after the package.sh fix is
# the active dev/prod artifact, this can be dropped — the deb will pull
# libsndio7.0 transitively.
apt-get install -y -qq --no-install-recommends ca-certificates curl gnupg libsndio7.0 unzip

# AWS CLI v2.  Ubuntu 24.04 dropped the `awscli` apt package (it was
# v1.x anyway).  userdata for the baked AMI uses `aws s3 sync` to stage
# patches and `aws secretsmanager get-secret-value` to pull the CAPE
# admin credentials — both fail with command-not-found if awscli isn't
# present at first boot.  Install v2 via the canonical Amazon zip and
# bake it into the AMI so first-boot doesn't have to reach awscli.
# amazonaws.com itself.
#
# Mirrors install-nestedvirt.sh::ensure_aws_cli (the legacy bootstrap's
# equivalent), but at bake time so the deb-baked AMI ships ready-to-run.
if ! command -v aws >/dev/null 2>&1; then
    awstmp="$(mktemp -d)"
    curl -fsSL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip \
        -o "${awstmp}/awscliv2.zip"
    unzip -q "${awstmp}/awscliv2.zip" -d "${awstmp}"
    "${awstmp}/aws/install" --update
    rm -rf "${awstmp}"
fi
aws --version

# cape-mongodb depends on mongodb-org-server, which lives on
# repo.mongodb.org rather than the Ubuntu archive. Set up the
# upstream 8.0 source + signing key here, before the `apt-get install
# cape-*` step below, so apt can resolve mongodb-org-server when
# cape-mongodb pulls it in transitively. (We don't ship our own
# mongodb-org-server fork — cape-mongodb is a thin wrapper around
# the upstream binary plus a tuned systemd unit + masking glue.)
install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://pgp.mongodb.com/server-8.0.asc \
    | gpg --dearmor -o /etc/apt/keyrings/mongo.gpg --yes
chmod 0644 /etc/apt/keyrings/mongo.gpg
. /etc/os-release
cat > /etc/apt/sources.list.d/mongodb.list <<EOF_MONGO
deb [signed-by=/etc/apt/keyrings/mongo.gpg arch=amd64] https://repo.mongodb.org/apt/ubuntu ${VERSION_CODENAME}/mongodb-org/8.0 multiverse
EOF_MONGO

mkdir -p /usr/share/keyrings

# Pull the public signing key from the release, unauthenticated (public
# fork, no CDN gate). Streaming straight into gpg --dearmor avoids
# leaving an armored copy on disk.
curl -fsSL "${APT_KEYRING_URL}" \
    | gpg --dearmor -o /usr/share/keyrings/cape-rules.gpg
chmod 644 /usr/share/keyrings/cape-rules.gpg

# Apt source pinned to the prod (`main`) channel — Phase 2 bake never
# pulls from `main-dev`. The workflow asserts CAPE_CORE_VERSION is
# present in `main` before kicking off the bake. Flat GitHub-Releases
# repo: no dists/ tree, so suite/component collapse to `./`.
cat > /etc/apt/sources.list.d/cape-rules.list <<EOF_SOURCES
deb [signed-by=/usr/share/keyrings/cape-rules.gpg] ${APT_REPO_URL}/ ./
EOF_SOURCES

apt-get update -qq

# --force-confnew: when a deb's conffile collides with one already on
# disk (e.g. clamav-freshclam ships /etc/clamav/freshclam.conf, then
# cape-host-config overwrites it with the CDN-pointed version),
# dpkg's default behavior is to PROMPT — which in a packer/SSM
# non-interactive context drops to readline, can't read stdin, and
# aborts with "end of file on stdin at conffile prompt".  The result
# is the deb landing in `iU` (unpacked but not configured) state with
# files like /etc/suricata/update.yaml never materialized.
#
# Caught on a validation deploy — cape-host-config
# `iU`, cape-suricata-update.service crashloop'd looking for the
# missing /etc/suricata/update.yaml.  cape-deb-e2e's Layer 4 userdata
# already passes this flag; Phase 1 packer bake had been missing it.
APT_NONINTERACTIVE=(
    -o "Dpkg::Options::=--force-confnew"
    -o "Dpkg::Options::=--force-confdef"
)

# Pin install. apt picks the matching version; if the pin isn't in the
# repo the install fails and Packer aborts the bake — which is what we
# want, since Phase 2 must not produce a "blessed" AMI from a stale or
# missing version.
apt-get install -y -qq "${APT_NONINTERACTIVE[@]}" \
    "cape-core=${CAPE_CORE_VERSION}" \
    "cape-signatures=${CAPE_SIGNATURES_VERSION}" \
    "cape-qemu=${CAPE_QEMU_VERSION}" \
    "cape-suricata=${CAPE_SURICATA_VERSION}"

# Threat-content + host-config + host-runtime + mongo-tuning debs
# (unpinned — these release on a different cadence from cape-* core
# and we want the latest).  Missing packages aren't fatal here; an
# AMI baked before any of these workflows have published is still
# usable, the next bake picks them up.
#
# cape-mongodb pulls in mongodb-org-server + mongodb-org-shell from
# the mongodb.org apt source we configured above.  Default `apt-get
# install` honors Recommends so the upstream metapackage's recommended
# tooling (mongodb-org-mongos etc.) comes along too; --no-install-
# recommends keeps the bake lean and stops mongodb-org-mongos from
# claiming port 27018 alongside cape-mongodb's 27017.
apt-get install -y -qq --no-install-recommends "${APT_NONINTERACTIVE[@]}" \
    cape-yara-forge cape-sigma-rules cape-host-config cape-host-runtime \
    cape-mongodb cape-fakenet || true

# cape-libvirt installation is INTENTIONALLY DISABLED.
#
# cape-libvirt declares `Conflicts: libvirt-daemon-system, libvirt0,
# libvirt-daemon, libvirt-clients` so apt removes Ubuntu's libvirt
# stack as the first step of any cape-libvirt install attempt.  But
# cape-libvirt's hand-rolled Depends references pre-time_t64 lib
# names (libreadline8, libtirpc3, …) which Ubuntu noble has renamed
# to *t64 variants, so apt aborts after the removal.  Net result:
# the warm AMI ends up with NO libvirt at all and the Phase 2 clone
# script fails with "Unit file libvirtd.service does not exist."
# Caught on an earlier bake.
#
# Since cape-libvirt isn't load-bearing for the product (only
# enabling host-side QemuScreenshots, which we don't use — the
# in-VM screenshots_windows / screenshots_linux agents handle
# screenshots), skip it entirely until its Depends are regenerated
# correctly for noble.
#
# Followup: regenerate cape-libvirt Depends with dh_shlibdeps (or
# manually update to noble's t64 names) and re-enable here.

# Hold the cape-* packages so unattended-upgrades or operator-driven
# `apt-get upgrade` on the baked AMI won't drift the version away from
# what the AMI was tagged with. The first-boot userdata explicitly
# unholds before its own `apt-get upgrade` step.
apt-mark hold cape-core cape-signatures cape-qemu cape-suricata

# Sanity: every cape-* package is present at the pinned version.
for pkg in cape-core cape-signatures cape-qemu cape-suricata; do
    installed=$(dpkg-query -W -f='${Version}' "$pkg")
    echo "  $pkg: $installed"
done
# Threat-content packages — log presence, don't hard-fail (some may
# legitimately not be published yet on a fresh apt repo).
for pkg in cape-yara-forge cape-sigma-rules cape-host-config cape-host-runtime \
           cape-mongodb cape-fakenet; do
    installed=$(dpkg-query -W -f='${Version}' "$pkg" 2>/dev/null || echo MISSING)
    echo "  $pkg: $installed"
done

echo "[$(date -Iseconds)] 00-install-cape: done"
