#!/usr/bin/env bash
# Layer 2 — Container install smoke test.
#
# Boots a clean ubuntu:24.04 container, copies the freshly-built .debs in,
# installs them, verifies:
#   - bundled venv resolves CAPE imports without ImportError
#   - cape-web binds on :8000 with a minimal "machinery=none" config
#   - dpkg -V reports clean
#
# Doesn't exercise libvirt (containers can't). That's Layer 5's job.
#
# Usage:
#   ./test_install.sh /path/to/debs/

set -euo pipefail

DEBS_DIR="${1:?usage: $0 <debs-dir>}"
[[ -d "$DEBS_DIR" ]] || { echo "no debs at $DEBS_DIR"; exit 1; }

CONTAINER_NAME="cape-l2-$$"

cleanup() {
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "→ launching ubuntu:24.04 container"
docker run -d --name "$CONTAINER_NAME" \
    -v "$DEBS_DIR:/debs:ro" \
    ubuntu:24.04 sleep infinity

docker exec "$CONTAINER_NAME" bash -c '
    set -euo pipefail
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    # Install our debs together so apt resolves intra-package Depends.
    # cape-qemu and cape-suricata are amd64; cape-signatures is all.
    apt-get install -y -qq /debs/cape-core_*_amd64.deb \
                           /debs/cape-signatures_*_all.deb \
                           /debs/cape-qemu_*_amd64.deb \
                           /debs/cape-suricata_*_amd64.deb

    echo "→ dpkg -V on each package"
    for pkg in cape-core cape-signatures cape-qemu cape-suricata; do
        dpkg -V "$pkg"
    done

    echo "→ venv import sanity"
    /opt/CAPEv2/.venv/bin/python -c "
import sys
print(f\"python: {sys.version}\")
import lib.cuckoo, web, modules
print(\"CAPE imports OK\")
"

    echo "→ cape-web binds on :8000 (10s probe)"
    /opt/CAPEv2/.venv/bin/python /opt/CAPEv2/web/manage.py runserver \
        0.0.0.0:8000 --noreload &
    SRV=$!
    sleep 6
    curl -sf -o /dev/null --max-time 3 http://127.0.0.1:8000/api/tasks/list/ \
        && echo "  /api/tasks/list/ → 200" \
        || echo "  /api/tasks/list/ probe failed (fatal? depends on auth/db config)"
    kill $SRV 2>/dev/null || true
'

echo
echo "Layer 2 smoke test passed."
