#!/bin/bash

# Clone Windows 11 VMs for CAPE Sandbox
# Handles disk cloning, MAC addresses, network configuration, and unique identifiers
# Boots VMs, waits for initialization, creates timestamped snapshots, and generates CAPE config

set -e

# Colors
NC='\033[0m'
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'

# Usage function
usage() {
    echo "Usage: $0 <template_vm_name> <start_number> <end_number> <network_base> [clone_type] [clone_name_prefix]"
    echo ""
    echo "Arguments:"
    echo "  template_vm_name   - Name of the template VM to clone from"
    echo "  start_number       - Starting number for clones (e.g., 101)"
    echo "  end_number         - Ending number for clones (e.g., 110)"
    echo "  network_base       - Network base address (e.g., 192.168.100)"
    echo "  clone_type         - 'linked' (default, fast) or 'full' (independent)"
    echo "  clone_name_prefix  - Custom name prefix for clones (default: template name)"
    echo ""
    echo "Examples:"
    echo "  $0 win11_21h2 101 110 192.168.100"
    echo "      # Creates: win11_21h2_101, win11_21h2_102, etc."
    echo ""
    echo "  $0 win11_21h2 101 110 192.168.100 linked win11_21h2_office2016"
    echo "      # Creates: win11_21h2_office2016_101, win11_21h2_office2016_102, etc."
    echo ""
    echo "Features:"
    echo "  - Creates unique virbr* network with DHCP pool and reservations"
    echo "  - Boots all VMs, waits 15 minutes for initialization"
    echo "  - Creates timestamped snapshots"
    echo "  - Generates CAPE config file automatically"
    echo ""
    exit 1
}

# Check arguments
if [ $# -lt 4 ]; then
    usage
fi

TEMPLATE_VM="$1"
START_NUM="$2"
END_NUM="$3"
NETWORK_BASE="$4"
CLONE_TYPE="${5:-linked}"  # Default to linked
CLONE_NAME_PREFIX="${6:-$TEMPLATE_VM}"  # Default to template name

# Configuration
DISK_BASE_PATH="/var/lib/libvirt/images"
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Calculate network parameters from base
NETWORK_SUFFIX=$(echo "$NETWORK_BASE" | awk -F. '{print $NF}')
VM_NETWORK="cape-${NETWORK_SUFFIX}"
GATEWAY_IP="${NETWORK_BASE}.1"
DHCP_START="${NETWORK_BASE}.2"
DHCP_END="${NETWORK_BASE}.254"
DNS_PRIMARY="8.8.8.8"
DNS_SECONDARY="8.8.4.4"

# Config and snapshot files
CONFIG_FILE="${SCRIPT_DIR}/kvm_${VM_NETWORK}_${TIMESTAMP}.conf"
SNAPSHOT_NAME="snapshot1"

# Validate clone type
if [ "$CLONE_TYPE" != "linked" ] && [ "$CLONE_TYPE" != "full" ]; then
    echo -e "${RED}[-] Clone type must be 'linked' or 'full'${NC}"
    exit 1
fi

# Validate network base format
if ! [[ "$NETWORK_BASE" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
    echo -e "${RED}[-] Invalid network base format: $NETWORK_BASE${NC}"
    echo -e "${RED}    Expected format: xxx.xxx.xxx (e.g., 192.168.100)${NC}"
    exit 1
fi

# Check if template exists
if ! virsh list --all | grep -q "$TEMPLATE_VM"; then
    echo -e "${RED}[-] Template VM '$TEMPLATE_VM' not found${NC}"
    virsh list --all
    exit 1
fi

# Get template disk path
TEMPLATE_DISK=$(virsh domblklist "$TEMPLATE_VM" | grep -E '\.qcow2|\.img' | head -1 | awk '{print $2}')
if [ -z "$TEMPLATE_DISK" ]; then
    echo -e "${RED}[-] Could not find template disk for $TEMPLATE_VM${NC}"
    exit 1
fi

echo -e "${GREEN}[+] Cloning Configuration:${NC}"
echo -e "    Template VM: $TEMPLATE_VM"
echo -e "    Clone Name Prefix: $CLONE_NAME_PREFIX"
echo -e "    Template Disk: $TEMPLATE_DISK"
echo -e "    Clone Type: $CLONE_TYPE"
echo -e "    VM Range: ${START_NUM}-${END_NUM}"
echo -e "    Total VMs: $((END_NUM - START_NUM + 1))"
echo -e "    Network: $VM_NETWORK ($NETWORK_BASE.0/24)"
echo -e "    Gateway: $GATEWAY_IP"
echo -e "    DHCP Pool: $DHCP_START - $DHCP_END"
echo -e "    DNS Servers: $DNS_PRIMARY, $DNS_SECONDARY"
echo ""

# Ensure CAPE network exists
if virsh net-list --all | grep -q "$VM_NETWORK"; then
    echo -e "${YELLOW}[!] Network already exists: $VM_NETWORK${NC}"

    # Verify it has the correct configuration
    EXISTING_GATEWAY=$(virsh net-dumpxml ${VM_NETWORK} 2>/dev/null | grep -oP '(?<=<ip address=.)[^"]*' | head -1)
    if [ "$EXISTING_GATEWAY" = "$GATEWAY_IP" ]; then
        echo -e "${GREEN}[+] Network configuration matches (Gateway: $GATEWAY_IP)${NC}"
    else
        echo -e "${RED}[!] WARNING: Network exists but with different config!${NC}"
        echo -e "${RED}    Expected gateway: $GATEWAY_IP${NC}"
        echo -e "${RED}    Existing gateway: $EXISTING_GATEWAY${NC}"
        echo -e "${YELLOW}    Proceeding anyway - using existing network${NC}"
    fi
else
    echo -e "${YELLOW}[!] Creating network: $VM_NETWORK${NC}"

    # Generate unique MAC for this network
    NETWORK_MAC=$(printf '52:%02X:%02X:%02X:%02X:%02X' $((RANDOM%256)) $((RANDOM%256)) $((RANDOM%256)) $((RANDOM%256)) $((RANDOM%256)))

    # Generate unique UUID for this network
    NETWORK_UUID=$(uuidgen)

    cat > /tmp/${VM_NETWORK}.xml << EOF
<network xmlns:dnsmasq='http://libvirt.org/schemas/network/dnsmasq/1.0'>
  <name>${VM_NETWORK}</name>
  <uuid>${NETWORK_UUID}</uuid>
  <bridge name='virbr${NETWORK_SUFFIX}' stp='on' delay='0'/>
  <mac address='${NETWORK_MAC}'/>
  <domain name='${VM_NETWORK}'/>
  <dns>
    <forwarder addr='${DNS_PRIMARY}'/>
    <forwarder addr='${DNS_SECONDARY}'/>
  </dns>
  <ip address='${GATEWAY_IP}' netmask='255.255.255.0'>
    <dhcp>
      <range start='${DHCP_START}' end='${DHCP_END}'/>
    </dhcp>
  </ip>
  <dnsmasq:options>
    <dnsmasq:option value='dhcp-option=3,${GATEWAY_IP}'/>
    <dnsmasq:option value='dhcp-option=6,${DNS_PRIMARY},${DNS_SECONDARY}'/>
    <dnsmasq:option value='dhcp-option=44,0.0.0.0'/>
    <dnsmasq:option value='dhcp-option=45,0.0.0.0'/>
    <dnsmasq:option value='dhcp-option=46,8'/>
    <dnsmasq:option value='dhcp-option=252,"\n"'/>
    <dnsmasq:option value='stop-dns-rebind'/>
  </dnsmasq:options>
</network>
EOF

    virsh net-define /tmp/${VM_NETWORK}.xml
    virsh net-autostart ${VM_NETWORK}
    virsh net-start ${VM_NETWORK}
    echo -e "${GREEN}[+] Network created: $VM_NETWORK (virbr${NETWORK_SUFFIX})${NC}"
fi

# Verify network is running
if ! virsh net-list | grep -q "${VM_NETWORK}.*active"; then
    echo -e "${YELLOW}[!] Starting network: $VM_NETWORK${NC}"
    virsh net-start ${VM_NETWORK}
fi

# Configure firewall rules to isolate VMs
echo -e "${YELLOW}[!] Configuring network isolation rules...${NC}"
echo -e "    - VMs blocked from accessing each other"
echo -e "    - VMs blocked from accessing RFC1918 networks"
echo -e "    - VMs allowed to route through gateway to internet (tun0)"
echo ""

NETWORK_CIDR="${NETWORK_BASE}.0/24"
BRIDGE_NAME="virbr${NETWORK_SUFFIX}"

# Function to apply firewall rules and save for persistence
apply_firewall_rules() {
    local FIREWALL_DIR="/etc/cape/firewall"
    local RULES_FILE="${FIREWALL_DIR}/rules-${VM_NETWORK}.nft"

    # Create firewall config directory
    if [ ! -d "$FIREWALL_DIR" ]; then
        mkdir -p "$FIREWALL_DIR"
        chmod 700 "$FIREWALL_DIR"
    fi

    if command -v nft &> /dev/null; then
        echo -e "    Using nftables for network isolation"

        # Create table if it doesn't exist
        nft list table bridge cape-isolation 2>/dev/null > /dev/null || \
            nft create table bridge cape-isolation

        # Create chain if it doesn't exist
        nft list chain bridge cape-isolation forward-vms 2>/dev/null > /dev/null || \
            nft create chain bridge cape-isolation forward-vms { type filter hook forward priority -200 \; policy accept \; }

        # Clear existing rules for this bridge (to allow re-runs)
        nft flush chain bridge cape-isolation forward-vms 2>/dev/null || true

        # Rule 1: Allow DHCP traffic (UDP 67/68)
        nft add rule bridge cape-isolation forward-vms meta iif "${BRIDGE_NAME}" udp sport 67 udp dport 68 accept 2>/dev/null

        # Rule 2: Allow traffic TO gateway from VMs
        nft add rule bridge cape-isolation forward-vms meta iif "${BRIDGE_NAME}" ip daddr ${GATEWAY_IP} accept 2>/dev/null

        # Rule 3: Allow traffic FROM gateway to VMs (responses)
        nft add rule bridge cape-isolation forward-vms meta oif "${BRIDGE_NAME}" ip saddr ${GATEWAY_IP} accept 2>/dev/null

        # Rule 4: Block all other traffic on this bridge (VMs can't reach each other or other networks)
        nft add rule bridge cape-isolation forward-vms meta iif "${BRIDGE_NAME}" meta oif "${BRIDGE_NAME}" drop 2>/dev/null
        nft add rule bridge cape-isolation forward-vms meta iif "${BRIDGE_NAME}" drop 2>/dev/null

        echo -e "    ${GREEN}✓ nftables rules applied${NC}"

        # Save rules for persistence
        cat > "${RULES_FILE}" << 'NFT_RULES_EOF'
# CAPE Sandbox Network Isolation Rules - Auto-generated
# Network: NETWORK_NAME_PLACEHOLDER
# Bridge: BRIDGE_PLACEHOLDER
# Gateway: GATEWAY_PLACEHOLDER
# Generated: TIMESTAMP_PLACEHOLDER
#
# These rules isolate VMs from each other and RFC1918 networks
# while allowing routing through the gateway to external networks (tun0)

table bridge cape-isolation {
  chain forward-vms {
    type filter hook forward priority -200; policy accept;

    # Allow DHCP (UDP 67/68)
    meta iif "BRIDGE_PLACEHOLDER" udp sport 67 udp dport 68 accept

    # Allow traffic TO gateway from VMs
    meta iif "BRIDGE_PLACEHOLDER" ip daddr GATEWAY_PLACEHOLDER accept

    # Allow traffic FROM gateway to VMs
    meta oif "BRIDGE_PLACEHOLDER" ip saddr GATEWAY_PLACEHOLDER accept

    # Block VM-to-VM and VM-to-other-networks
    meta iif "BRIDGE_PLACEHOLDER" meta oif "BRIDGE_PLACEHOLDER" drop
    meta iif "BRIDGE_PLACEHOLDER" drop
  }
}
NFT_RULES_EOF

        # Replace placeholders
        sed -i "s|NETWORK_NAME_PLACEHOLDER|${VM_NETWORK}|g" "${RULES_FILE}"
        sed -i "s|BRIDGE_PLACEHOLDER|${BRIDGE_NAME}|g" "${RULES_FILE}"
        sed -i "s|GATEWAY_PLACEHOLDER|${GATEWAY_IP}|g" "${RULES_FILE}"
        sed -i "s|TIMESTAMP_PLACEHOLDER|$(date)|g" "${RULES_FILE}"

        echo -e "    ${GREEN}✓ Rules saved to: ${RULES_FILE}${NC}"

        return 0

    elif command -v iptables &> /dev/null; then
        echo -e "    Using iptables for network isolation"

        # Allow traffic to gateway
        iptables -I FORWARD -i ${BRIDGE_NAME} -d ${GATEWAY_IP} -j ACCEPT 2>/dev/null || true
        iptables -I FORWARD -o ${BRIDGE_NAME} -s ${GATEWAY_IP} -j ACCEPT 2>/dev/null || true

        # Block all VM-to-anything-else traffic
        iptables -I FORWARD -i ${BRIDGE_NAME} ! -d ${GATEWAY_IP} -j DROP 2>/dev/null || true

        echo -e "    ${GREEN}✓ iptables rules applied${NC}"

        # Save current iptables rules
        if command -v iptables-save &> /dev/null; then
            iptables-save > "${RULES_FILE}.iptables" 2>/dev/null || true
            echo -e "    ${GREEN}✓ Rules saved to: ${RULES_FILE}.iptables${NC}"
        fi

        return 0

    else
        echo -e "    ${RED}[!] ERROR: Neither nftables nor iptables found${NC}"
        echo -e "    ${RED}[!] Cannot apply network isolation rules${NC}"
        return 1
    fi
}

# Function to create systemd service for persistent firewall
create_firewall_service() {
    local SERVICE_FILE="/etc/systemd/system/cape-firewall-isolation.service"
    local RULES_DIR="/etc/cape/firewall"

    if [ ! -f "$SERVICE_FILE" ]; then
        echo -e "    Creating systemd service for persistent firewall rules..."

        cat > "$SERVICE_FILE" << 'SYSTEMD_SERVICE_EOF'
[Unit]
Description=CAPE Sandbox Network Isolation Firewall Rules
After=network.target libvirtd.service
Before=cape.service

[Service]
Type=oneshot
ExecStart=/bin/bash /etc/cape/firewall/load-rules.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
SYSTEMD_SERVICE_EOF

        chmod 644 "$SERVICE_FILE"

        # Create the rule loader script
        cat > "${RULES_DIR}/load-rules.sh" << 'LOADER_SCRIPT_EOF'
#!/bin/bash
# CAPE Firewall Rules Loader
# Loads persisted firewall rules at boot

echo "Loading CAPE sandbox network isolation rules..."

RULES_DIR="/etc/cape/firewall"

if command -v nft &> /dev/null; then
    # Load nftables rules
    for rule_file in "$RULES_DIR"/rules-*.nft; do
        if [ -f "$rule_file" ]; then
            echo "  Loading: $(basename $rule_file)"
            nft -f "$rule_file" 2>/dev/null || echo "    Warning: Failed to load $rule_file"
        fi
    done
elif command -v iptables-restore &> /dev/null; then
    # Load iptables rules
    for rule_file in "$RULES_DIR"/rules-*.iptables; do
        if [ -f "$rule_file" ]; then
            echo "  Loading: $(basename $rule_file)"
            iptables-restore < "$rule_file" 2>/dev/null || echo "    Warning: Failed to load $rule_file"
        fi
    done
fi

echo "CAPE firewall isolation rules loaded."
LOADER_SCRIPT_EOF

        chmod 755 "${RULES_DIR}/load-rules.sh"

        # Enable and start the service
        systemctl daemon-reload 2>/dev/null || true
        systemctl enable cape-firewall-isolation.service 2>/dev/null || true
        systemctl start cape-firewall-isolation.service 2>/dev/null || true

        echo -e "    ${GREEN}✓ Systemd service created and enabled${NC}"
        echo -e "    ${GREEN}✓ Service file: ${SERVICE_FILE}${NC}"
    else
        echo -e "    ${YELLOW}[!] Systemd service already exists${NC}"
    fi
}

apply_firewall_rules

# Create systemd service for persistence (requires sudo)
if [ "$EUID" -eq 0 ]; then
    create_firewall_service
else
    echo -e "    ${YELLOW}[!] Skipping systemd service creation (requires root)${NC}"
fi

echo ""
echo -e "${BLUE}[*] Network Isolation Details:${NC}"
echo -e "    Gateway (DHCP/CAPE Server): ${GATEWAY_IP}"
echo -e "    VM Network: ${NETWORK_CIDR}"
echo -e "    VM-to-VM traffic: ${RED}BLOCKED${NC}"
echo -e "    VM-to-RFC1918 traffic: ${RED}BLOCKED${NC}"
echo -e "    VM-to-Internet (via tun0): ${GREEN}ALLOWED (through gateway)${NC}"
echo ""

# Arrays to track clones created
declare -a CLONES_CREATED
declare -a CLONE_IPS
declare -a CLONE_MACS

# Clone VMs
for i in $(seq "$START_NUM" "$END_NUM"); do
    CLONE_NAME="${CLONE_NAME_PREFIX}_${i}"
    CLONE_DISK="${DISK_BASE_PATH}/${CLONE_NAME}.qcow2"
    CLONE_IP="${NETWORK_BASE}.${i}"

    # Check if clone already exists
    if virsh list --all | grep -q "$CLONE_NAME"; then
        echo -e "${YELLOW}[!] VM $CLONE_NAME already exists, skipping${NC}"
        continue
    fi

    echo -e "${GREEN}[+] Creating clone: $CLONE_NAME${NC}"

    # Generate random MAC address
    # Using locally administered unicast MAC (02:xx:xx:xx:xx:xx)
    MAC_ADDR=$(printf '02:%02X:%02X:%02X:%02X:%02X' $((RANDOM%256)) $((RANDOM%256)) $((RANDOM%256)) $((RANDOM%256)) $((RANDOM%256)))

    # Create disk
    if [ "$CLONE_TYPE" = "linked" ]; then
        echo -e "    Creating linked clone disk..."
        qemu-img create -f qcow2 -F qcow2 -b "$TEMPLATE_DISK" "$CLONE_DISK" > /dev/null
    else
        echo -e "    Creating full clone disk..."
        cp "$TEMPLATE_DISK" "$CLONE_DISK"
    fi

    # Generate unique UUID and serial
    CLONE_UUID=$(uuidgen)
    CLONE_SERIAL="DLXSER$(head /dev/urandom | tr -dc A-Z0-9 | head -c 8)"

    # Dump template XML
    virsh dumpxml "$TEMPLATE_VM" > "/tmp/${CLONE_NAME}_temp.xml"

    # Modify XML for clone (following Python machinedupe.py approach)
    # 1. Change name
    sed -i "s|<name>$TEMPLATE_VM</name>|<name>$CLONE_NAME</name>|g" "/tmp/${CLONE_NAME}_temp.xml"

    # 2. Remove UUID entirely - libvirt will auto-generate (Python script approach)
    sed -i '/<uuid>.*<\/uuid>/d' "/tmp/${CLONE_NAME}_temp.xml"

    # 3. Update sysinfo UUID
    sed -i "s|<entry name='uuid'>.*</entry>|<entry name='uuid'>$CLONE_UUID</entry>|g" "/tmp/${CLONE_NAME}_temp.xml"

    # 4. Change disk path
    sed -i "s|$TEMPLATE_DISK|$CLONE_DISK|g" "/tmp/${CLONE_NAME}_temp.xml"

    # 5. Remove runtime/metadata elements that break clones
    sed -i '/<nvram/d' "/tmp/${CLONE_NAME}_temp.xml"
    sed -i '/<\/nvram>/d' "/tmp/${CLONE_NAME}_temp.xml"
    sed -i '/<backingStore\/>/d' "/tmp/${CLONE_NAME}_temp.xml"
    sed -i '/<backingStore>/,/<\/backingStore>/d' "/tmp/${CLONE_NAME}_temp.xml"

    # 6. Change MAC address
    sed -i "s|<mac address='[^']*'/>|<mac address='$MAC_ADDR'/>|g" "/tmp/${CLONE_NAME}_temp.xml"

    # 7. Change network to use the CAPE network
    sed -i "s|network='[^']*'|network='${VM_NETWORK}'|g" "/tmp/${CLONE_NAME}_temp.xml"

    # 8. Change serial numbers in SMBIOS
    sed -i "s|<entry name='serial'>.*</entry>|<entry name='serial'>$CLONE_SERIAL</entry>|g" "/tmp/${CLONE_NAME}_temp.xml"

    # 9. Remove TPM/UEFI-specific elements (using SeaBIOS, not UEFI)
    sed -i '/<tpm model/,/<\/tpm>/d' "/tmp/${CLONE_NAME}_temp.xml"
    sed -i '/<loader/d' "/tmp/${CLONE_NAME}_temp.xml"

    # Define the clone
    virsh define "/tmp/${CLONE_NAME}_temp.xml" > /dev/null

    # Add static DHCP entry (remove any stale entry for this IP first)
    STALE_MAC=$(virsh net-dumpxml ${VM_NETWORK} 2>/dev/null | grep "ip='${CLONE_IP}'" | grep -oP "mac='[^']+'" | cut -d"'" -f2)
    if [ -n "$STALE_MAC" ]; then
        echo -e "    Removing stale DHCP entry for ${CLONE_IP} (MAC: ${STALE_MAC})"
        virsh net-update ${VM_NETWORK} delete ip-dhcp-host \
            "<host mac='${STALE_MAC}' name='${CLONE_NAME}' ip='${CLONE_IP}'/>" \
            --live --config 2>/dev/null || true
    fi
    virsh net-update ${VM_NETWORK} add-last ip-dhcp-host \
        "<host mac='${MAC_ADDR}' name='${CLONE_NAME}' ip='${CLONE_IP}'/>" \
        --live --config 2>/dev/null || true

    # Track this clone
    CLONES_CREATED+=("$CLONE_NAME")
    CLONE_IPS+=("$CLONE_IP")
    CLONE_MACS+=("$MAC_ADDR")

    echo -e "${GREEN}    ✓ Clone created:${NC}"
    echo -e "      - Name: $CLONE_NAME"
    echo -e "      - IP: $CLONE_IP"
    echo -e "      - MAC: $MAC_ADDR"
    echo -e "      - UUID: $CLONE_UUID"
    echo -e "      - Disk: $CLONE_DISK"

    # Clean up temp XML
    rm -f "/tmp/${CLONE_NAME}_temp.xml"
done

echo ""
echo -e "${GREEN}[+] Cloning complete! Starting VMs and initializing...${NC}"
echo ""

# Pre-warm the backing qcow2 from EBS.  AWS EBS volumes restored from
# snapshot have lazy block initialization — the first read of an
# uninitialized block forces a slow path through S3 (multi-second
# latencies) before EBS marks the block "initialized" and serves
# subsequent reads at normal speed.
#
# This bit us hard: qemu's default error_policy='stop' pauses the VM
# on slow disk reads, and on deb-baked AMIs (snapshot-restored
# volumes) 1-5/24 VMs would consistently end up in libvirt 'paused'
# state during the first-boot read burst.  Legacy QCOW2-bootstrap
# deploys dodged this by writing the qcow2 fresh from S3 into a
# fully-allocated EBS volume — no lazy init, no slow reads.
#
# Reading the backing file once before booting any VM forces EBS to
# fully-allocate the blocks the clones will share.  For our 32G
# template on a 1000 MB/s gp3 it's ~30s wall.  The linked-clone
# disks are fully-allocated already (locally created by qemu-img
# create), so only the backing file needs warming.
TEMPLATE_DISK="/var/lib/libvirt/images/${CLONE_NAME_PREFIX}.qcow2"
if [ -f "$TEMPLATE_DISK" ]; then
    template_size_h=$(du -h "$TEMPLATE_DISK" | awk '{print $1}')
    echo -e "${YELLOW}[!] Pre-warming EBS-backed template disk ($template_size_h) to defeat lazy block init...${NC}"
    warmup_start=$(date +%s)
    dd if="$TEMPLATE_DISK" of=/dev/null bs=1M status=none iflag=direct
    warmup_secs=$(( $(date +%s) - warmup_start ))
    echo -e "    ${GREEN}Template disk warm (${warmup_secs}s).${NC}"
else
    echo -e "${RED}    Template disk not found at $TEMPLATE_DISK — skipping pre-warm${NC}"
fi
echo ""

# Boot all clones.  Track failures — silently continuing here is what
# let ami-bake ship AMIs with shutoff snapshots that CAPE then rejects
# at startup with "Snapshot is not in a 'running' state".  Earlier
# shape was `virsh start || echo "Failed to start"` then proceed to
# the 15-min warmup + snapshot phase regardless; we'd take a snapshot
# of every shutoff VM and ship the AMI.  Bake the failure into the
# script's exit code so ami-bake Phase 2 surfaces it.
START_FAILURES=()
# Boot in batches.  `virsh start` is non-blocking (qemu fork+exec
# returns sub-second), so unbatched fires all 24 qemu processes in
# ~5-10s and slams the EBS queue with concurrent qcow2 reads from
# the shared backing file + simultaneous Windows kernel inits.  On
# unlucky m8i.8xlarge / EBS placement the I/O contention causes
# qemu's default werror=stop to fire on some VMs (libvirt 'paused'
# state).  Caught on validation deploys off a baked AMI (5/24
# paused) and earlier baked AMIs.
#
# Strategy: start BOOT_BATCH_SIZE VMs simultaneously, wait
# BOOT_BATCH_INTERVAL_SECS for them to get past the heavy initial
# boot I/O, then start the next batch.  Defaults: 4 VMs every 3 min
# → 24 VMs across 6 batches = 15min of staggered starts.  Plus a
# final 15-min wait so the LAST batch gets full Windows first-boot
# time (Windows 11 OOBE typically needs 10-15 min).
#
# Total clone-phase time: 15 + 15 = 30 min.  Every VM gets ≥15 min
# of boot before snapshot; earlier batches get 18-30 min.
BOOT_BATCH_SIZE=4
BOOT_BATCH_INTERVAL_SECS=180
BOOT_FINAL_WAIT_SECS=900
if [ ${#CLONES_CREATED[@]} -gt 0 ]; then
    total_clones=${#CLONES_CREATED[@]}
    num_batches=$(( (total_clones + BOOT_BATCH_SIZE - 1) / BOOT_BATCH_SIZE ))
    echo -e "${YELLOW}[!] Booting $total_clones VMs in $num_batches batches of $BOOT_BATCH_SIZE (${BOOT_BATCH_INTERVAL_SECS}s between batches)...${NC}"
    for ((b=0; b<num_batches; b++)); do
        start=$((b * BOOT_BATCH_SIZE))
        end=$((start + BOOT_BATCH_SIZE))
        [ "$end" -gt "$total_clones" ] && end=$total_clones
        echo -e "${YELLOW}[!] Batch $((b+1))/$num_batches: starting ${CLONES_CREATED[@]:start:BOOT_BATCH_SIZE}${NC}"
        for ((j=start; j<end; j++)); do
            clone="${CLONES_CREATED[$j]}"
            echo -e "    Starting: $clone"
            if ! virsh start "$clone" > /dev/null 2>&1; then
                echo -e "${RED}    Failed to start $clone${NC}"
                virsh start "$clone" 2>&1 | sed 's/^/        /' || true
                START_FAILURES+=("$clone")
            fi
        done
        # Sleep between batches, but not after the last one — the
        # final-wait loop below handles that.
        if [ "$((b+1))" -lt "$num_batches" ]; then
            echo -e "    Waiting ${BOOT_BATCH_INTERVAL_SECS}s before next batch..."
            sleep "$BOOT_BATCH_INTERVAL_SECS"
        fi
    done

    if [ ${#START_FAILURES[@]} -gt 0 ]; then
        echo ""
        echo -e "${RED}[!] ${#START_FAILURES[@]} of $total_clones VMs failed to start: ${START_FAILURES[*]}${NC}"
        echo -e "${RED}    Aborting — proceeding to snapshot phase against shutoff VMs would${NC}"
        echo -e "${RED}    produce an AMI that CAPE rejects at startup.${NC}"
        exit 1
    fi

    echo ""
    final_wait_min=$((BOOT_FINAL_WAIT_SECS / 60))
    echo -e "${BLUE}[*] Waiting $final_wait_min minutes for last-batch VMs to reach steady state...${NC}"
    earliest_uptime_min=$(( ((num_batches - 1) * BOOT_BATCH_INTERVAL_SECS + BOOT_FINAL_WAIT_SECS) / 60 ))
    echo -e "    First-batch VMs will have $earliest_uptime_min min of boot time at snapshot"
    echo -e "    Last-batch VMs will have $final_wait_min min"
    echo -e "    Network: ${VM_NETWORK} (virbr${NETWORK_SUFFIX})"

    # Final wait — by the time this elapses, EVERY VM has had at least
    # BOOT_FINAL_WAIT_SECS of boot, and earlier batches have had more.
    for ((i=BOOT_FINAL_WAIT_SECS; i>0; i--)); do
        if (( i % 60 == 0 )); then
            echo -ne "    $(( i / 60 )) minutes remaining...\r"
        fi
        sleep 1
    done
    echo -e "    ${GREEN}Final wait complete!${NC}                  "

    # Defense in depth: verify every clone is still in 'running' state
    # before snapshotting.  Windows 11 first-boot is non-deterministic
    # — a small fraction of clones tend to BSOD during the initial
    # OOBE/specialize phase (likely SMBIOS-mismatch detection or KVM
    # paravirt-driver enumeration race).  Without retry, the warmup
    # finishes with 21/24 running and the script aborts; that turns
    # a transient Windows quirk into a hard deploy block.
    #
    # Retry policy: up to 3 attempts per failed VM.  For each pass:
    #   - `virsh start` the shutoff VM (idempotent on already-running)
    #   - wait 90s for Windows to reach a steady state again
    #   - re-check state
    # If any clone still isn't running after the retries, abort —
    # we'd rather refuse to ship than snapshot a known-broken VM.
    echo ""
    echo -e "${YELLOW}[!] Verifying all VMs running before snapshot...${NC}"
    for attempt in 1 2 3; do
        NOT_RUNNING=()
        for clone in "${CLONES_CREATED[@]}"; do
            state=$(virsh domstate "$clone" 2>/dev/null || echo unknown)
            if [ "$state" != "running" ]; then
                NOT_RUNNING+=("$clone")
            fi
        done
        if [ ${#NOT_RUNNING[@]} -eq 0 ]; then
            echo -e "    ${GREEN}All ${#CLONES_CREATED[@]} VMs running.${NC}"
            break
        fi
        echo -e "${YELLOW}[!] Attempt $attempt: ${#NOT_RUNNING[@]} VMs not running: ${NOT_RUNNING[*]}${NC}"
        if [ "$attempt" -eq 3 ]; then
            echo -e "${RED}[!] After 3 retries, ${#NOT_RUNNING[@]} VMs still not running.  Aborting before snapshot.${NC}"
            exit 1
        fi
        for clone in "${NOT_RUNNING[@]}"; do
            echo -e "    Restarting: $clone"
            virsh start "$clone" 2>&1 | sed 's/^/        /' || true
        done
        echo -e "    Waiting 90s for restarted VMs to re-stabilize..."
        sleep 90
    done

    # Parallel snapshot creation.  `virsh snapshot-create-as` on a
    # running 4GB-RAM Windows VM takes 30-60s (qcow2 internal snapshot
    # captures CPU + RAM + disk state).  Sequential = 12-24 min for
    # 24 VMs which busted the bake driver's 60-min budget in an
    # earlier bake run.  Snapshots are independent — fire all 24 in the
    # background and `wait` for completion.  Bounded I/O concurrency
    # by the same EBS queue, but per-VM is ~30s wall not 30s × 24.
    echo ""
    echo -e "${YELLOW}[!] Creating timestamped snapshots (parallel)...${NC}"
    snap_pids=()
    snap_log_dir=$(mktemp -d)
    for clone in "${CLONES_CREATED[@]}"; do
        echo -e "    Snapshotting: $clone -> $SNAPSHOT_NAME"
        (
            virsh snapshot-create-as "$clone" --name "$SNAPSHOT_NAME" \
                --description "Clean snapshot created $(date)" \
                > "$snap_log_dir/$clone.out" 2> "$snap_log_dir/$clone.err"
        ) &
        snap_pids+=("$!:$clone")
    done
    SNAPSHOT_FAILURES=()
    for pid_clone in "${snap_pids[@]}"; do
        pid="${pid_clone%%:*}"
        clone="${pid_clone##*:}"
        if ! wait "$pid"; then
            echo -e "${RED}    Failed to snapshot $clone${NC}"
            cat "$snap_log_dir/$clone.err" 2>/dev/null | sed 's/^/        /' || true
            SNAPSHOT_FAILURES+=("$clone")
        fi
    done
    rm -rf "$snap_log_dir"
    if [ ${#SNAPSHOT_FAILURES[@]} -gt 0 ]; then
        echo -e "${RED}[!] Snapshot failures: ${SNAPSHOT_FAILURES[*]}${NC}"
        exit 1
    fi

    echo ""
    echo -e "${GREEN}[+] Stopping VMs...${NC}"
    for clone in "${CLONES_CREATED[@]}"; do
        virsh shutdown "$clone" > /dev/null 2>&1 || true
    done

    # Wait for clean shutdown
    sleep 3
fi

# Generate cleanup script
echo ""
echo -e "${GREEN}[+] Generating cleanup script...${NC}"

CLEANUP_SCRIPT="${SCRIPT_DIR}/cleanup-${VM_NETWORK}-${TIMESTAMP}.sh"

cat > "$CLEANUP_SCRIPT" << 'CLEANUP_SCRIPT_START'
#!/bin/bash
# CAPE VM Cleanup Script - Auto-generated
# Generated: TIMESTAMP_PLACEHOLDER
# Network: NETWORK_NAME_PLACEHOLDER
# WARNING: This will DESTROY and DELETE all VMs and their disks!

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${RED}[!] WARNING: This will DELETE the following VMs and their disks:${NC}"
CLEANUP_SCRIPT_START

# Add VM list to cleanup script
for idx in "${!CLONES_CREATED[@]}"; do
    CLONE_NAME="${CLONES_CREATED[$idx]}"
    CLONE_DISK="${DISK_BASE_PATH}/${CLONE_NAME}.qcow2"

    cat >> "$CLEANUP_SCRIPT" << EOF
echo -e "    - ${CLONE_NAME}"
EOF
done

cat >> "$CLEANUP_SCRIPT" << 'CLEANUP_SCRIPT_MIDDLE'

echo ""
read -p "Are you sure you want to proceed? (type 'yes' to confirm): " confirm

if [ "$confirm" != "yes" ]; then
    echo -e "${YELLOW}[!] Cleanup cancelled${NC}"
    exit 0
fi

echo ""
echo -e "${GREEN}[+] Destroying and cleaning up VMs...${NC}"

CLEANUP_SCRIPT_MIDDLE

# Add cleanup commands for each VM
for idx in "${!CLONES_CREATED[@]}"; do
    CLONE_NAME="${CLONES_CREATED[$idx]}"
    CLONE_DISK="${DISK_BASE_PATH}/${CLONE_NAME}.qcow2"

    cat >> "$CLEANUP_SCRIPT" << EOF

# Cleanup: ${CLONE_NAME}
echo -e "    Destroying: ${CLONE_NAME}"
virsh destroy ${CLONE_NAME} 2>/dev/null || true
sleep 1

echo -e "    Deleting snapshots: ${CLONE_NAME}"
virsh snapshot-list ${CLONE_NAME} --name 2>/dev/null | while read snap; do
    [ -n "\$snap" ] && virsh snapshot-delete --domain ${CLONE_NAME} --snapshotname "\$snap" 2>/dev/null || true
done

echo -e "    Undefining: ${CLONE_NAME}"
virsh undefine ${CLONE_NAME} 2>/dev/null || true

echo -e "    Removing disk: ${CLONE_DISK}"
rm -f ${CLONE_DISK}
EOF
done

cat >> "$CLEANUP_SCRIPT" << 'CLEANUP_SCRIPT_END'

echo ""
echo -e "${GREEN}[+] Cleanup complete!${NC}"
echo -e "${YELLOW}[!] Note: Network and firewall rules were NOT removed${NC}"
echo -e "    To remove network manually:"
echo -e "    virsh net-destroy NETWORK_NAME_PLACEHOLDER && virsh net-undefine NETWORK_NAME_PLACEHOLDER"
CLEANUP_SCRIPT_END

# Update placeholders in cleanup script
sed -i "s|TIMESTAMP_PLACEHOLDER|$(date)|g" "$CLEANUP_SCRIPT"
sed -i "s|NETWORK_NAME_PLACEHOLDER|${VM_NETWORK}|g" "$CLEANUP_SCRIPT"

chmod +x "$CLEANUP_SCRIPT"

echo -e "${GREEN}[+] Cleanup script created:${NC}"
echo -e "    ${BLUE}${CLEANUP_SCRIPT}${NC}"
echo ""

# Generate CAPE configuration file
echo ""
echo -e "${GREEN}[+] Generating CAPE configuration file...${NC}"

MACHINES_LIST=$(IFS=,; echo "${CLONES_CREATED[*]}")

cat > "$CONFIG_FILE" << CAPE_CONFIG_START
# CAPE KVM Configuration - Auto-generated
# Generated: $(date)
# Network: ${VM_NETWORK} (virbr${NETWORK_SUFFIX})
# Gateway/CAPE Server: ${GATEWAY_IP}
# DNS Servers: ${DNS_SERVER_1}, ${DNS_SERVER_2}

[kvm]
machines = ${MACHINES_LIST}
interface = virbr${NETWORK_SUFFIX}
dsn = qemu:///system

CAPE_CONFIG_START

# Add VM configurations
for idx in "${!CLONES_CREATED[@]}"; do
    CLONE_NAME="${CLONES_CREATED[$idx]}"
    CLONE_IP="${CLONE_IPS[$idx]}"
    CLONE_MAC="${CLONE_MACS[$idx]}"
    cat >> "$CONFIG_FILE" << EOF

[${CLONE_NAME}]
label = ${CLONE_NAME}
platform = windows
arch = x64
ip = ${CLONE_IP}
snapshot = ${SNAPSHOT_NAME}
interface = virbr${NETWORK_SUFFIX}
resultserver_ip = ${GATEWAY_IP}
resultserver_port = 2${CLONE_IP##*.}
agent_port = 8000
# MAC: ${CLONE_MAC}
EOF
done

# Config generated directly - no placeholders to replace

# Ensure CAPE waits for libvirt network on boot
DROPIN_DIR="/etc/systemd/system/cape.service.d"
mkdir -p "$DROPIN_DIR"
cat > "${DROPIN_DIR}/network-deps.conf" << DROPIN_EOF
[Unit]
Wants=libvirtd.service
After=libvirtd.service

[Service]
# Ensure libvirt network is active before CAPE starts
ExecStartPre=+/bin/bash -c 'for i in 1 2 3 4 5; do virsh net-start ${VM_NETWORK} 2>/dev/null && break || sleep 2; done; virsh net-info ${VM_NETWORK} | grep -q "Active:.*yes"'
DROPIN_EOF
systemctl daemon-reload 2>/dev/null || true
echo -e "    ${GREEN}✓ Systemd drop-in created: ${DROPIN_DIR}/network-deps.conf${NC}"

echo -e "${GREEN}[+] Configuration file created:${NC}"
echo -e "    ${BLUE}${CONFIG_FILE}${NC}"
echo ""

# Display summary
echo -e "${GREEN}[+] Summary:${NC}"
echo -e "    Total VMs Created: ${#CLONES_CREATED[@]}"
echo -e "    Network: ${VM_NETWORK} (virbr${NETWORK_SUFFIX})"
echo -e "    IP Range: ${DHCP_START} - ${DHCP_END}"
echo -e "    Gateway: ${GATEWAY_IP}"
echo -e "    DNS: ${DNS_PRIMARY}, ${DNS_SECONDARY}"
echo -e "    Snapshot: ${SNAPSHOT_NAME}"
echo -e "    Config File: $(basename $CONFIG_FILE)"
echo -e "    Cleanup Script: $(basename $CLEANUP_SCRIPT)"
echo ""

# Install the generated config into /etc/cape/kvm.conf if cape-core is
# present (deb-baked AMI deploy path).  Without this, cape.service
# crashes at startup with "Domain not found: no domain with matching
# name 'cuckoo1'" because the shipped /etc/cape/kvm.conf carries
# upstream CAPE's `machines = cuckoo1` default — clone-win11-vms.sh
# defined win11_seabios_101..NN but never replaced the conffile.
# Falls back to printing the manual-append hint when /etc/cape is
# absent (legacy QCOW2-bootstrap hosts where the active config lives
# at /home/cape/CAPEv2/conf/kvm.conf).
if [ -d /etc/cape ] && [ -w /etc/cape ]; then
    install -m 0644 -o cape -g cape "${CONFIG_FILE}" /etc/cape/kvm.conf
    echo -e "${GREEN}[+] Installed kvm.conf:${NC} /etc/cape/kvm.conf"
    echo -e "    Source: ${CONFIG_FILE}"
    echo -e "    Machines: ${#CLONES_CREATED[@]}"
fi

echo -e "${GREEN}[+] Next Steps:${NC}"
echo -e "    1. Review and customize: ${CONFIG_FILE}"
if [ ! -d /etc/cape ]; then
    echo -e "    2. Append to CAPE config: cat ${CONFIG_FILE} >> /home/cape/CAPEv2/conf/kvm.conf"
fi
echo -e "    3. Start VMs: virsh start ${CLONES_CREATED[0]} (or batch start)"
echo -e "    4. Verify network connectivity: ping ${DHCP_START}"
echo -e "    5. Verify VPN/gateway: route inside VM should point to ${GATEWAY_IP}"
echo -e "    6. To cleanup all VMs: ${CLEANUP_SCRIPT}"
echo ""

echo -e "${GREEN}[+] Management Commands:${NC}"
echo -e "    Start all:     for i in {$START_NUM..$END_NUM}; do virsh start ${CLONE_NAME_PREFIX}_\$i; done"
echo -e "    Stop all:      for i in {$START_NUM..$END_NUM}; do virsh shutdown ${CLONE_NAME_PREFIX}_\$i; done"
echo -e "    Force stop:    for i in {$START_NUM..$END_NUM}; do virsh destroy ${CLONE_NAME_PREFIX}_\$i; done"
echo -e "    Delete all:    for i in {$START_NUM..$END_NUM}; do virsh undefine ${CLONE_NAME_PREFIX}_\$i; done"
echo -e "    Restore snap:  for i in {$START_NUM..$END_NUM}; do virsh snapshot-revert ${CLONE_NAME_PREFIX}_\$i ${SNAPSHOT_NAME}; done"
echo ""

# Show disk usage
echo -e "${GREEN}[+] Disk Usage:${NC}"
if [ "$CLONE_TYPE" = "linked" ]; then
    echo -e "    ${YELLOW}[!] Using linked clones - template disk is required${NC}"
fi
du -sh "${DISK_BASE_PATH}/${TEMPLATE_VM}"* 2>/dev/null | head -15

echo ""
echo -e "${YELLOW}[!] Important Notes:${NC}"
if [ "$CLONE_TYPE" = "linked" ]; then
    echo -e "    - DO NOT delete the template disk: $TEMPLATE_DISK"
    echo -e "    - Clones depend on template disk remaining intact"
fi
echo -e "    - Each VM has unique UUID, MAC, IP, and serial numbers"
echo -e "    - Using SeaBIOS (no UEFI/TPM/NVRAM)"
echo -e "    - TPM/UEFI entries removed from clones"
echo -e "    - Network: ${VM_NETWORK} with gateway at ${GATEWAY_IP} (this host)"
echo -e "    - VMs configured for CAPE with 15-minute initialization"
echo -e "    - DHCP pool: ${DHCP_START} - ${DHCP_END}"
echo ""

echo -e "${BLUE}[*] Network Routing Setup (on CAPE Server):${NC}"
echo -e "    To route VMs through tun0 to the internet:"
echo -e ""
echo -e "    1. Enable IP forwarding (persistent):"
echo -e "       ${YELLOW}echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf${NC}"
echo -e "       ${YELLOW}sysctl -p${NC}"
echo -e ""
echo -e "    2. Configure NAT for VM traffic to tun0:"
echo -e "       ${YELLOW}iptables -t nat -A POSTROUTING -i ${BRIDGE_NAME} -o tun0 -j MASQUERADE${NC}"
echo -e ""
echo -e "    3. Save NAT rules for persistence:"
echo -e "       ${YELLOW}iptables-save > /etc/cape/firewall/nat-${VM_NETWORK}.iptables${NC}"
echo -e ""
echo -e "    4. Create systemd service to load NAT rules:"
echo -e "       ${YELLOW}systemctl enable cape-firewall-isolation.service${NC}"
echo -e ""
echo -e "    VM Isolation Rules:"
echo -e "       - Automatically saved to: /etc/cape/firewall/rules-${VM_NETWORK}.nft"
echo -e "       - Loaded at boot via: cape-firewall-isolation.service"
echo -e ""
