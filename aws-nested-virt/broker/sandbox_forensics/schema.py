"""sandbox-forensics-v1 schema constants and helpers.

The canonical schema lives in
``docs/schemas/sandbox-forensics-v1.json`` (JSON Schema 2020-12).
This module provides Python-side constants and lightweight helpers
so adapters and downstream consumers don't have to hard-code field
names.
"""

from __future__ import annotations

SCHEMA_VERSION = "1.0"

# Disposition vocabulary (open set; downstream consumers may extend)
DISPOSITION_MALICIOUS = "malicious"
DISPOSITION_SUSPICIOUS = "suspicious"
DISPOSITION_BENIGN = "benign"
DISPOSITION_UNKNOWN = "unknown"
DISPOSITION_PHISH = "phish"
DISPOSITION_SPAM = "spam"

# Target.type vocabulary
TARGET_TYPE_FILE = "file"
TARGET_TYPE_URL = "url"


def empty_envelope(job_id: str, source: str) -> dict:
    """Return a minimal valid sandbox-forensics-v1 envelope.

    Adapters fill in fields as they translate native data; anything left
    as defaults still validates against the v1 schema.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "analysis": {
            "id": job_id,
            "source": source,
        },
        "target": {
            "type": TARGET_TYPE_FILE,
        },
        "score": 0.0,
        "disposition": [DISPOSITION_UNKNOWN],
        "families": [],
        "detections": [],
    }
