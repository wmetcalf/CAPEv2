#!/bin/bash
set -euo pipefail

NETWORK_DIR="/etc/libvirt/qemu/networks"
DOMAIN_DIR="/etc/libvirt/qemu"

restore_network() {
  local name="$1"
  local xml_path="${NETWORK_DIR}/${name}.xml"

  if [[ ! -f "${xml_path}" ]]; then
    return 0
  fi

  if ! virsh net-info "${name}" >/dev/null 2>&1; then
    virsh net-define "${xml_path}" >/dev/null
  fi

  virsh net-autostart "${name}" >/dev/null 2>&1 || true
  if ! virsh net-info "${name}" | grep -q "Active:.*yes"; then
    virsh net-start "${name}" >/dev/null
  fi

  virsh net-info "${name}" | grep -q "Active:.*yes"
}

restore_domain() {
  local xml_path="$1"
  local name

  name="$(basename "${xml_path}" .xml)"
  if virsh dominfo "${name}" >/dev/null 2>&1; then
    return 0
  fi

  virsh define "${xml_path}" >/dev/null
}

restore_network "cape-100"
restore_network "hostonly"

shopt -s nullglob
for xml_path in "${DOMAIN_DIR}"/win11_seabios*.xml; do
  restore_domain "${xml_path}"
done
shopt -u nullglob
