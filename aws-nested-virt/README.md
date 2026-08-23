# sandbox-code

Source repo for the `cape-core`, `cape-signatures`, `cape-qemu`, and
`cape-suricata` Debian packages — Phase 2 of the sandbox content
distribution service.

## Status

**Scaffolding in place; not yet buildable.** The `debian/` packaging,
`qemu-build/` and `suricata-build/` source-build inputs, and
`tests/` harness skeleton are committed. The `cape/` and `community/`
subtrees are still empty placeholders — they need the CAPEv2 fork
(fork branch) and the `kevoreilly/community` git subtree pulled
in before `dpkg-buildpackage` can produce real binaries.

## Layout

```
sandbox-code/
├── cape/                  # CAPEv2 fork (fork branch). EMPTY placeholder.
├── community/             # kevoreilly/community subtree. EMPTY placeholder.
├── qemu-build/            # kvm-qemu.sh — patched QEMU + SeaBIOS build inputs
├── seabios-build/         # SeaBIOS share dir (kvm-qemu.sh seabios mode)
├── suricata-build/        # suricata_from_source.sh — patched Suricata build
├── packer/                # AMI bake automation. EMPTY placeholder.
├── debian/                # Single source package, four binaries
├── tests/                 # Layers 1–3 test harness
│   ├── layer1-build-sanity/
│   ├── layer2-container-smoke/
│   └── layer3-upgrade/
└── .github/workflows/
    └── cape-build.yml     # dpkg-buildpackage in CI, uploads .debs as artifact
```

## Packages produced

| Package | Architecture | Triggers restart of |
|---|---|---|
| `cape-core` | amd64 | `cape`, `cape-web`, `cape-rooter`, `cape-processor`, `guac-web` |
| `cape-signatures` | all | `cape-processor` only |
| `cape-qemu` | amd64 | none (libvirt picks up next domain cold start) |
| `cape-suricata` | amd64 | `cape-processor` (uses suricata in unix-socket mode per task) |

## Design

Full design spec lives in the aws-nested-virt IaC repo's design spec.
Key decisions captured there:

- Vendored Python venv via `dh-virtualenv`
- Patches as commits in the fork (no `*.patch` files at runtime)
- Apt repo on S3, signed with a single GHA-stored GPG key shared with Phase 1
- 5-layer test harness with promotion gate (dev → prod apt channel)
- Packer-driven AMI bake with mandatory tagging policy

## Phase 1 dependency

This work depends on the Phase 1 detection content distribution service
(per its design spec) standing up the
private apt repo on S3 first. Until then, `cape-build.yml` produces
`.deb` artifacts as GHA workflow artifacts only — there's no `apt-publish`
step yet.
