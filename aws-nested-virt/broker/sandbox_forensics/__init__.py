"""sandbox_forensics — vendor-neutral sandbox forensics normalization.

Adapts native sandbox reports (CAPE, CrowdStrike Falcon Sandbox, VMRay,
Joe Sandbox, Triage, Any.run, ...) into the canonical sandbox-forensics-v1
schema documented at ``docs/schemas/sandbox-forensics-v1.json``.

Used by:
  * the broker's normalizer Lambda (S3 PutObject → adapter → normalized.json)
  * the broker app for ad-hoc re-normalization on demand

Usage::

    from sandbox_forensics.adapters import normalize

    with open("report.json") as f:
        native = json.load(f)

    normalized = normalize(native, source="cape", job_id="...")

The dispatcher selects the right adapter from the ``source`` argument; each
adapter is a single ``adapt(native: dict, job_id: str) -> dict`` function.
"""

__version__ = "0.1.0"
SCHEMA_VERSION = "1.0"
