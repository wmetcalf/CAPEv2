#!/usr/bin/env bash
#
# aws-nested-virt-reroot-buildtest.sh
# ------------------------------------------------------------------------------
# Acceptance harness for the M5 "debian re-root" (branch feat/cape-deb-reroot).
#
# It SIMULATES the fork layout — CAPE source at the source-package ROOT, the
# sandbox pipeline machinery under aws-nested-virt/ — from the current portable
# tree, then runs the M2 build recipe (the same one cape-build.yml uses) inside
# an ubuntu:24.04 container to prove `dpkg-buildpackage` produces cape-core.deb
# and cape-signatures.deb from that layout.
#
# The portable tree stores CAPE under cape/ and the machinery at the repo root;
# the fork inverts that. This script builds a staging copy shaped like the fork:
#
#   <staging>/                      <- source-package ROOT (holds debian/)
#   ├── <CAPE source contents>      <- from portable cape/. (lib, web, conf,
#   │                                  systemd, pyproject.toml, poetry.lock, …)
#   ├── community/                  <- root sibling (cape-signatures' tree)
#   ├── aws-nested-virt/            <- ALL pipeline machinery
#   │   ├── debian/                 <-   (packer/, *-build/, scripts/, .github/,
#   │   ├── scripts/                <-    terraform/, tests/, keys/, …)
#   │   └── …
#   └── debian/                     <- staged: `cp -r aws-nested-virt/debian ./debian`
#
# The re-rooted debian/rules + debian/cape-core.install then reference the CAPE
# tree at `.` (not cape/) and the sandbox scripts at aws-nested-virt/scripts.
#
# USAGE
#   ./aws-nested-virt-reroot-buildtest.sh                 # stage + container build
#   DRY_RUN=1 ./aws-nested-virt-reroot-buildtest.sh       # stage + print layout only
#   SRC=/path/to/portable ./aws-nested-virt-reroot-buildtest.sh
#   KEEP=1 ./aws-nested-virt-reroot-buildtest.sh          # keep the staging dir
#
# The container build is long (~20-40 min: dh-virtualenv compiles lief /
# yara-python / libvirt-python from source). Run it from the controller, not
# inline. DRY_RUN validates only the staging construction.
# ------------------------------------------------------------------------------
set -euo pipefail

# --- config -------------------------------------------------------------------
SRC="${SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
IMAGE="${IMAGE:-ubuntu:24.04}"
PKG_DIR_NAME="sandbox-cape"           # the source-package dir (== fork root sim)
DRY_RUN="${DRY_RUN:-0}"
KEEP="${KEEP:-0}"

# docker may need sudo; honour DOCKER if the caller wants `podman` etc.
DOCKER="${DOCKER:-sudo docker}"

log()  { printf '\033[1;34m[buildtest]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[buildtest] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ -d "$SRC/cape" ]      || die "no cape/ under SRC=$SRC (not a portable tree?)"
[ -d "$SRC/community" ] || die "no community/ under SRC=$SRC"
[ -d "$SRC/debian" ]    || die "no debian/ under SRC=$SRC"
[ -f "$SRC/cape/pyproject.toml" ] || die "SRC/cape/pyproject.toml missing (CAPE subtree not populated)"

# --- staging ------------------------------------------------------------------
WORK="$(mktemp -d /tmp/reroot-buildtest.XXXXXX)"   # mounted to /work
STAGING="$WORK/$PKG_DIR_NAME"                        # workdir inside container
mkdir -p "$STAGING/aws-nested-virt"

cleanup() {
  if [ "$KEEP" = "1" ]; then
    log "KEEP=1 — leaving staging tree at: $WORK"
  else
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT

log "SRC     = $SRC"
log "staging = $STAGING"

# 1a. CAPE source CONTENTS -> staging ROOT (this is the re-root: CAPE at `.`)
log "copying cape/. -> staging root"
cp -a "$SRC/cape/." "$STAGING/"

# 1b. community/ -> staging ROOT sibling (cape-signatures.install is root-relative)
log "copying community/ -> staging root (sibling)"
cp -a "$SRC/community" "$STAGING/"

# 1c. everything else (pipeline machinery) -> staging/aws-nested-virt/
#     debian/, packer/, *-build/, scripts/, .github/, terraform/, tests/, keys/, …
log "copying pipeline machinery -> staging/aws-nested-virt/"
shopt -s dotglob nullglob
for entry in "$SRC"/*; do
  name="$(basename "$entry")"
  case "$name" in
    cape|community) continue ;;   # already placed at the root
    .git)           continue ;;   # never copy the real repo metadata
  esac
  cp -a "$entry" "$STAGING/aws-nested-virt/"
done
shopt -u dotglob nullglob

# 1d. stage debian/ at the fork root — the exact CI step:
#     `cp -r aws-nested-virt/debian ./debian`
log "staging debian/ at fork root (cp -r aws-nested-virt/debian ./debian)"
[ -d "$STAGING/aws-nested-virt/debian" ] || die "aws-nested-virt/debian missing after machinery copy"
rm -rf "$STAGING/debian"
cp -r "$STAGING/aws-nested-virt/debian" "$STAGING/debian"

# --- staging sanity (structural, fast) ---------------------------------------
log "staged layout (top of fork root):"
( cd "$STAGING" && ls -1 | sed 's/^/    /' | head -40 )
echo "    ---"
for must in pyproject.toml poetry.lock conf/default systemd community aws-nested-virt/scripts debian/rules debian/control; do
  if [ -e "$STAGING/$must" ]; then
    printf '    [ok]      %s\n' "$must"
  else
    printf '    [MISSING] %s\n' "$must"
    die "expected staged path missing: $must"
  fi
done
# These must NOT be at the fork root (they must live under aws-nested-virt/):
for badroot in scripts packer terraform; do
  if [ -e "$STAGING/$badroot" ]; then
    die "unexpected: '$badroot' at fork root — it belongs under aws-nested-virt/"
  fi
done

if [ "$DRY_RUN" = "1" ]; then
  log "DRY_RUN=1 — staging built and validated; skipping container build."
  log "staging kept at: $WORK (inspect: ls -R $STAGING)"
  KEEP=1
  exit 0
fi

# --- container build recipe (M2) ---------------------------------------------
# Written to a file so we avoid nested-quote hell in `docker run bash -c`.
RECIPE="$WORK/build-recipe.sh"
cat > "$RECIPE" <<'RECIPE_EOF'
#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# Base toolchain — mirrors cape-build.yml's "Install base packaging toolchain".
# The heavy lib*-dev set + rsync come from debian/control's Build-Depends via
# mk-build-deps below, keeping debian/control the single source of truth. rsync
# is listed here too so the recipe is self-evidently complete if run standalone.
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    build-essential debhelper devscripts dh-virtualenv equivs \
    python3.12 python3.12-venv python3.12-dev python3-pip pipx python3-virtualenv \
    git curl ca-certificates lintian rsync

# poetry (+ export plugin) — debian/rules' override_dh_virtualenv runs
# `poetry export` from the package root to freeze requirements.txt.
pipx install poetry
pipx inject poetry poetry-plugin-export
export PATH="$HOME/.local/bin:$PATH"
poetry --version

# Container runs as root over a bind mount owned by another uid.
git config --global --add safe.directory "$PWD" || true

# Install Build-Depends from debian/control (source of truth), incl. rsync.
mk-build-deps --install --remove \
    --tool='apt-get -y -o Debug::pkgProblemResolver=yes --no-install-recommends' \
    debian/control

# The re-rooted build: CAPE is the package root, debian/ is staged at `.`.
dpkg-buildpackage -us -uc -b

echo "=== built artifacts (dpkg-buildpackage writes to the parent dir) ==="
ls -la .. | grep -E 'cape-.*\.deb|sandbox-cape_.*\.(changes|buildinfo)' || true
RECIPE_EOF
chmod +x "$RECIPE"

log "launching container build: $IMAGE  (workdir /work/$PKG_DIR_NAME)"
log "  (this is the long step — dh-virtualenv compiles native wheels)"
$DOCKER run --rm \
  -v "$WORK":/work \
  -w "/work/$PKG_DIR_NAME" \
  "$IMAGE" \
  bash /work/build-recipe.sh

# --- report -------------------------------------------------------------------
log "container build finished. .debs land in the parent of the package dir:"
log "  host path: $WORK/"
shopt -s nullglob
debs=( "$WORK"/cape-*.deb )
if [ "${#debs[@]}" -gt 0 ]; then
  for d in "${debs[@]}"; do printf '    %s\n' "$d"; done
  KEEP=1
  log "SUCCESS — set KEEP=1 to preserve; artifacts above."
else
  die "no cape-*.deb produced under $WORK — inspect the container output above."
fi
