"""Central-mode toggle. OFF (default) = single-node behavior (local pg/mongo/FS).
ON = the web/api app reads the central data plane (RDS via [database], DocumentDB
via [mongodb]) and serves artifacts from S3.

The relational + mongo CONNECTIONS are pointed by their existing conf; this flag
gates the code-level behavior that has no conf today — the FS->S3 artifact seam —
and carries the S3 location. Parsing logic is split into the pure `_parse()` helper
so it's unit-testable without importing the CAPE config machinery.
"""
from dataclasses import dataclass


def _as_bool(v, default=False):
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class CentralModeConfig:
    enabled: bool = False
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_prefix: str = "results"  # results/<job_id>/...
    # The report doc -> central DocumentDB write is the NATIVE mongodb.py reporting
    # module pointed at DocumentDB via [mongodb] (tls=yes, retrywrites=no) — the
    # write path compat/docdb_compat.py already validated against live DocumentDB.
    # central_mode therefore only carries the FS->S3 artifact location.


def _parse(sec) -> "CentralModeConfig":
    """Pure: turn a [central_mode] config section (dict-like) into CentralModeConfig."""
    get = sec.get if hasattr(sec, "get") else (lambda k, d=None: d)
    return CentralModeConfig(
        enabled=_as_bool(get("enabled", False), False),
        s3_bucket=str(get("s3_bucket", "") or ""),
        s3_region=str(get("s3_region", "us-east-1") or "us-east-1"),
        s3_prefix=str(get("s3_prefix", "results") or "results"),
    )


def central_mode_config() -> "CentralModeConfig":
    # Lazy import so module import stays dependency-free (the CAPE config machinery
    # is only touched when this is actually called at runtime).
    from lib.cuckoo.common.config import Config

    try:
        sec = Config("cuckoo").get("central_mode")
    except Exception:
        sec = {}
    return _parse(sec)
