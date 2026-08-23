# Portable sandbox-code snapshot

- Source branch: `feat/cape-mt-central-port`
- Source commit: `647d12f0eff40bb1abc0f6268ff6fe614eaae9c2`
- Export: complete tracked tree, without `.git` or repository history

## Purpose

This is the full reproducible build-and-bake workspace. It includes Debian
package sources, GitHub Actions build orchestration, Packer AMI definitions,
provisioners, Terraform smoke/E2E tests, and validation scripts.

## Sanitization

- No Git history, remotes, reflogs, or credentials are included.
- No ignored files, caches, bytecode, local build outputs, Terraform state,
  `.tfvars`, or local environment files are included.
- Workflow secret names remain as input interfaces; secret values are absent.
- The Layer 4 smoke test was adjusted to derive its expected package host from
  `apt_repo_url` instead of embedding the source environment's private domain.
- Package/CDN scripts now require `APT_REPO_URL`; checked-in host configuration
  uses `apt.example.invalid` and resolves it into the selected URL while building.
- The source environment's bundled FakeNet interception CA was removed. The
  FakeNet package build now creates a fresh CA key and certificate with OpenSSL.
- CAPE's placeholder Google service-account JSON contains explicit
  `REPLACE_ME` values and no private-key marker.
- Canonical's public Ubuntu AWS owner ID remains because it selects official
  Ubuntu images and is not an account credential.
- No private-key markers remain in the snapshot.

## Destination

Use this tree from `/home/cape/src/sandbox-code-portable`. Configure the
workflow/packer variables and secret interfaces for the destination environment
before publishing packages or baking an AMI.
