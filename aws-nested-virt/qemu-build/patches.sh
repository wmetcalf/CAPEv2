#!/usr/bin/env bash
# qemu-build/patches.sh — sourceable anti-VM-detection patch functions.
#
# Extracted from the live-installer's kvm-qemu.sh so the same patch logic
# runs in CI without the script's apt/sudo/system-config side effects.
#
# Source this file from qemu-build/build.sh AFTER cd'ing into the qemu
# source tree, then call:
#
#   apply_qemu_patches "$qemu_source_dir"
#   apply_seabios_patches "$qemu_source_dir/roms/seabios"
#
# The functions return non-zero on hard failures only; missing target
# files in newer/older QEMU versions are warnings (sandbox malware
# detection patches drift across upstream refactors).

set -u

# ---------------------------------------------------------------------------
# Replacement strings — chosen to mimic real consumer hardware. Keep these
# in sync with the live-installer's kvm-qemu.sh so VMs cloned from either
# build path expose identical anti-VM signatures.
# ---------------------------------------------------------------------------

CPUID="${CPUID:-Intel(R) Core(TM) i7-7700 CPU @ }"
QEMU_HD_REPLACEMENT="${QEMU_HD_REPLACEMENT:-SAMSUNG MZ76E120}"
QEMU_DVD_REPLACEMENT="${QEMU_DVD_REPLACEMENT:-HL-PQ-SV WB8}"
PEN_REPLACER="${PEN_REPLACER:-Wacom}"
SCSI_REPLACER="${SCSI_REPLACER:-INTEL}"
ATAPI_REPLACER="${ATAPI_REPLACER:-LITEON}"
MICRODRIVE_REPLACER="${MICRODRIVE_REPLACER:-SANDISK}"
BOCHS_BLOCK_REPLACER="${BOCHS_BLOCK_REPLACER:-intel}"
BXPC_REPLACER="${BXPC_REPLACER:-INTL}"

# ---------------------------------------------------------------------------
# Helpers — run sed -i on a file if it exists, warn on miss. Sandbox
# malware detection patches need to fail-soft because the same patch
# library targets a sliding window of QEMU upstream versions.
# ---------------------------------------------------------------------------

_sed_aux() {
    # _sed_aux <sed-pattern> <file> [warning-message]
    local pattern="$1" file="$2" msg="${3:-}"
    if [ ! -f "$file" ]; then
        [ -n "$msg" ] && echo "[!] $msg (target file not found: $file)" >&2
        return 0
    fi
    if ! sed -i "$pattern" "$file"; then
        [ -n "$msg" ] && echo "[!] $msg" >&2
    fi
}

# ---------------------------------------------------------------------------
# CPUID hypervisor signature — replace KVMKVMKVM with GenuineIntel.
# Without this, malware grepping CPUID leaf 0x40000000 for "KVMKVMKVM"
# trivially detects KVM hosts.
# ---------------------------------------------------------------------------

_patch_hypervisor_signatures() {
    local qemu_dir="$1"
    local kvm_file="$qemu_dir/target/i386/kvm/kvm.c"
    local para_hdr="$qemu_dir/include/standard-headers/asm-x86/kvm_para.h"

    # QEMU 9.2.x stores the KVM CPUID signature as a 12-byte literal
    # `"KVMKVMKVM\0\0\0"` (9 ASCII + 3 explicit null bytes) in two places:
    #   target/i386/kvm/kvm.c:           `memcpy(signature, "KVMKVMKVM\0\0\0", 12);`
    #   include/.../asm-x86/kvm_para.h:  `#define KVM_SIGNATURE "KVMKVMKVM\0\0\0"`
    # Replace with `"GenuineIntel"` — exactly 12 ASCII bytes, drop-in.
    #
    # Also handle the legacy `"KVMKVMKVM"` (no embedded nulls) form for
    # QEMU < 8.x, plus the hex-encoded variant some forks ship.

    local replaced=0
    for f in "$kvm_file" "$para_hdr"; do
        [ -f "$f" ] || continue
        sed -i 's/"KVMKVMKVM\\0\\0\\0"/"GenuineIntel"/g' "$f" || true
        sed -i 's/"KVMKVMKVM"/"GenuineIntel"/g'              "$f" || true
        sed -i 's/\\x4b\\x56\\x4d\\x4b\\x56\\x4d\\x4b\\x56\\x4d/\\x47\\x65\\x6e\\x75\\x69\\x6e\\x65\\x49\\x6e\\x74/g' "$f" || true
        if grep -q GenuineIntel "$f"; then
            replaced=$((replaced + 1))
            echo "[+] Hypervisor signature patched in $(basename "$f")"
        fi
    done

    if [ "$replaced" -eq 0 ]; then
        echo "[!] No KVMKVMKVM signature found in any expected file (QEMU version drift?)"
    fi
    # Verify the literal is gone from the binary's source view.
    if [ -f "$kvm_file" ] && grep -q '"KVMKVMKVM' "$kvm_file"; then
        echo "::error::patched kvm.c still contains a KVMKVMKVM literal"
        return 1
    fi
}

# ---------------------------------------------------------------------------
# CPU brand string — replace QEMU's host-cpu fill function so CPUID leaf
# 0x80000002–0x80000004 returns a fake retail CPU brand.
# ---------------------------------------------------------------------------

_patch_cpu_brand_strings() {
    local cpuid_str="$1"
    local qemu_dir="$2"
    local file="$qemu_dir/target/i386/host-cpu.c"
    [ -f "$file" ] || { echo "[!] $file not found"; return 0; }

    sed -i '/^static int host_cpu_fill_model_id/,/^}$/c\
static int host_cpu_fill_model_id(char *str)\
{\
    /* PATCHED: Use fake CPU brand to hide real host CPU */\
    const char *fake_brand = "'"$cpuid_str"'";\
    memset(str, 0, 48);\
    strncpy(str, fake_brand, 47);\
    return 0;\
}' "$file"

    grep -q "PATCHED: Use fake CPU brand" "$file" \
        && echo "[+] CPU brand string patched in host-cpu.c" \
        || { echo "[!] CPU brand patch verify failed"; return 1; }
}

# ---------------------------------------------------------------------------
# CPU model_id — replace .model_id = "..." entries in cpu.c so Windows'
# WMI queries don't see "Common KVM" / "Westmere" but the fake brand.
# ---------------------------------------------------------------------------

_patch_cpu_model_ids() {
    local cpuid_str="$1"
    local qemu_dir="$2"
    local file="$qemu_dir/target/i386/cpu.c"
    [ -f "$file" ] || { echo "[!] $file not found"; return 0; }

    # Strip trailing " CPU @ ..." for the model_id field (which expects
    # just the brand portion).
    local cpu_brand="${cpuid_str% CPU @ *}"

    sed -i 's/\.model_id = "[^"]*"/\.model_id = "'"$cpu_brand"'"/g' "$file" || true

    # Multi-line PropValue { "model-id", "..." } entries — handled by
    # python because sed can't easily span lines for these.
    if command -v python3 >/dev/null 2>&1; then
        python3 - "$file" "$cpu_brand" <<'PYEOF' || true
import re, sys, pathlib
p = pathlib.Path(sys.argv[1])
brand = sys.argv[2]
s = p.read_text()
new = re.sub(r'\{ "model-id",\s*"[^"]*" \}', f'{{ "model-id", "{brand}" }}', s)
if new != s:
    p.write_text(new)
    print("[+] PropValue model-id entries patched")
PYEOF
    fi

    echo "[+] CPU model_ids patched in cpu.c"
}

# ---------------------------------------------------------------------------
# Public-facing umbrella: applies all the QEMU-tree string replacements.
# Source code paths are relative to the qemu source dir argument.
# ---------------------------------------------------------------------------

apply_qemu_patches() {
    local qd="$1"
    [ -d "$qd" ] || { echo "::error::qemu source dir not found: $qd"; return 1; }

    echo "[+] Patching QEMU anti-VM-detection clues in $qd"

    _patch_cpu_brand_strings "$CPUID" "$qd"
    _patch_cpu_model_ids     "$CPUID" "$qd"

    # Disk + device strings.
    _sed_aux "s/QEMU HARDDISK/$QEMU_HD_REPLACEMENT/g"  "$qd/hw/ide/core.c"      'QEMU HARDDISK in core.c'
    _sed_aux "s/QEMU HARDDISK/$QEMU_HD_REPLACEMENT/g"  "$qd/hw/scsi/scsi-disk.c" 'QEMU HARDDISK in scsi-disk.c'
    _sed_aux "s/QEMU DVD-ROM/$QEMU_DVD_REPLACEMENT/g"  "$qd/hw/ide/core.c"       'QEMU DVD-ROM in core.c'
    _sed_aux "s/QEMU DVD-ROM/$QEMU_DVD_REPLACEMENT/g"  "$qd/hw/ide/atapi.c"      'QEMU DVD-ROM in atapi.c'
    _sed_aux "s/QEMU CD-ROM/$QEMU_DVD_REPLACEMENT/g"   "$qd/hw/scsi/scsi-disk.c" 'QEMU CD-ROM in scsi-disk.c'
    _sed_aux "s/QEMU PenPartner tablet/$PEN_REPLACER PenPartner tablet/g" \
                                                       "$qd/hw/usb/dev-wacom.c"  'QEMU PenPartner tablet'
    _sed_aux 's/s->vendor = g_strdup("QEMU");/s->vendor = g_strdup("'"$SCSI_REPLACER"'");/g' \
                                                       "$qd/hw/scsi/scsi-disk.c" 'SCSI vendor in scsi-disk.c'
    _sed_aux 's/padstr8(buf + 8, 8, "QEMU");/padstr8(buf + 8, 8, "'"$ATAPI_REPLACER"'");/g' \
                                                       "$qd/hw/ide/atapi.c"      'ATAPI padstr in atapi.c'
    _sed_aux "s/QEMU MICRODRIVE/$MICRODRIVE_REPLACER MICRODRIVE/g" \
                                                       "$qd/hw/ide/core.c"       'QEMU MICRODRIVE in core.c'

    # Hypervisor + Bochs/BXPC strings.
    _patch_hypervisor_signatures "$qd"
    _sed_aux 's/"bochs"/"'"$BOCHS_BLOCK_REPLACER"'"/g' "$qd/block/bochs.c"             'BOCHS in block/bochs.c'
    _sed_aux 's/"BOCHS "/"ALASKA"/g'                    "$qd/include/hw/acpi/aml-build.h" 'BOCHS in aml-build.h'
    _sed_aux 's/Bochs Pseudo/Intel RealTime/g'          "$qd/roms/ipxe/src/drivers/net/pnic.c" 'Bochs Pseudo in pnic.c'
    _sed_aux 's/BXPC/'"$BXPC_REPLACER"'/g'              "$qd/include/hw/acpi/aml-build.h" 'BXPC in aml-build.h'

    # Input devices (keyboard / mouse / tablet) — common malware checks
    # iterate device descriptions looking for "QEMU".
    _sed_aux 's/"QEMU PS\/2 Keyboard"/"ASUS PS\/2 Keyboard"/g'   "$qd/hw/input/ps2.c"      'PS/2 Keyboard'
    _sed_aux 's/"QEMU HID Keyboard"/"ASUS HID Keyboard"/g'        "$qd/hw/input/hid.c"      'HID Keyboard'
    _sed_aux 's/"QEMU USB Keyboard"/"ASUS USB Keyboard"/g'        "$qd/hw/usb/dev-hid.c"    'USB Keyboard'
    _sed_aux 's/"QEMU ADB Keyboard"/"ASUS ADB Keyboard"/g'        "$qd/hw/input/adb-kbd.c"  'ADB Keyboard'
    _sed_aux 's/"QEMU USB Mouse"/"ASUS USB Mouse"/g'              "$qd/hw/usb/dev-hid.c"    'USB Mouse'
    _sed_aux 's/"QEMU USB Tablet"/"ASUS USB Tablet"/g'            "$qd/hw/usb/dev-hid.c"    'USB Tablet'
    _sed_aux 's/"QEMU Microsoft Mouse"/"ASUS Microsoft Mouse"/g'  "$qd/chardev/msmouse.c"   'MS Mouse'
    _sed_aux 's/"QEMU Wacom Pen Tablet"/"ASUS Wacom Pen Tablet"/g' "$qd/chardev/wctablet.c" 'Wacom Tablet'

    # USB descriptor strings.
    _sed_aux 's/\[STR_MANUFACTURER\][[:space:]]*=[[:space:]]*"QEMU"/[STR_MANUFACTURER]     = "ASUS"/g' \
        "$qd/hw/usb/dev-hid.c" 'USB Manufacturer'

    # NVMe controller name.
    _sed_aux 's/"QEMU NVMe Ctrl"/"ASUS NVMe Ctrl"/g' "$qd/hw/nvme/ctrl.c" 'NVMe Ctrl'

    # ACPI firmware / SMBIOS / BGRT — see kvm-qemu.sh for the full list.
    # We apply only the high-leverage ones here; lower-leverage ones can
    # be added incrementally if specific malware families slip through.
    _sed_aux 's/"QEMU0002"/"ASUS0002"/g' "$qd/hw/arm/virt-acpi-build.c" 'ACPI fw_cfg ID'
    _sed_aux 's/g_array_append_vals(array, ACPI_BUILD_APPNAME8, 4);/g_array_append_vals(array, "ASUS", 4);/g' \
        "$qd/hw/acpi/aml-build.c" 'ACPI Creator ID'
    _sed_aux 's/"QEMU VVFAT"/"ASUS VVFAT"/g'        "$qd/block/vvfat.c"   'VVFAT label'
    _sed_aux 's/g_utf8_to_utf16("QEMU v"/g_utf8_to_utf16("ASUS v"/g' "$qd/block/vhdx.c" 'VHDX creator'
    _sed_aux 's/smbios_set_defaults("QEMU",/smbios_set_defaults("ASUS",/g' \
        "$qd/hw/arm/virt-acpi-build.c" 'SMBIOS manufacturer'

    echo "[+] QEMU patches applied"
}

# ---------------------------------------------------------------------------
# SeaBIOS-side patches — applied to the bundled SeaBIOS source under
# qemu-source/roms/seabios/. The strings here show up in BIOS info that
# Windows reports via WMI.
# ---------------------------------------------------------------------------

apply_seabios_patches() {
    local sd="$1"
    [ -d "$sd" ] || { echo "::error::seabios source dir not found: $sd"; return 1; }

    echo "[+] Patching SeaBIOS clues in $sd"

    # The exact set of strings to replace lives across vgabios/, src/,
    # acpi-dsdt.dsl. Replicate the highest-leverage ones from
    # replace_seabios_clues_public; expand as needed.
    _sed_aux 's/Bochs Pseudo/Intel RealTime/g' "$sd/vgasrc/Kconfig"          'Bochs Pseudo in vgasrc Kconfig'
    _sed_aux 's/BOCHSCPU/INTELCPU/g'           "$sd/src/fw/acpi-dsdt.dsl"    'BOCHSCPU in DSDT'
    _sed_aux 's/QEMU\/Bochs/INTEL\/INTEL/g'    "$sd/vgasrc/Kconfig"          'QEMU/Bochs in vgasrc'
    _sed_aux 's/Bochs/Intel/g'                 "$sd/src/version.c"           'Bochs in version.c'

    echo "[+] SeaBIOS patches applied"
}
