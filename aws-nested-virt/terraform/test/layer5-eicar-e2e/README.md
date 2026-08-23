# Layer 5 — EICAR E2E on nestedvirt-ami

**Stub. Not yet implemented.** This stack will spin up a full
`terraform/nestedvirt-ami` stack pointing at the dev apt channel,
submit EICAR via the CAPE API, poll `/api/tasks/view/N` until complete,
pull `report.json`, run it through the existing `broker/sandbox_forensics`
adapter, and assert against the same fixtures `test_cape_adapter.py`
uses (≥13 sigs, score ≥ 5, disposition includes `malicious`).

## Day 1 scope

EICAR only. The shared driver `scripts/run-layer-test.sh --layer 5`
will dispatch this stack with the same apt-channel/version pinning
the Layer 4 driver uses. ~30 min runtime, ~$1.70 per release on
m8i.8xlarge on-demand.

## Future expansion (separate work item)

Real-malware corpus in S3 with restricted IAM (CI-only access). Each
fixture declares expected sigs / score range / disposition with hard-
fail vs soft-warn tiers so signature-set drift is visible without
false negatives. The Layer 5 driver loops the corpus instead of
hitting EICAR alone. See the Phase 2 spec
(`docs/superpowers/specs/2026-05-06-cape-core-deb-design.md`)
"Test Harness — Layer 5" section.

## Why this stack lives separately from layer4-host-smoke

Layer 5 needs nested-virt + a much bigger instance (m8i.8xlarge vs
t3.medium), uses a different source AMI (the baked nestedvirt-ami AMI
that includes pre-snapshotted VM clones), and runs ~30 min vs ~5 min.
Splitting the stacks lets the cheap fast Layer 4 run on every dev
publish and the expensive slow Layer 5 only run when Layer 4 passes.
