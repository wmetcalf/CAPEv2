#!/usr/bin/env bash
set -euo pipefail
CHANNEL="" DEBS="" REPO="" GPGKEY="" DRYRUN=0
while [ $# -gt 0 ]; do case "$1" in
  --channel) CHANNEL="$2"; shift 2;; --debs) DEBS="$2"; shift 2;;
  --repo) REPO="$2"; shift 2;; --gpg-key) GPGKEY="$2"; shift 2;;
  --dry-run) DRYRUN=1; shift;; *) echo "unknown arg: $1" >&2; exit 2;; esac; done
case "$CHANNEL" in dev|prod) :;; *) echo "--channel must be dev|prod" >&2; exit 2;; esac
[ -d "$DEBS" ] && [ -n "$REPO" ] && [ -n "$GPGKEY" ] || { echo "usage: --channel dev|prod --debs DIR --repo O/R --gpg-key FP [--dry-run]" >&2; exit 2; }
ls "$DEBS"/*.deb >/dev/null 2>&1 || { echo "no .deb files found in $DEBS" >&2; exit 2; }
TAG="apt-${CHANNEL}"
here="$(cd "$(dirname "$0")" && pwd)"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
pool="$work/pool"; out="$work/out"; mkdir -p "$pool"
# 1. Fetch the channel's current pool (its .deb assets) so the re-publish is additive.
if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  mapfile -t assets < <(gh release view "$TAG" --repo "$REPO" --json assets --jq '.assets[].name | select(endswith(".deb"))')
  for a in "${assets[@]:-}"; do [ -n "$a" ] && gh release download "$TAG" --repo "$REPO" --pattern "$a" --dir "$pool" --clobber; done
fi
# 2. Merge the new debs (same filename replaces; unique versions add).
cp -f "$DEBS"/*.deb "$pool"/
# 3. Regenerate the signed flat index over the full pool.
bash "$here/build-flat-apt-repo.sh" --pool "$pool" --out "$out" --gpg-key "$GPGKEY" --suite ./
cp -f "$pool"/*.deb "$out"/    # assets: debs + metadata co-located in the release
if [ "$DRYRUN" = 1 ]; then echo "[dry-run] would upload $(ls "$out" | wc -l) assets to $REPO $TAG"; ls "$out"; exit 0; fi
# 4. Ensure the release exists, then clobber-upload every asset.
gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1 || \
  gh release create "$TAG" --repo "$REPO" --title "apt ($CHANNEL channel)" \
    --notes "Flat apt repository for the $CHANNEL channel. Consume with: deb [signed-by=...] https://github.com/$REPO/releases/download/$TAG/ ./" --prerelease
gh release upload "$TAG" --repo "$REPO" --clobber "$out"/*
echo "published $(ls "$out"/*.deb | wc -l) debs to $REPO $TAG"
