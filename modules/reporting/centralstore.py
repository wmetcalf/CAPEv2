"""centralstore — worker-side ingest seam for central mode.

central_mode OFF (default): no-op. The worker behaves byte-for-byte single-node.

central_mode ON: stamp the broker-supplied global job_id into results.info.job_id
and push the analysis artifact tree to S3 at <prefix>/<job_id>/<rel>. Runs at
order 9998 — BEFORE the native mongodb reporting module (order 9999) — so the
report doc that mongodb.py writes to the central DocumentDB already carries
info.job_id. The DocumentDB write itself is the NATIVE mongodb.py path pointed at
DocumentDB via [mongodb] (tls=yes, retrywrites=no); compat/docdb_compat.py already
validated that write path (loop_saver/$set, calls chunking, files $addToSet,
tenant_scope_idx) against live DocumentDB. This module only adds the FS->S3 half
plus the job_id keying the read seam (artifact_storage.artifact_response) resolves.
"""
import logging
import os

from lib.cuckoo.common.abstracts import Report
from lib.cuckoo.common.central_mode import central_mode_config
from lib.cuckoo.common.exceptions import CuckooReportError

log = logging.getLogger(__name__)

# Upload the whole analysis tree to S3 (the "heavy detail" tier): shots, dropped
# files, pcap, procdump, AND reports/ (report.json/html/pdf are downloadable
# artifacts the filereport view serves). The DocumentDB report doc written by the
# native mongodb.py module is ADDITIVE (it powers the queryable UI tabs) — it does
# not replace the report files, so nothing is excluded here.
_EXCLUDE_DIRS = set()


def resolve_job_id(custom, analysis_id):
    """The broker passes the global job_id through the task `custom` field, carried
    all the way to reporting. Accept 'job_id=<v>' (optionally among other
    comma-separated k=v pairs) or a bare token. Fall back to 'local-<id>' so central
    mode also works when an analysis was submitted directly (no broker)."""
    if custom:
        text = str(custom)
        for part in text.split(","):
            part = part.strip()
            if part.startswith("job_id="):
                v = part.split("=", 1)[1].strip()
                if v:
                    return v
        token = text.strip()
        if token and "=" not in token and "," not in token:
            return token
    return f"local-{analysis_id}"


class CentralStore(Report):
    """Ship analysis artifacts to S3 + stamp the central job_id (central mode only)."""

    order = 9998  # before mongodb (9999): stamp info.job_id before the central DocumentDB write

    def run(self, results):
        cfg = central_mode_config()
        if not cfg.enabled:
            return  # single-node: no-op, behavior byte-for-byte unchanged

        if not cfg.s3_bucket:
            raise CuckooReportError("centralstore: central_mode enabled but [central_mode] s3_bucket unset")

        info = results.setdefault("info", {})
        analysis_id = info.get("id")
        job_id = info.get("job_id") or resolve_job_id(info.get("custom"), analysis_id)
        info["job_id"] = job_id  # carried into the DocumentDB doc; read seam keys S3 by it

        try:
            import boto3
        except ImportError as e:
            raise CuckooReportError("centralstore: central mode requires boto3") from e

        s3 = boto3.client("s3", region_name=cfg.s3_region)
        uploaded = self._upload_tree(s3, cfg.s3_bucket, cfg.s3_prefix, job_id)
        log.info("centralstore: uploaded %d artifacts to s3://%s/%s/%s/",
                 uploaded, cfg.s3_bucket, cfg.s3_prefix, job_id)

    def _upload_tree(self, s3, bucket, prefix, job_id):
        base = self.analysis_path
        if not base or not os.path.isdir(base):
            log.warning("centralstore: analysis_path missing or not a dir: %s", base)
            return 0
        count = 0
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in _EXCLUDE_DIRS]
            for fn in files:
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, base)
                key = f"{prefix}/{job_id}/{rel}"
                try:
                    s3.upload_file(full, bucket, key)
                    count += 1
                except Exception as e:
                    log.warning("centralstore: failed to upload %s -> %s: %s", rel, key, e)
        return count
