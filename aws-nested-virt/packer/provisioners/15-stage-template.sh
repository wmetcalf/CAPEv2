#!/usr/bin/env bash
# 15-stage-template.sh — drop the win11_seabios libvirt domain XML
# at the canonical persistent-domain path on the warm AMI.
#
# We deliberately do NOT call `virsh define` here. virsh define probes
# the emulator's KVM support, and the Packer build instance is an
# amazon-ebs builder without nested virtualization (no /dev/kvm).
# Define-time probe fails with:
#
#   error: unsupported configuration: Emulator '/usr/bin/qemu-system-x86_64'
#          does not support virt type 'kvm'
#
# Phase 2 (ami-bake-clone-phase.sh) runs on an m8i.8xlarge with nested
# virt enabled, where /dev/kvm exists; that's where the actual
# `virsh define` happens, just before clone-win11-vms.sh fans out 24
# linked clones.
#
# The XML is uploaded by a sibling `file` provisioner in cape-host.pkr.hcl
# (the `shell` provisioner with `script = ...` only uploads that one .sh,
# sibling files do not auto-upload). Default path matches the upload dest.
# Canonical copy lives in the aws-nested-virt IaC repo at
# terraform/nestedvirt/files/live-host/libvirt/win11_seabios.xml — keep
# the two in sync if the template hardware shape changes.

set -euo pipefail

echo "[$(date -Iseconds)] 15-stage-template: starting"

XML_SRC="${TEMPLATE_XML:-/tmp/win11_seabios.xml}"
XML_DST="/etc/libvirt/qemu/win11_seabios.xml"

if [[ ! -f "$XML_SRC" ]]; then
    echo "ERROR: $XML_SRC missing — sibling file provisioner must upload the template XML first" >&2
    exit 1
fi

# /etc/libvirt/qemu/ ships with libvirt-daemon-system (pulled in by
# cape-core's Depends). Double-check the dir exists with correct perms.
install -d -m 0755 -o root -g root /etc/libvirt/qemu

install -m 0644 -o root -g root "$XML_SRC" "$XML_DST"

ls -la "$XML_DST"

echo "[$(date -Iseconds)] 15-stage-template: staged at $XML_DST (Phase 2 will virsh define)"
