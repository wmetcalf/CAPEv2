#!/usr/bin/env bash
# ami-bake-clone-phase.sh — Phase 2 of the cape-host AMI bake.
#
# Inputs (env or flags):
#   --warm-ami-id        AMI built by Packer Phase 1 (cape-host-warm-*)
#   --subnet-id          subnet to launch in
#   --security-group-id  SG with at least SSH from the runner CIDR
#   --instance-profile   IAM profile granting ssm:* to the build host
#   --key-pair-name      key for SSH (we use SSM as primary; SSH is fallback)
#   --cape-core-version
#   --cape-signatures-version
#   --cape-qemu-version
#   --cape-suricata-version
#   --num-clones                  default 24
#   --vm-network-base             default 192.168.100
#   --base-vm-name                default win11_seabios
#   --vm-warmup-seconds           default 900
#   --instance-type               default m8i.8xlarge
#   --region                      default us-east-1
#   --previous-ami-id             optional; tagged Retention=superseded if set
#   --os-deb-hashes-digest        optional; 64-char sha256 of the
#                                 sorted-JSON dict of OS-affecting
#                                 cape-* deb sha256s.  Tagged on the
#                                 produced AMI as OsDebHashes so the
#                                 next apt-publish can skip-ami-bake
#                                 when the OS-affecting deb set is
#                                 byte-identical.
#   [--keep-on-fail]
#
# Output (stdout, last line): the new AMI id.
#
# Why this isn't in Packer: the amazon-ebs builder doesn't expose
# CpuOptions.NestedVirtualization, and the clone+snapshot phase needs
# nested virt to actually boot Windows guests during the bake.

set -euo pipefail

# ---- arg parsing ----------------------------------------------------------

WARM_AMI_ID=""
SUBNET_ID=""
SG_ID=""
INSTANCE_PROFILE=""
KEY_PAIR_NAME=""
CAPE_CORE_VERSION=""
CAPE_SIGNATURES_VERSION=""
CAPE_QEMU_VERSION=""
CAPE_SURICATA_VERSION=""
NUM_CLONES=24
VM_NETWORK_BASE=192.168.100
BASE_VM_NAME=win11_seabios
# Budget for the inner clone-win11-vms.sh on the bake host.  Name is
# historical ("warmup") but the value bounds the whole clone-phase:
# staggered VM starts + final wait + snapshot creation + shutdown.
# Combined with the 1800s slack below, this is the SSM-command
# deadline.
#
# Sizing history:
#   900s  → fired at 45 min on an early 60s linear stagger
#   1800s → fired at 60 min on the batched boot — clone phase ran
#           past the deadline because sequential snapshot-create-as
#           on 24 running 4GB-RAM Windows VMs takes 12-24 min on top
#           of the 30-min boot phase
#   2700s → with parallelized snapshots (see clone-win11-vms.sh), the
#           inner script is now ~32 min wall.  2700+1800 = 75 min
#           total budget leaves comfortable headroom for retry slack
#           and a slow EBS shutdown phase.
VM_WARMUP_SECONDS=2700
INSTANCE_TYPE=m8i.8xlarge
REGION=us-east-1
PREVIOUS_AMI_ID=""
# 64-char sha256 of the sorted-JSON dict of OS-affecting cape-* deb
# sha256s, computed in apt-publish.yml.  Stored as the OsDebHashes AMI
# tag so the next apt-publish (prod) can skip ami-bake when the deb
# set is byte-identical to this AMI's.  Empty = no skip-data, next
# bake fires unconditionally (fail-safe).
OS_DEB_HASHES_DIGEST=""
KEEP_ON_FAIL=0

while [ $# -gt 0 ]; do
    case "$1" in
        --warm-ami-id)              WARM_AMI_ID="$2"; shift 2;;
        --subnet-id)                SUBNET_ID="$2"; shift 2;;
        --security-group-id)        SG_ID="$2"; shift 2;;
        --instance-profile)         INSTANCE_PROFILE="$2"; shift 2;;
        --key-pair-name)            KEY_PAIR_NAME="$2"; shift 2;;
        --cape-core-version)        CAPE_CORE_VERSION="$2"; shift 2;;
        --cape-signatures-version)  CAPE_SIGNATURES_VERSION="$2"; shift 2;;
        --cape-qemu-version)        CAPE_QEMU_VERSION="$2"; shift 2;;
        --cape-suricata-version)    CAPE_SURICATA_VERSION="$2"; shift 2;;
        --num-clones)               NUM_CLONES="$2"; shift 2;;
        --vm-network-base)          VM_NETWORK_BASE="$2"; shift 2;;
        --base-vm-name)             BASE_VM_NAME="$2"; shift 2;;
        --vm-warmup-seconds)        VM_WARMUP_SECONDS="$2"; shift 2;;
        --instance-type)            INSTANCE_TYPE="$2"; shift 2;;
        --region)                   REGION="$2"; shift 2;;
        --previous-ami-id)          PREVIOUS_AMI_ID="$2"; shift 2;;
        --os-deb-hashes-digest)     OS_DEB_HASHES_DIGEST="$2"; shift 2;;
        --keep-on-fail)             KEEP_ON_FAIL=1; shift;;
        *) echo "::error::unknown arg: $1" >&2; exit 2;;
    esac
done

require() {
    local name="$1" value="$2"
    [ -n "$value" ] || { echo "::error::missing required arg --${name//_/-}"; exit 2; }
}
require warm_ami_id "$WARM_AMI_ID"
require subnet_id "$SUBNET_ID"
require security_group_id "$SG_ID"
require instance_profile "$INSTANCE_PROFILE"
require cape_core_version "$CAPE_CORE_VERSION"
require cape_signatures_version "$CAPE_SIGNATURES_VERSION"
require cape_qemu_version "$CAPE_QEMU_VERSION"
require cape_suricata_version "$CAPE_SURICATA_VERSION"

log() { echo "[$(date -Iseconds)] $*" >&2; }

# ---- launch with nested virt ---------------------------------------------

log "Launching $INSTANCE_TYPE from $WARM_AMI_ID with NestedVirtualization=enabled"

run_args=(
    --image-id "$WARM_AMI_ID"
    --instance-type "$INSTANCE_TYPE"
    --subnet-id "$SUBNET_ID"
    --security-group-ids "$SG_ID"
    --iam-instance-profile "Name=$INSTANCE_PROFILE"
    --cpu-options "NestedVirtualization=enabled"
    --metadata-options "HttpTokens=required,HttpEndpoint=enabled"
    --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=1000,VolumeType=gp3,Iops=16000,Throughput=1000,DeleteOnTermination=true}'
    --tag-specifications "ResourceType=instance,Tags=[{Key=Owner,Value=capev2},{Key=Purpose,Value=cape-host-base-bake},{Key=Phase,Value=2-clone-snapshot},{Key=CapeCoreVersion,Value=$CAPE_CORE_VERSION},{Key=BakeRunId,Value=${GITHUB_RUN_ID:-local}}]"
    --region "$REGION"
)
[ -n "$KEY_PAIR_NAME" ] && run_args+=( --key-name "$KEY_PAIR_NAME" )

INSTANCE_ID=$(aws ec2 run-instances "${run_args[@]}" \
    --query 'Instances[0].InstanceId' --output text)
log "Launched $INSTANCE_ID"
# The builder is tagged BakeRunId=$GITHUB_RUN_ID at launch (above), so the
# workflow's always() cleanup step can find + terminate it by run id even
# if this script is SIGKILLed before its own trap runs — no env/file
# propagation needed (AWS is the source of truth, scoped to THIS run).

cleanup() {
    local ec=$?
    set +e
    if [[ "$KEEP_ON_FAIL" -eq 1 && "$ec" -ne 0 ]]; then
        log "::warning::--keep-on-fail set; leaving $INSTANCE_ID up for debugging"
        return $ec
    fi
    log "Terminating $INSTANCE_ID"
    aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" --region "$REGION" >/dev/null 2>&1 || true
    return $ec
}
trap cleanup EXIT
# A bare `trap ... EXIT` does NOT fire when bash is killed by SIGTERM/SIGINT
# — which is exactly how GitHub Actions cancels a job — so the shell would
# die before cleanup ran, leaking the m8i.8xlarge builder (this was the
# source of a 2026-06-02 orphan builder).  Trap the signals
# so cleanup still runs locally on cancellation; `exit 143` then triggers
# the EXIT trap exactly once.
trap 'exit 143' TERM INT HUP

# ---- wait for instance + ssm ---------------------------------------------

aws ec2 wait instance-running --instance-ids "$INSTANCE_ID" --region "$REGION"

log "Waiting for SSM agent online"
for i in $(seq 1 60); do
    status=$(aws ssm describe-instance-information \
        --filters "Key=InstanceIds,Values=$INSTANCE_ID" \
        --region "$REGION" \
        --query 'InstanceInformationList[0].PingStatus' \
        --output text 2>/dev/null || true)
    [[ "$status" == "Online" ]] && break
    sleep 5
done
[[ "$status" == "Online" ]] || { log "::error::SSM agent never came online on $INSTANCE_ID"; exit 1; }

# ---- run clone-win11-vms.sh via SSM --------------------------------------

# clone-win11-vms.sh ships in /opt/CAPEv2/scripts/ via the cape-core deb.
# We supply numbering 101..(101+NUM_CLONES-1) per nestedvirt convention.
START_NUM=101
END_NUM=$(( START_NUM + NUM_CLONES - 1 ))

# SSM AWS-RunShellScript executes via /bin/sh (dash on Ubuntu) which
# doesn't support `set -o pipefail`. Drop the script onto the host
# first, then invoke it under bash. tee + chmod via single SSM command
# so the heredoc only has to work once.
CLONE_SCRIPT_BODY=$(cat <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec > /var/log/cape-host-bake-clone.log 2>&1
echo "[\$(date -Iseconds)] starting clone phase"

# libvirtd must be running so virsh define / clone-win11-vms.sh can
# talk to it. The cape-* debs install libvirt-daemon-system but don't
# always enable+start it during install. enable --now is idempotent.
systemctl enable --now libvirtd

# Phase 1 staged the template XML at /etc/libvirt/qemu/win11_seabios.xml
# but didn't \`virsh define\` it (the Packer build instance has no
# /dev/kvm so define-time emulator probe rejects type='kvm'). Define
# it here, where /dev/kvm exists, BEFORE clone-win11-vms.sh runs —
# the clone script greps \`virsh list --all\` for the template name
# and bails immediately if it's missing.
if ! virsh dominfo "$BASE_VM_NAME" >/dev/null 2>&1; then
    virsh define /etc/libvirt/qemu/${BASE_VM_NAME}.xml
fi
echo "[\$(date -Iseconds)] template defined:"
virsh list --all

/opt/CAPEv2/scripts/clone-win11-vms.sh \\
    "$BASE_VM_NAME" "$START_NUM" "$END_NUM" "$VM_NETWORK_BASE" linked
echo "[\$(date -Iseconds)] clone phase done"
EOF
)

# Encode the body so we can pass it through SSM RunShellScript without
# escaping every special character.
CLONE_SCRIPT_B64=$(printf '%s' "$CLONE_SCRIPT_BODY" | base64 -w0)
CLONE_CMD="echo $CLONE_SCRIPT_B64 | base64 -d > /tmp/cape-host-bake-clone.sh && chmod +x /tmp/cape-host-bake-clone.sh && /tmp/cape-host-bake-clone.sh"

log "Dispatching clone command via SSM (NUM_CLONES=$NUM_CLONES, warmup=${VM_WARMUP_SECONDS}s)"

# AWS-RunShellScript has two distinct timeouts.  --timeout-seconds is
# the *queue* timeout (how long the command stays valid waiting for an
# online SSM agent to pick it up).  The document's executionTimeout
# parameter is the *runtime* limit on the instance, and defaults to
# 3600s (1h).  Our clone-phase regularly needs ~70 min (Phase 1 13min
# + clone-create + warmup 45min + snapshot + shutdown), so the default
# fires before the script finishes — caught on 2026-05-22 ami-bake run
# where Phase 2 timed out at exactly 1h with the bake
# script still mid-clone.
#
# Pass executionTimeout = 7200s (2h) to match the queue timeout.  Use
# jq to build the parameter JSON — embedding a base64 blob in the
# shorthand "commands=[...]" form requires per-character escaping that
# jq handles for us, and the JSON form is the only way to set
# executionTimeout (the shorthand only supports the commands array).
PARAMS_JSON=$(jq -nc \
    --arg cmd "$CLONE_CMD" \
    '{commands: [$cmd], executionTimeout: ["7200"]}')

cmd_id=$(aws ssm send-command \
    --instance-ids "$INSTANCE_ID" \
    --document-name AWS-RunShellScript \
    --parameters "$PARAMS_JSON" \
    --timeout-seconds 7200 \
    --region "$REGION" \
    --query 'Command.CommandId' \
    --output text)

# clone-win11-vms.sh handles its own warmup + snapshot internally (~15
# min as configured). We poll up to the warmup budget + 30 min slack.
DEADLINE=$(( $(date +%s) + VM_WARMUP_SECONDS + 1800 ))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    inv_status=$(aws ssm get-command-invocation \
        --command-id "$cmd_id" \
        --instance-id "$INSTANCE_ID" \
        --region "$REGION" \
        --query 'Status' \
        --output text 2>/dev/null || echo Pending)
    case "$inv_status" in
        Success) log "Clone phase succeeded"; break ;;
        Failed|Cancelled|TimedOut)
            log "::error::SSM clone command status=$inv_status"
            log "::group::SSM StandardOutput"
            aws ssm get-command-invocation --command-id "$cmd_id" \
                --instance-id "$INSTANCE_ID" --region "$REGION" \
                --query 'StandardOutputContent' --output text >&2 || true
            log "::endgroup::"
            log "::group::SSM StandardError"
            aws ssm get-command-invocation --command-id "$cmd_id" \
                --instance-id "$INSTANCE_ID" --region "$REGION" \
                --query 'StandardErrorContent' --output text >&2 || true
            log "::endgroup::"
            log "::group::tail of /var/log/cape-host-bake-clone.log"
            tail_cmd=$(aws ssm send-command \
                --instance-ids "$INSTANCE_ID" \
                --document-name AWS-RunShellScript \
                --parameters 'commands=["sudo tail -200 /var/log/cape-host-bake-clone.log 2>/dev/null || echo missing"]' \
                --region "$REGION" \
                --query 'Command.CommandId' \
                --output text 2>/dev/null || true)
            sleep 4
            aws ssm get-command-invocation --command-id "$tail_cmd" \
                --instance-id "$INSTANCE_ID" --region "$REGION" \
                --query 'StandardOutputContent' --output text >&2 || true
            log "::endgroup::"
            exit 1 ;;
    esac
    sleep 30
done
[[ "$inv_status" == "Success" ]] || { log "::error::clone phase timed out"; exit 1; }

# ---- stop + create-image -------------------------------------------------

log "Stopping instance for AMI capture"
aws ec2 stop-instances --instance-ids "$INSTANCE_ID" --region "$REGION" >/dev/null
aws ec2 wait instance-stopped --instance-ids "$INSTANCE_ID" --region "$REGION"

bake_date=$(date -u +%Y-%m-%dT%H:%M:%SZ)
# AMI names must match [A-Za-z0-9 .,/()@_'\[\]\-]+. Sanitize the cape-core
# version: `~` (Debian pre-release marker) and `+` (cape suffix) are
# rejected by AWS. Mirrors the sanitize in packer/cape-host.pkr.hcl.
sanitized_version=$(echo "$CAPE_CORE_VERSION" | tr '~+' '-.')
ami_name="cape-host-base-${sanitized_version}-${bake_date//[:.+]/-}"

log "Creating AMI $ami_name"
NEW_AMI_ID=$(aws ec2 create-image \
    --instance-id "$INSTANCE_ID" \
    --name "$ami_name" \
    --description "CAPEv2 sandbox host (Phase 2 final). cape-core=$CAPE_CORE_VERSION cape-signatures=$CAPE_SIGNATURES_VERSION cape-qemu=$CAPE_QEMU_VERSION cape-suricata=$CAPE_SURICATA_VERSION. Includes $NUM_CLONES Win11 linked clones with snapshot1." \
    --no-reboot \
    --region "$REGION" \
    --query 'ImageId' \
    --output text)
log "Created $NEW_AMI_ID (waiting for available)"

aws ec2 wait image-available --image-ids "$NEW_AMI_ID" --region "$REGION"

# ---- mandatory tagging policy --------------------------------------------

aws ec2 create-tags --resources "$NEW_AMI_ID" --region "$REGION" --tags \
    "Key=Owner,Value=capev2" \
    "Key=Purpose,Value=cape-host-base" \
    "Key=CapeCoreVersion,Value=$CAPE_CORE_VERSION" \
    "Key=CapeSignaturesVersion,Value=$CAPE_SIGNATURES_VERSION" \
    "Key=CapeQemuVersion,Value=$CAPE_QEMU_VERSION" \
    "Key=CapeSuricataVersion,Value=$CAPE_SURICATA_VERSION" \
    "Key=BakeDate,Value=$bake_date" \
    "Key=Retention,Value=keep" \
    "Key=BuildRunId,Value=${GITHUB_RUN_ID:-local}"

# OsDebHashes: 64-char sha256 of the sorted-JSON dict of OS-affecting
# cape-* deb sha256s computed in apt-publish.yml's "Compute OS-affecting
# deb sha256s" step.  The next apt-publish (prod) reads this tag back
# via aws ec2 describe-tags and skips ami-bake when its own freshly-
# computed digest matches.  See .github/workflows/apt-publish.yml
# "Check if ami-bake can be skipped" step for the comparison.
#
# Why a digest instead of the full JSON dict: AWS tag value cap is 256
# chars; the 8-package dict would be ~700 chars.  Digest collapses to
# 64 chars at the cost of operator readability — the full per-package
# dict is logged in the apt-publish run summary.
if [ -n "$OS_DEB_HASHES_DIGEST" ]; then
    aws ec2 create-tags --resources "$NEW_AMI_ID" --region "$REGION" --tags \
        "Key=OsDebHashes,Value=$OS_DEB_HASHES_DIGEST"
    log "Tagged $NEW_AMI_ID OsDebHashes=$OS_DEB_HASHES_DIGEST (skip-ami-bake check input)"
fi

# Tag the underlying snapshots too — Recycle Bin retention rules
# match on resource tags, not AMI relationships.
SNAP_IDS=$(aws ec2 describe-images --image-ids "$NEW_AMI_ID" --region "$REGION" \
    --query 'Images[0].BlockDeviceMappings[].Ebs.SnapshotId' --output text)
for snap in $SNAP_IDS; do
    aws ec2 create-tags --resources "$snap" --region "$REGION" --tags \
        "Key=Owner,Value=capev2" \
        "Key=Purpose,Value=cape-host-base" \
        "Key=CapeCoreVersion,Value=$CAPE_CORE_VERSION" \
        "Key=BakeDate,Value=$bake_date" \
        "Key=AmiId,Value=$NEW_AMI_ID" \
        "Key=Retention,Value=keep"
done

# ---- supersede previous AMI in this lineage ------------------------------

if [ -n "$PREVIOUS_AMI_ID" ]; then
    log "Marking $PREVIOUS_AMI_ID Retention=superseded, SupersededBy=$NEW_AMI_ID"
    aws ec2 create-tags --resources "$PREVIOUS_AMI_ID" --region "$REGION" --tags \
        "Key=Retention,Value=superseded" \
        "Key=SupersededBy,Value=$NEW_AMI_ID" \
        "Key=SupersededAt,Value=$bake_date"
fi

log "Bake complete: $NEW_AMI_ID"
echo "$NEW_AMI_ID"
