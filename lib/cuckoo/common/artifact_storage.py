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
    stamped into info.job_id at reporting; centralstore re-keys info.id to the unique
    central task id). Resolve task_id -> job_id via mongo.

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


def ensure_local_analysis(task_id, scope=None, exclude_prefixes=("memory/",)):
    """Central mode: lazily materialize the S3 results/<job_id>/ tree into the local
    storage/analyses/<task_id>/ dir so the MANY report features that read the local
    filesystem (json report, evtx, ETW aux/*.json, sysmon, process.log, behavior
    feeds, dropped files, …) work centrally without rewriting each reader. Cached via
    a .central_staged marker (subsequent calls are a cheap stat). Excludes large
    on-demand artifacts (memory dumps) which stream via materialize_artifact instead.
    No-op single-node. Best-effort: never raises into the view."""
    cfg = central_mode_config()
    if not cfg.enabled:
        return
    local = _local_analysis_path(task_id, "")
    marker = os.path.join(local, ".central_staged")
    if os.path.exists(marker):
        return
    try:
        job_id = _job_id_for_task(task_id, scope)
        if not job_id:
            return
        s3 = _s3_client(cfg.s3_region)
        prefix = f"{cfg.s3_prefix}/{job_id}/"
        os.makedirs(local, exist_ok=True)
        local_real = os.path.realpath(local)
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=cfg.s3_bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                rel = obj["Key"][len(prefix):]
                if not rel or rel.endswith("/") or any(rel.startswith(p) for p in exclude_prefixes):
                    continue
                # Defence-in-depth: this is the ONLY seam path that turns an S3 key
                # suffix into a LOCAL filesystem destination, so guard it exactly like
                # the per-file seam guards its relpaths (_safe_relpath). centralstore
                # only ever produces in-tree keys today, but a key with '..' / an
                # absolute segment must never write outside the analysis dir.
                try:
                    _safe_relpath(rel)
                except Exception:
                    continue
                dest = os.path.join(local, rel)
                dest_real = os.path.realpath(dest)
                if dest_real != local_real and not dest_real.startswith(local_real + os.sep):
                    continue
                if os.path.exists(dest):
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                s3.download_file(cfg.s3_bucket, obj["Key"], dest)
        with open(marker, "w") as f:
            f.write(job_id)
    except Exception:
        # leave whatever was staged; the per-file seam still covers downloads
        pass


def artifact_exists(task_id, relpath, scope=None):
    """True iff an analysis artifact exists — checked locally (single-node) or via an S3
    HEAD (central mode). Used to gate optional UI download links (decrypted/mixed pcap,
    tlskeys, mitmdump) that the worker may or may not have produced; a local-FS check
    returns False for S3-backed artifacts in central mode, hiding links for files that
    actually exist."""
    cfg = central_mode_config()
    if not cfg.enabled:
        return os.path.exists(_local_analysis_path(task_id, relpath))
    try:
        key = f"{cfg.s3_prefix}/{_job_id_for_task(task_id, scope)}/{_safe_relpath(relpath)}"
        _s3_client(cfg.s3_region).head_object(Bucket=cfg.s3_bucket, Key=key)
        return True
    except Exception:
        return False


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
