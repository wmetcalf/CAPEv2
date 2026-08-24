#!/usr/bin/env bash
# Phase 2 e2e test driver.
#
# Wraps `terraform apply` of either Layer 4 or Layer 5, polls the test
# host's result marker via SSM, and tears the stack down regardless of
# pass/fail (`trap EXIT terraform destroy`). Returns the layer-specific
# exit code.
#
# Usage:
#   run-layer-test.sh --layer {4|5} \
#                     --apt-repo-url URL \
#                     --apt-keyring-url URL \
#                     --threat-content-url URL \
#                     --cape-core-version VER \
#                     --cape-signatures-version VER \
#                     --cape-qemu-version VER \
#                     --cape-suricata-version VER \
#                     [--test-run-id ID] \
#                     [--ubuntu-ami-id AMI] \
#                     [--region REGION] \
#                     [--keep-on-fail]
#
# Designed for CI use; can also be invoked manually for ad-hoc runs.

set -euo pipefail

# ---- arg parsing ----------------------------------------------------------

LAYER=""
APT_REPO_URL=""
APT_KEYRING_URL=""
THREAT_CONTENT_URL=""
CAPE_CORE_VERSION=""
CAPE_SIGNATURES_VERSION=""
CAPE_QEMU_VERSION=""
CAPE_SURICATA_VERSION=""
TEST_RUN_ID="${GITHUB_RUN_ID:-$(date +%s)-$$}"
UBUNTU_AMI_ID=""
CAPE_HOST_BASE_AMI_ID=""
CAPE_ADMIN_SECRET_NAME=""
REGION="us-east-1"
KEEP_ON_FAIL=0

usage() { sed -n '4,28p' "$0"; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --layer)                  LAYER="$2"; shift 2;;
        --apt-repo-url)           APT_REPO_URL="$2"; shift 2;;
        --apt-keyring-url)        APT_KEYRING_URL="$2"; shift 2;;
        --threat-content-url)     THREAT_CONTENT_URL="$2"; shift 2;;
        --cape-core-version)      CAPE_CORE_VERSION="$2"; shift 2;;
        --cape-signatures-version) CAPE_SIGNATURES_VERSION="$2"; shift 2;;
        --cape-qemu-version)      CAPE_QEMU_VERSION="$2"; shift 2;;
        --cape-suricata-version)  CAPE_SURICATA_VERSION="$2"; shift 2;;
        --test-run-id)            TEST_RUN_ID="$2"; shift 2;;
        --ubuntu-ami-id)          UBUNTU_AMI_ID="$2"; shift 2;;
        --cape-host-base-ami-id)  CAPE_HOST_BASE_AMI_ID="$2"; shift 2;;
        --cape-admin-secret-name) CAPE_ADMIN_SECRET_NAME="$2"; shift 2;;
        --region)                 REGION="$2"; shift 2;;
        --keep-on-fail)           KEEP_ON_FAIL=1; shift;;
        -h|--help)                usage;;
        *) echo "unknown arg: $1" >&2; usage;;
    esac
done

[[ "$LAYER" == "4" || "$LAYER" == "5" ]] || { echo "::error::--layer must be 4 or 5"; exit 2; }
[[ -n "$APT_REPO_URL" && -n "$APT_KEYRING_URL" && -n "$THREAT_CONTENT_URL" ]] || \
    { echo "::error::missing --apt-repo-url / --apt-keyring-url / --threat-content-url"; exit 2; }
[[ -n "$CAPE_CORE_VERSION" && -n "$CAPE_SIGNATURES_VERSION" \
   && -n "$CAPE_QEMU_VERSION" && -n "$CAPE_SURICATA_VERSION" ]] || \
    { echo "::error::missing cape-*-version args"; exit 2; }

# Layer 4 launches against a stock Ubuntu AMI to test the deb install path.
# Layer 5 launches against the latest cape-host-base AMI to run a real
# EICAR analysis through it — operator passes --cape-host-base-ami-id.
if [[ "$LAYER" == "4" && -z "$UBUNTU_AMI_ID" ]]; then
    UBUNTU_AMI_ID=$(aws ec2 describe-images --region "$REGION" \
        --owners 099720109477 \
        --filters 'Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*' \
                  'Name=architecture,Values=x86_64' \
                  'Name=state,Values=available' \
        --query 'sort_by(Images, &CreationDate)[-1].ImageId' \
        --output text)
fi

if [[ "$LAYER" == "5" && -z "$CAPE_HOST_BASE_AMI_ID" ]]; then
    # Look for the most recent AMI with Owner=capev2, Purpose=cape-host-base.
    CAPE_HOST_BASE_AMI_ID=$(aws ec2 describe-images --region "$REGION" \
        --owners self \
        --filters 'Name=tag:Owner,Values=capev2' \
                  'Name=tag:Purpose,Values=cape-host-base' \
                  'Name=tag:Retention,Values=keep' \
                  'Name=state,Values=available' \
        --query 'sort_by(Images, &CreationDate)[-1].ImageId' \
        --output text)
    [[ "$CAPE_HOST_BASE_AMI_ID" != "None" && -n "$CAPE_HOST_BASE_AMI_ID" ]] || \
        { echo "::error::no cape-host-base AMI found — pass --cape-host-base-ami-id explicitly"; exit 2; }
fi

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
case "$LAYER" in
    4) STACK_DIR="$REPO_ROOT/test/layer4-host-smoke";;
    5) STACK_DIR="$REPO_ROOT/test/layer5-eicar-e2e";;
    *) echo "::error::unsupported layer $LAYER"; exit 2;;
esac

[ -d "$STACK_DIR" ] || { echo "::error::stack dir not found: $STACK_DIR"; exit 2; }

# Per-layer terraform var lists. Each layer has its own input shape.
case "$LAYER" in
    4)
        TF_VARS=(
            -var "test_run_id=$TEST_RUN_ID"
            -var "ubuntu_ami_id=$UBUNTU_AMI_ID"
            -var "apt_repo_url=$APT_REPO_URL"
            -var "apt_repo_keyring_url=$APT_KEYRING_URL"
            -var "threat_content_url=$THREAT_CONTENT_URL"
            -var "cape_core_version=$CAPE_CORE_VERSION"
            -var "cape_signatures_version=$CAPE_SIGNATURES_VERSION"
            -var "cape_qemu_version=$CAPE_QEMU_VERSION"
            -var "cape_suricata_version=$CAPE_SURICATA_VERSION"
            -var "aws_region=$REGION"
        )
        BOOTSTRAP_DEADLINE_SEC=1800  # 30 min — cape-core's Depends pull
                                     # clamav-daemon + freshclam (~200MB
                                     # signature DB download on first
                                     # start), libvirt-daemon-system,
                                     # postgresql. The aggregate postinst
                                     # phase pushes well past 10 min on
                                     # a t3.medium with normal bandwidth.
        ;;
    5)
        TF_VARS=(
            -var "test_run_id=$TEST_RUN_ID"
            -var "cape_host_base_ami_id=$CAPE_HOST_BASE_AMI_ID"
            -var "apt_repo_url=$APT_REPO_URL"
            -var "apt_repo_keyring_url=$APT_KEYRING_URL"
            -var "cape_core_version=$CAPE_CORE_VERSION"
            -var "cape_signatures_version=$CAPE_SIGNATURES_VERSION"
            -var "cape_qemu_version=$CAPE_QEMU_VERSION"
            -var "cape_suricata_version=$CAPE_SURICATA_VERSION"
            -var "cape_admin_secret_name=$CAPE_ADMIN_SECRET_NAME"
            -var "aws_region=$REGION"
        )
        BOOTSTRAP_DEADLINE_SEC=3600  # 60 min — a full 24-clone rebuild (~32-35m)
                                     # + apt overlay + cape startup can brush 40m
                                     # on a HEALTHY host; give margin so a green
                                     # host is not reported as a timeout.
        ;;
esac

cd "$STACK_DIR"

# ---- cleanup trap ---------------------------------------------------------

cleanup() {
    local exit_code=$?
    set +e
    if [[ "$KEEP_ON_FAIL" -eq 1 && "$exit_code" -ne 0 ]]; then
        echo "::warning::--keep-on-fail set; leaving stack up for debugging (run id $TEST_RUN_ID)"
        return $exit_code
    fi
    echo "[$(date -Iseconds)] tearing down stack (exit_code=$exit_code)"
    terraform destroy -auto-approve "${TF_VARS[@]}" 2>&1 | tail -20 || true
    return $exit_code
}
trap cleanup EXIT

# ---- apply -----------------------------------------------------------------

terraform init -input=false -backend=false
terraform apply -auto-approve "${TF_VARS[@]}"

INSTANCE_ID=$(terraform output -raw instance_id)
RESULT_PATH=$(terraform output -raw result_marker_path)

echo "[$(date -Iseconds)] waiting for SSM agent to be online on $INSTANCE_ID"
for i in $(seq 1 30); do
    status=$(aws ssm describe-instance-information \
        --filters "Key=InstanceIds,Values=$INSTANCE_ID" \
        --region "$REGION" \
        --query 'InstanceInformationList[0].PingStatus' \
        --output text 2>/dev/null || true)
    [[ "$status" == "Online" ]] && break
    sleep 10
done

DEADLINE_SEC=$(( $(date +%s) + BOOTSTRAP_DEADLINE_SEC ))

echo "[$(date -Iseconds)] polling for $RESULT_PATH (deadline ${BOOTSTRAP_DEADLINE_SEC}s)"
while [ "$(date +%s)" -lt "$DEADLINE_SEC" ]; do
    cmd_id=$(aws ssm send-command --instance-ids "$INSTANCE_ID" \
        --document-name AWS-RunShellScript \
        --parameters "commands=[\"sudo cat $RESULT_PATH 2>/dev/null || echo NOT_READY\"]" \
        --region "$REGION" \
        --query 'Command.CommandId' \
        --output text)
    sleep 4
    output=$(aws ssm get-command-invocation \
        --command-id "$cmd_id" \
        --instance-id "$INSTANCE_ID" \
        --region "$REGION" \
        --query 'StandardOutputContent' \
        --output text 2>/dev/null || echo NOT_READY)
    if [[ "$output" != *"NOT_READY"* ]]; then
        echo "[$(date -Iseconds)] bootstrap-ready marker present"
        echo "$output" | tee /tmp/layer${LAYER}-bootstrap-result.json
        fail=$(echo "$output" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("fail", 1))' 2>/dev/null || echo 1)
        if [[ "$fail" -ne 0 ]]; then
            echo "::error::Layer $LAYER bootstrap failed"
            # Dump the host's userdata log so the failure cause shows up
            # in the workflow log instead of disappearing with teardown.
            echo "::group::host userdata log (last 200 lines)"
            log_cmd=$(aws ssm send-command \
                --instance-ids "$INSTANCE_ID" \
                --document-name AWS-RunShellScript \
                --parameters "commands=[\"sudo tail -200 /var/log/layer${LAYER}-userdata.log 2>/dev/null || sudo tail -200 /var/log/cloud-init-output.log\"]" \
                --region "$REGION" \
                --query 'Command.CommandId' \
                --output text 2>/dev/null || true)
            sleep 4
            aws ssm get-command-invocation \
                --command-id "$log_cmd" \
                --instance-id "$INSTANCE_ID" \
                --region "$REGION" \
                --query 'StandardOutputContent' \
                --output text 2>/dev/null || echo "(unable to fetch log)"
            echo "::endgroup::"
            exit 1
        fi
        # Layer 4 stops here — bootstrap success IS the test signal.
        # Layer 5 continues with EICAR submission.
        if [[ "$LAYER" == "4" ]]; then
            exit 0
        fi
        break
    fi
    sleep 30
done

if [ "$(date +%s)" -ge "$DEADLINE_SEC" ]; then
    echo "::error::Layer $LAYER timed out waiting for bootstrap-ready marker"
    # Dump whatever log + cloud-init state we can find so the cause is
    # visible before the cleanup trap tears the host down.
    echo "::group::host userdata log on timeout (last 200 lines)"
    log_cmd=$(aws ssm send-command \
        --instance-ids "$INSTANCE_ID" \
        --document-name AWS-RunShellScript \
        --parameters "commands=[\"echo === layer-userdata.log ===; sudo tail -200 /var/log/layer${LAYER}-userdata.log 2>/dev/null || echo missing; echo === cloud-init-output.log ===; sudo tail -200 /var/log/cloud-init-output.log 2>/dev/null || echo missing; echo === cloud-init.log tail ===; sudo tail -100 /var/log/cloud-init.log 2>/dev/null || echo missing\"]" \
        --region "$REGION" \
        --query 'Command.CommandId' \
        --output text 2>/dev/null || true)
    sleep 4
    aws ssm get-command-invocation \
        --command-id "$log_cmd" \
        --instance-id "$INSTANCE_ID" \
        --region "$REGION" \
        --query 'StandardOutputContent' \
        --output text 2>/dev/null || echo "(unable to fetch log)"
    echo "::endgroup::"
    exit 1
fi

# ---- Layer 5: drive the EICAR submission + adapter assertion --------------

echo "[$(date -Iseconds)] Layer 5 bootstrap green; handing off to EICAR driver"
INSTANCE_ID="$INSTANCE_ID" REGION="$REGION" REPO_ROOT="$REPO_ROOT/.." \
    "$REPO_ROOT/test/scripts/layer5-eicar-driver.sh"
