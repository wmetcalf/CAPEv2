#!/bin/bash
# Fix Docker's raw table drop rule that blocks VM→FakeNet TCP.
#
# Docker adds: ip daddr 172.28.100.2 iifname != "br-fakenet" drop
# in ip raw PREROUTING. This blocks VMs from reaching FakeNet because
# FakeNet DNS responds with 172.28.100.2 and VMs connect directly.
# We insert an accept rule for virbr100 before Docker's drop.

FAKENET_IP="172.28.100.2"
BRIDGE_IF="virbr100"
MAX_WAIT=120

echo "Waiting for Docker raw rule for ${FAKENET_IP}..."

for i in $(seq 1 $MAX_WAIT); do
    if nft list chain ip raw PREROUTING 2>/dev/null | grep -q "daddr ${FAKENET_IP}.*drop"; then
        echo "  Docker raw drop rule detected after ${i}s"

        # Remove any stale accept rule
        HANDLE=$(nft -a list chain ip raw PREROUTING 2>/dev/null | \
                 grep "iifname \"${BRIDGE_IF}\".*daddr ${FAKENET_IP}.*accept" | \
                 awk '{print $NF}')
        [ -n "$HANDLE" ] && nft delete rule ip raw PREROUTING handle "$HANDLE" 2>/dev/null

        # Insert accept before the drop
        nft insert rule ip raw PREROUTING iifname "${BRIDGE_IF}" ip daddr ${FAKENET_IP} counter accept
        echo "  ✓ FakeNet raw exception added (${BRIDGE_IF} → ${FAKENET_IP})"
        exit 0
    fi
    sleep 1
done

echo "  WARNING: Docker raw drop rule never appeared after ${MAX_WAIT}s"
exit 0
