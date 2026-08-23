#!/usr/bin/env bash
# 10-stage-qcow2.sh — Pull the win11_seabios.qcow2 template into
# /var/lib/libvirt/images/ so Phase 2's clone-win11-vms.sh has it
# locally. Skipping the download in Phase 2 saves ~30 GB of network
# transfer on every AMI bake.
#
# Required env:
#   QCOW2_S3_BUCKET   bucket holding the template
#   QCOW2_S3_KEY      object key (e.g., images/win11_seabios.qcow2)
#   BASE_VM_NAME      template VM name (used for the on-disk filename)

set -euo pipefail

echo "[$(date -Iseconds)] 10-stage-qcow2: starting"

# Ubuntu 24.04 dropped the legacy `awscli` apt package (Python 2-era).
# Install AWS CLI v2 directly from amazon if missing — works on stock
# Ubuntu Noble + downstream AMIs alike.
if ! command -v aws >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
        unzip ca-certificates curl
    tmp=$(mktemp -d)
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "$tmp/awscli.zip"
    unzip -q "$tmp/awscli.zip" -d "$tmp"
    "$tmp/aws/install" --update
    rm -rf "$tmp"
fi

mkdir -p /var/lib/libvirt/images
chown root:libvirt /var/lib/libvirt/images
chmod 0775 /var/lib/libvirt/images

target="/var/lib/libvirt/images/${BASE_VM_NAME}.qcow2"

aws s3 cp \
    "s3://${QCOW2_S3_BUCKET}/${QCOW2_S3_KEY}" \
    "$target" \
    --no-progress

# Set permissions libvirt expects so the qemu user can read the
# template at clone time.
chown libvirt-qemu:libvirt "$target"
chmod 0644 "$target"

# Stamp the staged qcow2 hash for traceability — Phase 2 logs this for
# audit, and operators can compare against the upstream S3 object's
# ETag.
sha256sum "$target" > "${target}.sha256"
chown libvirt-qemu:libvirt "${target}.sha256"

ls -la "$target" "${target}.sha256"

echo "[$(date -Iseconds)] 10-stage-qcow2: done"
