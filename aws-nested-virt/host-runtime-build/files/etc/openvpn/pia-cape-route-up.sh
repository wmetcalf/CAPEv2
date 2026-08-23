#!/bin/bash
# Restore CAPE guest routing after the PIA tunnel comes up.
#
# OpenVPN fires this from the route-up directive in pia-cape.conf, once
# per (re)connection, AFTER openvpn has installed its own routes.  It
# (re)populates the `tun0` policy routing table that setup-cape-routing.sh
# established at boot — that table's `default dev tun0` route gets wiped
# every time the tun0 device flaps (cipher renegotiation, network blip,
# service restart, etc.) because the kernel drops routes pinned to a
# vanishing device.  Without this hook, the policy rules added at boot
# (`from 192.168.100.0/24 lookup tun0`) divert VM traffic to an empty
# table — every analysis task silently loses internet, suricata/CAPE
# capture nothing, and the smoke gate fails false-negative.
#
# Use `ip route replace` (not `add`) so the hook is idempotent: harmless
# if the route already exists from setup-cape-routing.sh, fully restoring
# if it was wiped.
/bin/ip route replace 192.168.100.0/24 dev virbr100 table tun0
/bin/ip route replace default dev tun0 table tun0
if [ $? -eq 0 ]; then
    logger -t openvpn-pia "route-up: tun0 table routes set"
else
    logger -t openvpn-pia "route-up: FAILED to set tun0 table routes"
    exit 1
fi
