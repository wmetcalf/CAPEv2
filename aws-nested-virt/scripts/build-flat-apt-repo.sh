#!/usr/bin/env bash
# Generate a signed FLAT apt repository (deb <base>/ ./) from a pool of .debs.
set -euo pipefail
POOL="" OUT="" GPGKEY="" SUITE="./" ORIGIN="CAPEv2-AWS-NestedVirt" LABEL="CAPEv2-AWS-NestedVirt"
while [ $# -gt 0 ]; do case "$1" in
  --pool) POOL="$2"; shift 2;; --out) OUT="$2"; shift 2;;
  --gpg-key) GPGKEY="$2"; shift 2;; --suite) SUITE="$2"; shift 2;;
  --origin) ORIGIN="$2"; shift 2;; --label) LABEL="$2"; shift 2;;
  *) echo "unknown arg: $1" >&2; exit 2;; esac; done
[ -d "$POOL" ] && [ -n "$OUT" ] && [ -n "$GPGKEY" ] || { echo "usage: --pool DIR --out DIR --gpg-key FP [--suite ./]" >&2; exit 2; }
mkdir -p "$OUT"
# Packages index with BARE filenames (flat repo: assets share one namespace).
( cd "$POOL" && apt-ftparchive packages . ) > "$OUT/Packages"
# apt-ftparchive prints "Filename: ./foo.deb"; strip the leading "./" to a bare name.
sed -i 's#^Filename: \./#Filename: #' "$OUT/Packages"
gzip -9c "$OUT/Packages" > "$OUT/Packages.gz"
# Release over the flat index (Suite/Codename = ./ for a flat repo).
apt-ftparchive \
  -o "APT::FTPArchive::Release::Origin=$ORIGIN" \
  -o "APT::FTPArchive::Release::Label=$LABEL" \
  -o "APT::FTPArchive::Release::Suite=$SUITE" \
  -o "APT::FTPArchive::Release::Codename=$SUITE" \
  -o "APT::FTPArchive::Release::Architectures=amd64" \
  -o "APT::FTPArchive::Release::Components=main" \
  release "$OUT" > "$OUT/Release"
# Sign: detached (Release.gpg) + inline (InRelease).
gpg --batch --yes --default-key "$GPGKEY" -abs -o "$OUT/Release.gpg" "$OUT/Release"
gpg --batch --yes --default-key "$GPGKEY" --clearsign -o "$OUT/InRelease" "$OUT/Release"
gpg --batch --yes --armor --export "$GPGKEY" > "$OUT/keyring.asc"
echo "flat apt repo written to $OUT"
