#!/usr/bin/env bash
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"     # scripts/
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
# 1. build a trivial .deb
mkdir -p "$work/pkg/DEBIAN"
cat > "$work/pkg/DEBIAN/control" <<CTL
Package: flatrepo-selftest
Version: 1.0.0
Architecture: amd64
Maintainer: CAPEv2 AWS Nested-Virt <noreply@example.invalid>
Description: flat-repo self-test package
CTL
mkdir -p "$work/pool"
dpkg-deb --build "$work/pkg" "$work/pool/flatrepo-selftest_1.0.0_amd64.deb" >/dev/null
# 2. an ephemeral signing key
export GNUPGHOME="$work/gnupg"; mkdir -p "$GNUPGHOME"; chmod 700 "$GNUPGHOME"
gpg --batch --gen-key <<KEY
%no-protection
Key-Type: RSA
Key-Length: 2048
Name-Real: FlatRepo Test
Name-Email: test@example.invalid
Expire-Date: 0
%commit
KEY
fp="$(gpg --list-secret-keys --with-colons | awk -F: '/^fpr:/{print $10; exit}')"
# 3. run the generator (does not exist yet -> FAIL)
bash "$here/build-flat-apt-repo.sh" --pool "$work/pool" --out "$work/out" --gpg-key "$fp" --suite ./
# 4. assertions
test -s "$work/out/Packages"
test -s "$work/out/Packages.gz"   # apt prefers the gzipped index
grep -q '^Filename: flatrepo-selftest_1.0.0_amd64.deb$' "$work/out/Packages"   # BARE filename
test -s "$work/out/InRelease" && test -s "$work/out/Release.gpg" && test -s "$work/out/keyring.asc"
gpg --verify "$work/out/Release.gpg" "$work/out/Release"
echo "PASS: flat-repo generator"

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  ( cd "$work/out" && cp "$work/pool"/*.deb . )   # co-locate debs with metadata (flat)
  python3 -m http.server 8099 --directory "$work/out" &>/dev/null & srv=$!; sleep 1
  docker run --rm --network host ubuntu:24.04 bash -c '
    set -e
    apt-get update -qq && apt-get install -y -qq gpg curl >/dev/null
    curl -fsSL http://127.0.0.1:8099/keyring.asc | gpg --dearmor > /usr/share/keyrings/flatrepo.gpg
    echo "deb [signed-by=/usr/share/keyrings/flatrepo.gpg] http://127.0.0.1:8099/ ./" > /etc/apt/sources.list.d/flatrepo.list
    apt-get update -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/flatrepo.list -o Dir::Etc::sourceparts=- -qq
    apt-get install -y flatrepo-selftest
    dpkg -s flatrepo-selftest | grep -q "Status: install ok installed"
  '
  kill $srv 2>/dev/null || true
  echo "PASS: apt-get consumed the flat repo"
else
  echo "SKIP: docker absent — flat-repo format verified, apt-get consumer check skipped"
fi
