#!/usr/bin/env bash
# cape-zircolite-build/fetch.sh — clone the Zircolite engine
# (github.com/wagga40/Zircolite) at a release tag into $WORK_DIR so
# package.sh can ship it to /opt/zircolite.
#
# This ships the TOOL (the sigma detection engine that CAPE's
# community/modules/processing/sigma.py shells out to).  The sigma
# RULE PACKS are a separate deb (cape-sigma-rules); the venv deps a
# separate change (cape-core pyproject).  See community/docs/
# sigma-integration.md for the full integration.
#
# Outputs:
#   $WORK_DIR/zircolite/            — the cloned tree (zircolite.py + lib)
#   $WORK_DIR/release-tag           — pinned tag, read by package.sh
#
# Inputs:
#   WORK_DIR            (required) staging dir
#   ZIRCOLITE_TAG       (optional) override the pinned tag (see DEFAULT below)
#   GH_TOKEN            (recommended) github API token for rate limits

set -euo pipefail

# Pinned Zircolite version — deliberately NOT "latest".
#
# Zircolite runs inside CAPE's shared poetry venv (sigma.py shells out via
# sys.executable), so its runtime deps must be satisfiable alongside cape's
# pinned dependency set.  Zircolite's requirements.txt drifts faster than
# cape's venv and tightens pins per release: e.g. v3.7.x moved
# `chardet` -> `chardet>=5.0,<6` and added `py7zr`, which is UNSATISFIABLE
# against cape's chardet==4.0.0 pin (chardet is load-bearing in cape's own
# report encoding detection — utils.py / litereport.py).  Tracking "latest"
# therefore silently breaks the sigma integration on the next upstream
# release.
#
# The pinned tag lives in the sibling `pinned-tag` file — the SINGLE source
# of truth shared with check-changed.sh, so the change-check compares the
# apt repo against the same version we actually build (otherwise the monthly
# cron compares against upstream "latest" and rebuilds needlessly every run).
# v3.2.0 is the version the reference build host runs and is proven
# compatible with cape's shared venv (chardet==4.0.0) — confirmed producing
# real detections + mitre_techniques there.  Bump it (edit pinned-tag) ONLY
# together with a re-validation of cape's pyproject venv against the new
# Zircolite requirements.txt.
DEFAULT_ZIRCOLITE_TAG="$(cat "$(dirname "${BASH_SOURCE[0]}")/pinned-tag")"

: "${WORK_DIR:?WORK_DIR required}"
log() { echo "[$(date -Iseconds)] [zircolite-fetch] $*"; }

mkdir -p "$WORK_DIR"

TAG="${ZIRCOLITE_TAG:-$DEFAULT_ZIRCOLITE_TAG}"
log "Using pinned Zircolite tag: $TAG (default ${DEFAULT_ZIRCOLITE_TAG}; reference-validated against cape's shared venv)"
echo "$TAG" > "$WORK_DIR/release-tag"

rm -rf "$WORK_DIR/zircolite"
log "Cloning wagga40/Zircolite @ $TAG"
git clone --depth=1 --branch "$TAG" \
    https://github.com/wagga40/Zircolite.git "$WORK_DIR/zircolite" >/dev/null 2>&1 \
    || { echo "::error::clone failed for tag $TAG"; exit 1; }

# Sanity: the entrypoint sigma.py invokes must exist.
[ -f "$WORK_DIR/zircolite/zircolite.py" ] \
    || { echo "::error::zircolite.py missing from clone — upstream layout changed?"; exit 1; }

# Drop the upstream .git + the bundled rules/ (we ship rule packs via
# cape-sigma-rules; keeping Zircolite's own rules/ would just bloat the
# deb with a second, unmanaged copy).  Keep config/ + templates/ + lib —
# zircolite.py needs them at runtime.
rm -rf "$WORK_DIR/zircolite/.git" "$WORK_DIR/zircolite/.github"
log "Staged Zircolite $TAG ($(du -sh "$WORK_DIR/zircolite" | awk '{print $1}'))"
ls "$WORK_DIR/zircolite/" | head -20 >&2
