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
import re

from lib.cuckoo.common.abstracts import Report
from lib.cuckoo.common.central_mode import central_mode_config, upload_target_realpath
from lib.cuckoo.common.exceptions import CuckooReportError

log = logging.getLogger(__name__)

# job_id becomes an S3 key segment, so it must not contain path separators or
# traversal. The broker should stamp an authenticated job_id; this is the last
# line of defence against a tenant-supplied `custom` poisoning another job's
# prefix (audit CRITICAL-1). local-<int> fallback satisfies the allowlist.
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

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
        if not _JOB_ID_RE.match(job_id):
            # Refuse a job_id that could escape/poison another job's S3 prefix.
            raise CuckooReportError(
                "centralstore: refusing unsafe job_id %r (must match %s)" % (job_id, _JOB_ID_RE.pattern))
        info["job_id"] = job_id  # carried into the DocumentDB doc; read seam keys S3 by it

        try:
            import boto3
        except ImportError as e:
            raise CuckooReportError("centralstore: central mode requires boto3") from e

        s3 = boto3.client("s3", region_name=cfg.s3_region)
        uploaded, failed = self._upload_tree(s3, cfg.s3_bucket, cfg.s3_prefix, job_id)
        # Guac recordings live outside the analysis tree (top-level storage/
        # guacrecordings/), so they need their own pass. Fold their counts in so the
        # done marker — the cleanup purge gate — only fires once EVERYTHING this job
        # produced (tree + binaries + any recording) is confirmed in S3.
        rec_up, rec_failed = self._upload_guacrecordings(s3, cfg.s3_bucket, cfg.s3_prefix, job_id, analysis_id)
        uploaded += rec_up
        failed += rec_failed
        log.info("centralstore: uploaded %d artifacts (%d recordings) to s3://%s/%s/%s/ (%d failed)",
                 uploaded, rec_up, cfg.s3_bucket, cfg.s3_prefix, job_id, failed)

        # Stamp a local marker ONLY when the whole tree is confirmed in S3. The worker's
        # cape-nvme-cleanup gate keys off this file: present => artifacts are durable in
        # the central stores => the local (ephemeral-NVMe) copy is safe to purge. On any
        # upload failure we leave no marker, so the analysis is retained until it is
        # re-confirmed or the worker recycles (24h) — never purged unconfirmed.
        if failed == 0:
            self._write_done_marker(cfg, job_id, uploaded)
        else:
            log.warning("centralstore: %d upload(s) failed for job_id=%s; NOT marking done "
                        "(local copy retained for cleanup safety)", failed, job_id)

    def _trusted_roots(self, base_real):
        """Content roots a sample-influenced symlink may legitimately resolve into:
        storage/binaries (the content-addressed sample/dropped-file store the analysis
        dir's `binary` symlink targets) and storage/guacrecordings. base_real is
        .../storage/analyses/<id>, so the storage root is two levels up."""
        storage_root = os.path.dirname(os.path.dirname(base_real))
        return [
            os.path.realpath(os.path.join(storage_root, "binaries")),
            os.path.realpath(os.path.join(storage_root, "guacrecordings")),
        ]

    def _upload_tree(self, s3, bucket, prefix, job_id):
        base = self.analysis_path
        if not base or not os.path.isdir(base):
            log.warning("centralstore: analysis_path missing or not a dir: %s", base)
            return 0, 0
        base_real = os.path.realpath(base)
        trusted_roots = self._trusted_roots(base_real)
        count = 0
        failed = 0
        for root, dirs, files in os.walk(base):
            # don't descend symlinked dirs; drop excluded dirs
            dirs[:] = [d for d in dirs if d not in _EXCLUDE_DIRS and not os.path.islink(os.path.join(root, d))]
            for fn in files:
                full = os.path.join(root, fn)
                # A regular in-tree file uploads itself; a symlink into a trusted
                # content root (binary -> storage/binaries/<sha256>, a recording ->
                # storage/guacrecordings/) uploads its RESOLVED content under the
                # analysis-relative key. Anything resolving elsewhere (a planted
                # symlink to e.g. ~/.aws/credentials) returns None and is skipped —
                # the artifact-exfil guard stays intact (audit CRITICAL).
                src = upload_target_realpath(full, base_real, trusted_roots)
                if src is None:
                    log.warning("centralstore: skipping out-of-tree/untrusted artifact %s", full)
                    continue
                rel = os.path.relpath(full, base)
                key = f"{prefix}/{job_id}/{rel}"
                try:
                    s3.upload_file(src, bucket, key)
                    count += 1
                except Exception as e:
                    failed += 1
                    log.warning("centralstore: failed to upload %s -> %s: %s", rel, key, e)
        return count, failed

    def _upload_guacrecordings(self, s3, bucket, prefix, job_id, task_id):
        """Ship Guacamole session recordings for this task. Recordings live in the
        top-level storage/guacrecordings/ (NOT inside the analysis tree, so the walk
        above never sees them) and are named '<task_id>_<session_id>' by the guac
        consumer. They only exist when an analyst live-viewed the VM mid-detonation,
        so this is usually a no-op; when present they upload under <job_id>/
        guacrecordings/<name> so the central store has them before the worker purges.
        """
        base_real = os.path.realpath(self.analysis_path)
        rec_root = os.path.join(os.path.dirname(os.path.dirname(base_real)), "guacrecordings")
        if not task_id or not os.path.isdir(rec_root):
            return 0, 0
        count = 0
        failed = 0
        prefix_match = f"{task_id}_"
        for fn in os.listdir(rec_root):
            # exact '<task_id>' or '<task_id>_<session>' — never a different task that
            # merely shares a leading digit (e.g. task 1 vs 15): require '_' boundary.
            if fn != str(task_id) and not fn.startswith(prefix_match):
                continue
            full = os.path.join(rec_root, fn)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            key = f"{prefix}/{job_id}/guacrecordings/{fn}"
            try:
                s3.upload_file(full, bucket, key)
                count += 1
            except Exception as e:
                failed += 1
                log.warning("centralstore: failed to upload recording %s -> %s: %s", fn, key, e)
        return count, failed

    def _write_done_marker(self, cfg, job_id, uploaded):
        """Write storage/analyses/<id>/.centralstore.done once the artifact tree is
        fully in S3. cape-nvme-cleanup purges only analyses carrying this marker, so
        it can never delete something that didn't reach the central stores."""
        base = self.analysis_path
        if not base or not os.path.isdir(base):
            return
        marker = os.path.join(base, ".centralstore.done")
        try:
            import json
            import time
            with open(marker, "w") as f:
                json.dump({
                    "job_id": job_id,
                    "s3": "s3://%s/%s/%s/" % (cfg.s3_bucket, cfg.s3_prefix, job_id),
                    "artifacts": uploaded,
                    "ts": time.time(),
                }, f)
        except Exception as e:
            log.warning("centralstore: could not write done marker %s: %s", marker, e)
