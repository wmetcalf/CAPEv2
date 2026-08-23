#!/bin/bash
# CAPE guest routing through VPN
# Called by cape-routing.service at boot

BRIDGE_IF="virbr100"
GUEST_NET="192.168.100.0/24"
VPN_IF="tun0"
VPN_TABLE="tun0"

echo "Configuring CAPE guest routing..."
echo "  Bridge: $BRIDGE_IF | Guest: $GUEST_NET | VPN: $VPN_IF | Table: $VPN_TABLE"

# Wait for tun0 to come up (max 60 seconds)
for i in $(seq 1 60); do
    if ip link show "$VPN_IF" &>/dev/null; then
        echo "  $VPN_IF is up"
        break
    fi
    if [ "$i" -eq 60 ]; then
        echo "  WARNING: $VPN_IF not up after 60s, proceeding anyway"
    fi
    sleep 1
done

# ── Routing Table ──
# Clear and rebuild table
ip route flush table $VPN_TABLE 2>/dev/null || true

# Default route through VPN
ip route add default dev $VPN_IF table $VPN_TABLE 2>/dev/null || true

# Guest subnet stays local (so replies go back via bridge, not VPN)
ip route add $GUEST_NET dev $BRIDGE_IF table $VPN_TABLE 2>/dev/null || true

echo "  ✓ Routing table $VPN_TABLE configured"

# ── Policy Routing Rules ──
# Clear existing rules first (idempotent)
ip rule del from $GUEST_NET to 10.0.0.0/8 lookup main priority 99 2>/dev/null || true
ip rule del from $GUEST_NET to 172.16.0.0/12 lookup main priority 99 2>/dev/null || true
ip rule del from $GUEST_NET to 192.168.0.0/16 lookup main priority 99 2>/dev/null || true
ip rule del from $GUEST_NET lookup vpn priority 100 2>/dev/null || true
ip rule del from $GUEST_NET lookup $VPN_TABLE priority 100 2>/dev/null || true

# Priority 99: Keep RFC1918 traffic local
ip rule add from $GUEST_NET to 10.0.0.0/8 lookup main priority 99
ip rule add from $GUEST_NET to 172.16.0.0/12 lookup main priority 99
ip rule add from $GUEST_NET to 192.168.0.0/16 lookup main priority 99

# Priority 100: Route guest internet through VPN
ip rule add from $GUEST_NET lookup $VPN_TABLE priority 100

echo "  ✓ Policy routing rules configured"

# ── iptables NAT/FORWARD ──
# Clear existing (idempotent)
iptables -t nat -D POSTROUTING -s $GUEST_NET -o $VPN_IF -j MASQUERADE 2>/dev/null || true
iptables -D FORWARD -s $GUEST_NET -o $VPN_IF -j ACCEPT 2>/dev/null || true
iptables -D FORWARD -i $VPN_IF -d $GUEST_NET -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true

# Add NAT masquerade
iptables -t nat -A POSTROUTING -s $GUEST_NET -o $VPN_IF -j MASQUERADE

# Allow forwarding
iptables -A FORWARD -s $GUEST_NET -o $VPN_IF -j ACCEPT
iptables -A FORWARD -i $VPN_IF -d $GUEST_NET -m state --state RELATED,ESTABLISHED -j ACCEPT

echo "  ✓ iptables NAT/FORWARD rules configured"

echo ""
echo "Routing summary:"
echo "  Rules:"
ip rule list | grep -E "(99|100):" | sed 's/^/    /'
echo "  Table $VPN_TABLE:"
ip route show table $VPN_TABLE | sed 's/^/    /'
echo ""
echo "CAPE guest routing setup complete."
