#!/usr/bin/env bash
# Layer 1 — Build sanity manifest assertions.
#
# Runs after dpkg-buildpackage in CI. Validates that the produced .debs
# have the expected file layout, conffile lists, and apt metadata.
# Doesn't install anything; pure structural checks on the .deb files.

set -euo pipefail

DEBS_DIR="${1:-..}"

fail=0
note() { printf '  → %s\n' "$*"; }
ok()   { printf '✓ %s\n' "$*"; }
bad()  { printf '✗ %s\n' "$*" >&2; fail=1; }

assert_files_in_deb() {
    local deb="$1"; shift
    local listing
    listing=$(dpkg-deb -c "$deb" | awk '{print $NF}')
    for path in "$@"; do
        if echo "$listing" | grep -qxF "./$path"; then
            note "$(basename "$deb"): contains $path"
        else
            bad "$(basename "$deb"): missing $path"
        fi
    done
}

assert_no_files_in_deb() {
    local deb="$1"; shift
    local listing
    listing=$(dpkg-deb -c "$deb" | awk '{print $NF}')
    for path in "$@"; do
        if echo "$listing" | grep -qxF "./$path"; then
            bad "$(basename "$deb"): unexpected file $path"
        else
            note "$(basename "$deb"): correctly excludes $path"
        fi
    done
}

assert_field() {
    local deb="$1" field="$2" expected="$3"
    local actual
    actual=$(dpkg-deb -f "$deb" "$field")
    if [[ "$actual" == *"$expected"* ]]; then
        note "$(basename "$deb"): $field contains '$expected'"
    else
        bad "$(basename "$deb"): $field='$actual' missing '$expected'"
    fi
}

# ---- cape-core ------------------------------------------------------------
core_deb=$(ls "$DEBS_DIR"/cape-core_*_amd64.deb 2>/dev/null | head -1)
if [[ -n "$core_deb" ]]; then
    ok "cape-core deb found: $core_deb"
    assert_field "$core_deb" Depends "python3.12"
    assert_field "$core_deb" Depends "qemu-kvm | cape-qemu"
    assert_field "$core_deb" Depends "suricata | cape-suricata"
    # Sanity: venv artifacts present, conffiles in place
    assert_files_in_deb "$core_deb" "opt/CAPEv2/.venv/" "etc/cape/cuckoo.conf"
else
    bad "cape-core deb not produced"
fi

# ---- cape-signatures ------------------------------------------------------
sigs_deb=$(ls "$DEBS_DIR"/cape-signatures_*_all.deb 2>/dev/null | head -1)
if [[ -n "$sigs_deb" ]]; then
    ok "cape-signatures deb found: $sigs_deb"
    # Critical: enforces sig/core ABI compat at apt level.
    assert_field "$sigs_deb" Depends "cape-core (>="
    assert_files_in_deb "$sigs_deb" "opt/CAPEv2/modules/signatures/" \
                                    "opt/CAPEv2/modules/parsers/"
else
    bad "cape-signatures deb not produced"
fi

# ---- cape-qemu ------------------------------------------------------------
qemu_deb=$(ls "$DEBS_DIR"/cape-qemu_*_amd64.deb 2>/dev/null | head -1)
if [[ -n "$qemu_deb" ]]; then
    ok "cape-qemu deb found: $qemu_deb"
    assert_field "$qemu_deb" Conflicts "qemu-system-x86"
    assert_field "$qemu_deb" Conflicts "seabios"
    assert_field "$qemu_deb" Provides  "qemu-system-x86"
    assert_field "$qemu_deb" Provides  "seabios"
    assert_files_in_deb "$qemu_deb" "usr/bin/qemu-system-x86_64" \
                                    "usr/share/qemu/bios.bin"
else
    bad "cape-qemu deb not produced"
fi

# ---- cape-suricata --------------------------------------------------------
sur_deb=$(ls "$DEBS_DIR"/cape-suricata_*_amd64.deb 2>/dev/null | head -1)
if [[ -n "$sur_deb" ]]; then
    ok "cape-suricata deb found: $sur_deb"
    assert_field "$sur_deb" Conflicts "suricata"
    assert_field "$sur_deb" Provides  "suricata"
    assert_files_in_deb "$sur_deb" "usr/bin/suricata" "usr/bin/suricata-update"
else
    bad "cape-suricata deb not produced"
fi

if [[ $fail -ne 0 ]]; then
    echo
    echo "Manifest assertions failed."
    exit 1
fi

echo
echo "All manifest assertions passed."
