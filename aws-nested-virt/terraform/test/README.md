# Phase 2 e2e test harness — Layers 4 and 5

Two ephemeral terraform stacks the cape-build CI promotes through after
publishing to the dev apt channel. Each stack creates an EC2 host,
installs the freshly-published cape-* debs from the dev channel, runs
its layer-specific assertions, and tears down.

| Layer | Instance | Duration | What it validates |
|---|---|---|---|
| 4 — host-smoke | t3.medium (no nested-virt) | ~5 min | Packages install cleanly, services come up, listening ports open. Cheap signal that the deb is well-formed. |
| 5 — eicar-e2e  | m8i.8xlarge (nested-virt) | ~30 min | Full nestedvirt-ami stack with VM clones; submit EICAR via API; assert `report.json` parses through `broker/sandbox_forensics` adapter (≥13 sigs, score ≥ 5, disposition includes `malicious`). |

Both stacks are designed for **fresh-per-release** lifecycle (per the
Phase 2 spec): `terraform apply` → run battery → `terraform destroy`,
under a `trap EXIT` so a crashed driver doesn't orphan instances.

## Trigger flow

```
sandbox-code/cape-build  ──► dev apt channel
                              │
                              ▼
                          ┌────────────┐
                          │  Layer 4   │ t3.medium
                          │  ~$0.04    │
                          └─────┬──────┘
                                │ pass
                                ▼
                          ┌────────────┐
                          │  Layer 5   │ m8i.8xlarge
                          │  ~$1.70    │
                          └─────┬──────┘
                                │ pass
                                ▼
                          promote to prod apt channel
```

Triggered via `repository_dispatch` from `sandbox-code`'s apt-publish
workflow once a new dev-channel publish lands. The dispatch payload
contains the new package versions; the test stacks pin to those
versions on install (so a passing Layer 5 means *those exact packages*
are blessed for promotion).

## Costs (cost-over-speed principle, per spec)

- Layer 4: **t3.medium** on-demand, ~5 min. Per release: ~$0.005.
  Cheap enough to run on every dev-channel publish without thinking.
- Layer 5: **m8i.8xlarge** on-demand, ~40 min total (incl. Packer-
  baked AMI launch + EICAR + teardown). Per release: ~$1.70.
  On-demand (not spot) — interruption mid-test loses the whole run
  and costs more than the spot savings on a 1-hour window.

Estimated annual: ~$15 if cape-signatures publishes weekly.

## Sample corpus (Layer 5)

Day 1: **EICAR only**. Validates the full pipeline and asserts against
the same fixtures `broker/sandbox_forensics/tests/test_cape_adapter.py`
uses (13+ behavioral sigs, ClamAV+YARA hits, score 10.0, disposition
malicious).

Future expansion (separate work item): a real-malware corpus in S3 with
restricted IAM (CI-only access). Each sample's fixture declares
expected sigs / score range / disposition with hard-fail vs soft-warn
tiers so signature-set drift is visible without false negatives. Layer
5 driver loops the corpus instead of hitting EICAR alone.

## Driver scripts

`scripts/run-layer-test.sh` is the shared driver:

```sh
./scripts/run-layer-test.sh --layer 4 \
                            --cape-core-version "2.5.1+cape.3" \
                            --cape-signatures-version "2026.05.06+rev1"
```

It owns:
- `terraform init && terraform apply` against the layer's stack
- Polling for ready state
- Running the layer-specific assertions
- `terraform destroy` in a `trap EXIT` (cleanup-on-crash)
- Aggregating exit code

Wired into a GHA workflow via `repository_dispatch` from
`sandbox-code/apt-publish.yml`.
