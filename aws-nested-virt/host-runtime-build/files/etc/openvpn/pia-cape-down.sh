#!/bin/bash
# Clean up CAPE guest routing when the PIA tunnel goes down.
#
# Counterpart to pia-cape-route-up.sh.  Fires from the down directive in
# pia-cape.conf.  Not strictly required (kernel drops `dev tun0` routes
# automatically when tun0 disappears) but makes the teardown explicit
# and leaves a clear breadcrumb in the journal so operators can tell
# when the tunnel cycled.
/bin/ip route del default dev tun0 table tun0 2>/dev/null
/bin/ip route del 192.168.100.0/24 dev virbr100 table tun0 2>/dev/null
logger -t openvpn-pia "down: tun0 table routes removed"
