# APT signing keys

This directory intentionally ships **no key material**.

The APT repository is signed at publish time with a keypair **you** own. Nothing
in this repo trusts a pre-baked key, and no adopter should trust one shipped by
someone else — the public half is the trust anchor for every deb your hosts
install, so generate your own:

```bash
# Generate a dedicated, non-expiring-ish signing keypair (adjust as you like):
gpg --batch --gen-key <<'EOF'
%no-protection
Key-Type: RSA
Key-Length: 4096
Subkey-Type: RSA
Subkey-Length: 4096
Name-Real: <Your Org> APT Repo
Name-Email: apt@example.invalid
Expire-Date: 20y
%commit
EOF

# Export the PRIVATE key and store it as the CI secret `APT_SIGNING_GPG_KEY`
# (used by .github/workflows/apt-publish.yml to sign Release/InRelease):
gpg --armor --export-secret-keys <FINGERPRINT>
```

The **public** key is published automatically by the pipeline as a release
asset (`APT_KEYRING_URL`), and the host bootstrap fetches it into
`/usr/share/keyrings/` via `signed-by=`. Do not commit the private key.
