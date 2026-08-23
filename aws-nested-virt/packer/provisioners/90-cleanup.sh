#!/usr/bin/env bash
# 90-cleanup.sh — Pre-snapshot cleanup so the warm AMI boots clean on
# any host (no inherited build-instance identity, no stale logs).

set -euo pipefail

echo "[$(date -Iseconds)] 90-cleanup: starting"

# Apt cache & log noise.
apt-get -qq clean
apt-get -qq autoremove --purge -y || true
rm -rf /var/lib/apt/lists/*

# Old log files. Keep the directory structure so journald + cape services
# don't trip on missing dirs.
find /var/log -type f \( -name '*.log' -o -name '*.gz' -o -name '*.[0-9]' \) -delete || true
truncate -s 0 /var/log/cloud-init.log /var/log/cloud-init-output.log 2>/dev/null || true

# cloud-init re-runs on the derived instance. Without this it'll think
# it's already configured and skip first-boot logic.
cloud-init clean --logs --seed || true

# SSH host keys regenerate on first boot via openssh-server's postinst
# trigger; baking them into the AMI would mean every derived instance
# shares the same fingerprint.
rm -f /etc/ssh/ssh_host_*

# machine-id: regenerate on first boot. systemd's first-boot logic
# triggers when this is empty.
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id

# Bash history and any tmp files from the build user.
rm -f /home/ubuntu/.bash_history /root/.bash_history
rm -rf /tmp/* /var/tmp/* || true

# fstrim before snapshot so the EBS snapshot is leaner.
fstrim -av || true

sync

echo "[$(date -Iseconds)] 90-cleanup: done"
