#!/usr/bin/env bash
# suricata-rules-build/compile.sh — run suricata-update against ET
# Open (suricata-update's built-in source) + any cape-project additions
# to produce the merged ruleset.
#
# suricata-update gives us sid de-duplication, classification.config
# / reference.config merging, an enable/disable-list slot we can
# grow into, and a validation pass.  Output stays correct as the
# source set grows.
#
# Inputs:
#   WORK_DIR          required, from fetch.sh
#
# Outputs at $WORK_DIR/compiled/:
#   suricata.rules            (suricata-update merged output)
#   classification.config     (merged from all sources)
#   reference.config          (merged)
#   build-meta.txt            (counts + source breakdown)

set -euo pipefail

: "${WORK_DIR:?WORK_DIR required}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CAPE_RULES_DIR="$REPO_ROOT/suricata-rules/sources/cape"
OUT_DIR="$WORK_DIR/compiled"
SU_CONFIG="$WORK_DIR/suricata-update.yaml"
SU_DATADIR="$WORK_DIR/su-state"

log() { echo "[$(date -Iseconds)] [suricata-rules-compile] $*"; }

mkdir -p "$OUT_DIR" "$SU_DATADIR"

# Make sure suricata-update is on PATH. pip-installed by the workflow's
# "Install build deps" step; fall back to pip install here for direct
# local runs.
if ! command -v suricata-update >/dev/null 2>&1; then
    log "suricata-update not on PATH; installing via pip --user"
    python3 -m pip install --quiet --user 'suricata-update>=1.3'
    export PATH="$HOME/.local/bin:$PATH"
fi

# suricata-update needs three steps to use the built-in 'et/open'
# source the way we want:
#
#   1. update-sources    download the index of built-in sources
#                        (et/open + et/pro + others) into the
#                        data-dir.  Without this, enable-source
#                        fails with 'unknown source: et/open'.
#   2. enable-source     mark et/open as enabled in the data-dir's
#                        per-source state.
#   3. update            fetch the enabled sources + merge with
#                        local:* + write the output.
#
# Each is a separate CLI subcommand with its own arg parser.  Mixing
# global flags with subcommand-specific positional/flag args put us
# in argparse traps in PRs #98 + #101 — keep them separate.

# Config: empty 'sources:' (suricata-update keeps source state in
# the data-dir, not the config); 'local:' picks up any cape rule
# files via explicit enumeration (suricata-update doesn't glob-
# expand local paths — testing showed the literal '*.rules'
# string being passed to open() and ENOENT'ing).
CAPE_RULES_LINES=""
if find "$CAPE_RULES_DIR" -maxdepth 1 -name '*.rules' -type f 2>/dev/null | grep -q .; then
    while IFS= read -r f; do
        CAPE_RULES_LINES+=$'\n'"  - $f"
    done < <(find "$CAPE_RULES_DIR" -maxdepth 1 -name '*.rules' -type f | sort)
fi

cat > "$SU_CONFIG" <<EOF
# Sources are managed via suricata-update's data-dir state
# (enable-source / disable-source), not this config.  Leaving
# 'sources:' empty here is intentional.

# Local rule files appended after the upstream et/open merge.
# Enumerated explicitly because suricata-update doesn't expand
# globs in the 'local:' list — it open()s each entry as a literal
# path.  Empty list when no cape-project additions exist yet.
local:${CAPE_RULES_LINES}
EOF

log "Step 1/3: download built-in source index (update-sources)"
suricata-update update-sources --data-dir "$SU_DATADIR"

log "Step 2/3: enable et/open"
suricata-update enable-source --data-dir "$SU_DATADIR" et/open

log "Step 3/3: fetch + merge (default 'update' command)"
# --no-test    : don't shell out to `suricata -T` for validation —
#                we don't have Suricata installed on the GHA runner.
# --suricata-conf /dev/null
#              : prevents suricata-update from reading the system
#                /etc/suricata/suricata.yaml (none on the runner).
# --output     : where to write merged suricata.rules.
# --config     : our pipeline-specific local-files config.
# --reload-command=''
#              : skip the post-update suricata-reload hook.
suricata-update \
    --no-test \
    --data-dir "$SU_DATADIR" \
    --suricata-conf /dev/null \
    --output "$OUT_DIR" \
    --config "$SU_CONFIG" \
    --reload-command='' \
    2>&1 | tail -40

[ -f "$OUT_DIR/suricata.rules" ] || {
    log "::error::suricata-update did not produce suricata.rules"
    ls -la "$OUT_DIR" || true
    exit 1
}

# Surface counts for the build-meta + sanity gate.
# `grep -cE ... || RULE_COUNT=0` — capture grep's exit code on the
# OR side, NOT echo into the substitution.  Earlier shape
# `$(grep -cE ... || echo 0)` produced "0\n0" when grep exited 1
# (no matches): grep printed its "0" count, THEN echo added
# another "0" — RULE_COUNT then failed [ "0\n0" -gt 10000 ] with
# 'integer expression expected' (testing).
if RULE_COUNT=$(grep -cE '^[[:space:]]*(alert|drop|reject|pass|log)[[:space:]]' "$OUT_DIR/suricata.rules"); then
    :
else
    RULE_COUNT=0
fi
if DISABLED_COUNT=$(grep -cE '^[[:space:]]*#[[:space:]]*(alert|drop|reject|pass|log)[[:space:]]' "$OUT_DIR/suricata.rules"); then
    :
else
    DISABLED_COUNT=0
fi

cat > "$OUT_DIR/build-meta.txt" <<EOF
cape-rules-suricata-rules build metadata
==========================================
Build date           : $(date -Iseconds)
ET Open version      : ${SURICATA_VERSION:-7.0.13}
ET Open upstream md5 : $(cat "$WORK_DIR/et-open.md5" 2>/dev/null || echo unknown)
Active rules         : ${RULE_COUNT}
Disabled rules       : ${DISABLED_COUNT}
suricata-update      : $(suricata-update --version 2>&1 | head -1)
Cape sources dir  : $CAPE_RULES_DIR
Cape rule files   : $(find "$CAPE_RULES_DIR" -name '*.rules' -type f 2>/dev/null | wc -l | tr -d ' ')
EOF
log "Build metadata:"
cat "$OUT_DIR/build-meta.txt"

[ "$RULE_COUNT" -gt 10000 ] || { log "::error::implausibly few active rules ($RULE_COUNT)"; exit 1; }
log "compile complete"
