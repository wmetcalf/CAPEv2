"""FS->S3 artifact seam. central_mode OFF -> local storage/analyses/<task_id>/<relpath>;
ON -> S3 <prefix>/<job_id>/<relpath>. Single entry point the artifact-serving views
call so single-node vs central differ in exactly one place. Validated live on a CAPE
box (the web stack can't be imported in isolation); the local/S3 branch logic here is
the unit-testable part.
"""
import os

from lib.cuckoo.common.constants import CUCKOO_ROOT
from lib.cuckoo.common.central_mode import central_mode_config


def _local_analysis_path(task_id, relpath):
    return os.path.join(CUCKOO_ROOT, "storage", "analyses", str(task_id), relpath)


def _safe_relpath(relpath):
    """Reject traversal/absolute/backslash before a relpath becomes an S3 key
    segment. Callers today pass regex-constrained (\\w+) or fixed relpaths, but this
    keeps the seam safe independent of caller discipline (audit MEDIUM-1)."""
    from django.http import Http404

    if not relpath or relpath.startswith("/") or "\\" in relpath or ".." in relpath.split("/"):
        raise Http404(f"invalid artifact path: {relpath!r}")
    return relpath


def _job_id_for_task(task_id, scope=None):
    """Central mode keys S3 by the global job_id (the broker passes it in custom,
    stamped into info.job_id at reporting). Resolve task_id -> job_id via mongo.

    `scope` is the requesting viewer's tenant filter (e.g. entitled_scope_filter):
    info.id is a per-worker sequence and collides across workers in a central
    deployment, so the lookup is ANDed with the viewer's scope to guarantee the
    resolved doc is one the viewer may actually see — not another tenant's analysis
    that happens to share the numeric id (audit HIGH: cross-store id collision)."""
    from dev_utils.mongodb import mongo_find_one
    from django.http import Http404

    query = {"info.id": int(task_id)}
    if scope:
        query = {"$and": [query, scope]}
    doc = mongo_find_one("analysis", query, {"info.job_id": 1})
    job_id = (doc or {}).get("info", {}).get("job_id")
    if not job_id:
        raise Http404("no job_id mapping for task")
    return job_id


def _s3_client(region):
    import boto3

    return boto3.client("s3", region_name=region)


def _file_iter(path, chunk):
    with open(path, "rb") as f:
        while True:
            data = f.read(chunk)
            if not data:
                break
            yield data


def artifact_response(task_id, relpath, content_type, filename, chunk=8192, scope=None):
    """Return a Django streaming response for an analysis artifact, local or S3.

    `scope`: the requesting viewer's tenant filter, threaded into the central
    task_id->job_id lookup so a viewer can't pull another tenant's artifact via an
    id collision (see _job_id_for_task)."""
    from django.http import StreamingHttpResponse, Http404

    cfg = central_mode_config()
    if not cfg.enabled:
        path = _local_analysis_path(task_id, relpath)
        if not os.path.exists(path):
            raise Http404(f"artifact not found: {relpath}")
        resp = StreamingHttpResponse(_file_iter(path, chunk), content_type=content_type)
        resp["Content-Length"] = os.path.getsize(path)
        resp["Content-Disposition"] = f"attachment; filename={filename}"
        return resp

    key = f"{cfg.s3_prefix}/{_job_id_for_task(task_id, scope)}/{_safe_relpath(relpath)}"
    try:
        obj = _s3_client(cfg.s3_region).get_object(Bucket=cfg.s3_bucket, Key=key)
    except Exception:
        raise Http404(f"artifact not found in S3: {key}")
    resp = StreamingHttpResponse(obj["Body"].iter_chunks(chunk), content_type=content_type)
    if "ContentLength" in obj:
        resp["Content-Length"] = obj["ContentLength"]
    resp["Content-Disposition"] = f"attachment; filename={filename}"
    return resp


def materialize_artifact(task_id, relpath, scope=None):
    """Return (local_path, is_temp) for an artifact that a caller must open as a real
    file — random-access slicing (procdump), filtering/regeneration (pcap), or handing
    to an external tool (VT upload). Single-node: the real local path, is_temp=False
    (DO NOT delete it). Central: the S3 object streamed to a temp file, is_temp=True
    (caller deletes it in a finally). Returns (None, False) if the artifact is absent."""
    cfg = central_mode_config()
    if not cfg.enabled:
        path = _local_analysis_path(task_id, relpath)
        return (path, False) if os.path.exists(path) else (None, False)

    import tempfile

    key = f"{cfg.s3_prefix}/{_job_id_for_task(task_id, scope)}/{_safe_relpath(relpath)}"
    try:
        obj = _s3_client(cfg.s3_region).get_object(Bucket=cfg.s3_bucket, Key=key)
    except Exception:
        return (None, False)
    fd, tmp = tempfile.mkstemp(prefix="cape_central_")
    try:
        with os.fdopen(fd, "wb") as f:  # closes fd even on exception
            for chunk in obj["Body"].iter_chunks(65536):
                f.write(chunk)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return (None, False)
    return (tmp, True)


def read_artifact_text(task_id, relpath, max_bytes=100000, scope=None):
    """Read a text artifact (e.g. process.log), local or S3, truncated to max_bytes."""
    cfg = central_mode_config()
    if not cfg.enabled:
        path = _local_analysis_path(task_id, relpath)
        if not os.path.exists(path):
            return ""
        with open(path, "r", errors="replace") as f:
            data = f.read(max_bytes + 1)
    else:
        key = f"{cfg.s3_prefix}/{_job_id_for_task(task_id, scope)}/{_safe_relpath(relpath)}"
        try:
            obj = _s3_client(cfg.s3_region).get_object(Bucket=cfg.s3_bucket, Key=key, Range=f"bytes=0-{max_bytes}")
            data = obj["Body"].read().decode("utf-8", errors="replace")
        except Exception:
            return ""
    if len(data) > max_bytes:
        return data[:max_bytes] + "\n... [TRUNCATED] ..."
    return data
