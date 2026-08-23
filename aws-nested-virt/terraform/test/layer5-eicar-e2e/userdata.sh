#!/usr/bin/env bash
# Layer 5 — EICAR e2e bootstrap.
#
# Source AMI is a cape-host-base AMI from sandbox-code's Phase 2 ami-bake.
# This userdata overlays the dev-channel cape-* debs on top, runs the same
# clone-rebuild + service-restart flow nestedvirt-ami uses, syncs the CAPE
# admin user from Secrets Manager, and writes a bootstrap-ready marker.
#
# The actual EICAR analysis assertion is driven from outside via SSM —
# see scripts/run-layer-test.sh's Layer 5 path. We deliberately keep
# adapter-shaped assertions (sigs/score/disposition checks) on the runner
# rather than on-host so failures bubble up to GHA cleanly.

set -euo pipefail

exec > >(tee /var/log/layer5-userdata.log | logger -t layer5-userdata -s 2>/dev/console) 2>&1

echo "[$(date -Iseconds)] Layer 5 EICAR e2e bootstrap starting"

export DEBIAN_FRONTEND=noninteractive
export AWS_REGION="${aws_region}"
export AWS_DEFAULT_REGION="${aws_region}"

############################################################
# 1. Add the dev apt channel and overlay the cape-* versions under test.
############################################################

mkdir -p /usr/share/keyrings /etc/apt/sources.list.d /etc/apt/apt.conf.d

# Public fork: the repo is a flat GitHub Release (no suite/component
# subtree), fetched unauthenticated — no api-key header to configure.
curl -fsSL "${apt_repo_keyring_url}" \
    | gpg --dearmor -o /usr/share/keyrings/cape-rules.gpg

cat > /etc/apt/sources.list.d/cape-rules-dev.list <<EOF_SOURCES
deb [signed-by=/usr/share/keyrings/cape-rules.gpg] ${apt_repo_url}/ ./
EOF_SOURCES

apt-get update -qq

# The AMI bake holds cape-* via apt-mark to keep `apt-get upgrade` from
# drifting. Unhold so the overlay install can change versions.
apt-mark unhold cape-core cape-signatures cape-qemu cape-suricata 2>/dev/null || true

# `pkg=version` install fails (rather than pulling a different version)
# when the dev channel doesn't have the requested version yet. That's the
# test signal — Layer 5 only runs against versions actually published to dev.
# --force-conf{def,old} preserves operator-modified conffiles in /etc/cape/.
apt-get install -y -qq -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" \
    cape-core="${cape_core_version}" \
    cape-signatures="${cape_signatures_version}" \
    cape-qemu="${cape_qemu_version}" \
    cape-suricata="${cape_suricata_version}"

apt-mark hold cape-core cape-signatures cape-qemu cape-suricata

############################################################
# 2. Rebuild VM clones. AMI snapshots include CPU/memory state from the
#    bake instance, which isn't portable. Same logic nestedvirt-ami uses.
############################################################

echo "[$(date -Iseconds)] Stopping CAPE services for clone rebuild..."
for svc in cape cape-web cape-processor guac-web suricata cape-rooter cape-routing cape-firewall-isolation cape-fakenet-fix; do
  systemctl stop "$svc.service" 2>/dev/null || true
done

echo "[$(date -Iseconds)] Destroying stale clones..."
for vm in $(virsh list --name 2>/dev/null | grep win11_seabios_1); do
  virsh destroy "$vm" 2>/dev/null || true
done
for vm in $(virsh list --all --name 2>/dev/null | grep win11_seabios_1); do
  virsh undefine "$vm" --snapshots-metadata 2>/dev/null || true
  rm -f "/var/lib/libvirt/images/$${vm}.qcow2"
done

# clone-win11-vms.sh is shipped by the cape-core deb. Older AMIs may still
# have it under /opt/cape-nestedvirt/bootstrap/live-installer/ — fall back.
if [ -x /opt/CAPEv2/scripts/clone-win11-vms.sh ]; then
  CLONE_SCRIPT=/opt/CAPEv2/scripts/clone-win11-vms.sh
elif [ -x /opt/cape-nestedvirt/bootstrap/live-installer/clone-win11-vms.sh ]; then
  CLONE_SCRIPT=/opt/cape-nestedvirt/bootstrap/live-installer/clone-win11-vms.sh
else
  echo "[$(date -Iseconds)] FATAL: no clone-win11-vms.sh on this AMI" >&2
  echo '{"fail": 1, "stage": "clone-rebuild", "reason": "clone-win11-vms.sh not found"}' \
    > /var/log/layer5-bootstrap-result.json
  exit 1
fi

echo "[$(date -Iseconds)] Rebuilding 24 clones via $CLONE_SCRIPT..."
bash "$CLONE_SCRIPT" win11_seabios 101 124 192.168.100 linked

############################################################
# 3. Service restart, in the same order nestedvirt-ami uses.
############################################################

echo "[$(date -Iseconds)] Restarting base services..."
systemctl restart cape-libvirt-restore.service 2>/dev/null || true
systemctl restart openvpn-pia.service 2>/dev/null || true
systemctl restart cape-firewall-isolation.service 2>/dev/null || true
systemctl restart cape-fakenet-fix.service 2>/dev/null || true
systemctl restart cape-routing.service 2>/dev/null || true
systemctl restart cape-rooter.service 2>/dev/null || true
systemctl restart suricata.service 2>/dev/null || true

echo "[$(date -Iseconds)] Restarting CAPE services..."
systemctl restart cape.service cape-web.service cape-processor.service guac-web.service || true

############################################################
# 4. Sync CAPE admin from Secrets Manager — the EICAR driver authenticates
#    against /apiv2/ with the token tied to this user.
############################################################

CAPE_ADMIN_SECRET_ARN="${cape_admin_secret_arn}"
if [[ -n "$${CAPE_ADMIN_SECRET_ARN}" ]]; then
  CAPE_ADMIN_SECRET_JSON=$(aws secretsmanager get-secret-value \
    --secret-id "$${CAPE_ADMIN_SECRET_ARN}" \
    --query SecretString --output text)

  sudo -u cape -H \
    CAPE_ADMIN_SECRET_JSON="$${CAPE_ADMIN_SECRET_JSON}" \
    bash -c "cd /opt/CAPEv2/web && /etc/poetry/bin/poetry run python manage.py shell" <<'PYEOF'
import json, os
from django.contrib.auth import get_user_model
data = json.loads(os.environ["CAPE_ADMIN_SECRET_JSON"])
U = get_user_model()
u, _ = U.objects.get_or_create(
    username=data["username"],
    defaults={"email": data.get("email", ""), "is_superuser": True, "is_staff": True},
)
u.is_superuser = True
u.is_staff = True
u.email = data.get("email", u.email)
u.set_password(data["password"])
u.save()
print(f"admin user provisioned: {u.username}")
PYEOF
  unset CAPE_ADMIN_SECRET_JSON
fi

############################################################
# 5. Wait for cape-web to actually answer requests, then write the
#    bootstrap-ready marker. Allow 5 min — clone rebuild + service
#    startup is the slow part.
############################################################

echo "[$(date -Iseconds)] Waiting for cape-web /apiv2/tasks/list/ to return 200..."
ready=0
for i in $(seq 1 60); do
  status=$(curl -s -o /dev/null -w "%%{http_code}" http://127.0.0.1:8000/apiv2/tasks/list/ 2>/dev/null || echo 000)
  if [[ "$status" == "200" ]]; then
    ready=1
    break
  fi
  sleep 5
done

############################################################
# 6. Pinned-version assertions — confirm the dev-channel overlay actually
#    landed before we tell the driver "go ahead with EICAR".
############################################################

RESULT=/var/log/layer5-bootstrap-result.json
fail=0
[[ "$ready" -eq 1 ]] || fail=1

declare -A expect=(
    [cape-core]="${cape_core_version}"
    [cape-signatures]="${cape_signatures_version}"
    [cape-qemu]="${cape_qemu_version}"
    [cape-suricata]="${cape_suricata_version}"
)

{
    echo "{"
    echo "  \"timestamp\": \"$(date -Iseconds)\","
    echo "  \"cape_web_ready\": $ready,"
    echo "  \"versions\": {"
    sep=""
    for pkg in cape-core cape-signatures cape-qemu cape-suricata; do
        installed=$(dpkg-query -W -f='$${Version}' "$pkg" 2>/dev/null || echo "")
        echo "    $${sep}\"$pkg\": {\"installed\": \"$installed\", \"expected\": \"$${expect[$pkg]}\"}"
        [[ "$installed" == "$${expect[$pkg]}" ]] || fail=1
        sep=","
    done
    echo "  },"
    echo "  \"vms_running\": $(virsh list --name 2>/dev/null | grep -c '^win11_seabios_1' || echo 0),"
    echo "  \"fail\": $fail"
    echo "}"
} > "$RESULT"

cat "$RESULT"

echo "[$(date -Iseconds)] Layer 5 bootstrap complete (fail=$fail). Driver takes over from here for EICAR submission."
