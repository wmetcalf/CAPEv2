#!/bin/bash
# CAPE Firewall Rules Loader - runs at boot

echo "Loading CAPE sandbox network isolation rules..."

RULES_DIR="/etc/cape/firewall"

if command -v nft &>/dev/null; then
    for rule_file in "$RULES_DIR"/rules-*.nft; do
        if [ -f "$rule_file" ]; then
            echo "  Loading: $(basename $rule_file)"
            nft -f "$rule_file" 2>/dev/null || echo "  WARNING: Failed to load $rule_file"
        fi
    done
else
    echo "  ERROR: nft not found"
    exit 1
fi

echo "CAPE firewall isolation rules loaded."
