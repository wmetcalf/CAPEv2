"""Central-mode artifact serving — the S3-backed counterparts of the analysis
download/serve views in web/analysis/views.py.

Kept in a SEPARATE module on purpose: views.py is heavily synced from upstream
CAPEv2 (cape/ subtree merges), so it carries only a thin
``if central_mode_config().enabled: return central_<view>(...)`` dispatch at the top
of each affected view. All the central-mode logic lives here, in a file upstream does
not have — so it never participates in a merge conflict, and we can take upstream
advancements with minimal friction.

These functions only run when central mode is ON, and only AFTER the upstream view's
decorator stack (require_task_visibility, ratelimit, auth, …) has already run — so the
relational task is authorized before we touch the central data plane. Single-node
behavior is entirely in views.py and is never reached through this module.
"""
import os

from django.shortcuts import render


def central_file_nl(request, category, task_id, dlfile):
    """Inline report assets: screenshots, bingraph, vba2graph (file_nl)."""
    from django.http import Http404

    from dashboard.views import entitled_scope_filter
    from lib.cuckoo.common.artifact_storage import artifact_response

    # tenant-scope the S3/DocumentDB lookup as defence-in-depth against task_id
    # collisions across workers (audit HIGH).
    scope = entitled_scope_filter(request.user)
    if category == "screenshot":
        cands = [(os.path.join("shots", dlfile + ext), dlfile + ext, cd) for ext, cd in ((".jpg", "image/jpeg"), (".png", "image/png"))]
    elif category == "bingraph":
        cands = [(os.path.join("bingraph", dlfile + "-ent.svg"), dlfile + "-ent.svg", "image/svg+xml")]
    elif category == "vba2graph":
        cands = [(os.path.join("vba2graph", "svg", f"{dlfile}.svg"), f"{dlfile}.svg", "image/svg+xml")]
    else:
        return render(request, "error.html", {"error": "Category not defined"})
    for relpath, fn, cd in cands:
        try:
            return artifact_response(task_id, relpath, cd, fn, scope=scope)
        except Http404:
            continue
    return render(request, "error.html", {"error": f"Could not find {category} {dlfile}"})


def central_filereport(request, task_id, fname):
    """Full analysis report download (filereport); fname is the resolved report file."""
    from django.http import Http404

    from dashboard.views import entitled_scope_filter
    from lib.cuckoo.common.artifact_storage import artifact_response

    scope = entitled_scope_filter(request.user)
    try:
        return artifact_response(task_id, f"reports/{fname}", "application/octet-stream", f"{task_id}_{fname}", scope=scope)
    except Http404:
        return render(request, "error.html", {"error": f"File not found: {fname}"})


def central_full_memory_dump(request, analysis_number, names):
    """Full memory dump / its strings (whole-file); names = candidate relpaths."""
    from django.http import Http404

    from dashboard.views import entitled_scope_filter
    from lib.cuckoo.common.artifact_storage import artifact_response

    scope = entitled_scope_filter(request.user)
    for name in names:
        try:
            return artifact_response(analysis_number, name, "application/octet-stream", name, scope=scope)
        except Http404:
            continue
    return render(request, "error.html", {"error": "File not found"})


def central_file(request, category, task_id, dlfile):
    """Main multi-category artifact download (file). Maps each category to its
    analysis-relative S3 relpath — the layout centralstore uploads to
    s3://<bucket>/results/<job_id>/<relpath>. On-the-fly bundles (zip_categories,
    pcapng) and search-driven *zipall sets aren't materialized in S3, so they return a
    clear central-mode error rather than a silent 404."""
    from django.http import Http404

    from dashboard.views import entitled_scope_filter
    from lib.cuckoo.common.artifact_storage import artifact_response
    from analysis.views import can_view_sample, zip_categories

    OCTET = "application/octet-stream"
    PCAP = "application/vnd.tcpdump.pcap"

    if category in ("sample", "static"):
        # by-hash sample download: enforce the SAME visible-task-referencing-the-sample
        # boundary as single-node (audit CRITICAL). The analysis sample is uploaded as
        # <job_id>/binary; dropped-by-hash is a documented follow-on (not served here).
        if not can_view_sample(request.user, sha256=dlfile):
            return render(request, "error.html", {"error": "File not found"})
        spec = ("binary", dlfile, OCTET)
    elif category == "dropped":
        spec = (f"files/{dlfile}", dlfile, OCTET)
    elif category.startswith("CAPE") and category not in zip_categories:
        spec = (f"CAPE/{dlfile}", dlfile, OCTET)
    elif category == "pcap":
        spec = ("dump.pcap", f"{dlfile}.pcap", PCAP)
    elif category == "decrypted_pcap":
        spec = ("dump_decrypted.pcap", f"{dlfile}.pcap", PCAP)
    elif category == "mixed_pcap":
        spec = ("dump_mixed.pcap", f"{dlfile}.pcap", PCAP)
    elif category == "debugger_log":
        spec = (f"debugger/{dlfile}.log", f"{dlfile}.log", "text/plain")
    elif category.startswith("procdump") and category not in zip_categories:
        spec = (f"procdump/{dlfile}", dlfile, OCTET)
    elif category in ("memdump", "memdumpstrings"):
        ext = ".dmp" if category == "memdump" else ".dmp.strings"
        spec = (f"memory/{dlfile}{ext}", f"{dlfile}{ext}", OCTET)
    elif category == "rtf":
        spec = (f"rtf_objects/{dlfile}", dlfile, OCTET)
    elif category == "usage":
        spec = ("aux/usage.svg", "usage.svg", "image/svg+xml")
    elif category == "suricata":
        spec = (f"logs/files/{dlfile}", dlfile, OCTET)
    elif category == "zip":  # suricata dropped files bundle (pre-existing in tree)
        spec = ("logs/files.zip", "files.zip", "application/zip")
    elif category == "tlskeys":
        spec = ("tlsdump/tlsdump.log", "tlsdump.log", "text/plain")
    elif category == "sysmon":
        spec = ("sysmon/sysmon.data", "sysmon.data", OCTET)
    elif category == "evtx":
        spec = ("evtx/evtx.zip", f"{task_id}_evtx.zip", "application/zip")
    elif category == "mitmdump":
        spec = ("mitmdump/dump.har", "dump.har", "text/plain")
    else:
        return render(request, "error.html", {
            "error": f"'{category}' is not yet available in central mode (server-side bundle/generated artifact)"})

    relpath, fn, cd = spec
    scope = entitled_scope_filter(request.user)
    try:
        return artifact_response(task_id, relpath, cd, f"{task_id}_{fn}", scope=scope)
    except Http404:
        return render(request, "error.html", {"error": f"Could not find {category} {dlfile}"})


def central_open_procdump(request, task_id, origname):
    """Acquire the proc memory dump as a LOCAL file for the slicing logic in
    procdump(): stream memory/<origname> (or its .zip) from S3 to a temp. Returns
    (dumpfile_path, tmp_file_path, tmpdir) matching procdump's existing cleanup
    variables (it unlinks tmp_file_path + delete_folder(tmpdir)). (None, None, None)
    if the dump is absent."""
    import tempfile
    import zipfile

    from django.conf import settings

    from dashboard.views import entitled_scope_filter
    from lib.cuckoo.common.artifact_storage import materialize_artifact

    scope = entitled_scope_filter(request.user)
    dumpfile, is_temp = materialize_artifact(task_id, f"memory/{origname}", scope=scope)
    if dumpfile:
        return dumpfile, (dumpfile if is_temp else None), None

    zpath, zt = materialize_artifact(task_id, f"memory/{origname}.zip", scope=scope)
    if not zpath:
        return None, None, None
    tmpdir = tempfile.mkdtemp(prefix="capeprocdump_", dir=settings.TEMP_PATH)
    with zipfile.ZipFile(zpath, "r") as f:
        extracted = f.extract(origname, path=tmpdir)
    if zt:
        try:
            os.unlink(zpath)
        except OSError:
            pass
    return extracted, extracted, tmpdir


def central_vtupload(request, category, task_id, filename, dlfile):
    """Upload a stored artifact to VirusTotal (vtupload): stream it from S3 to a temp,
    POST, clean up."""
    import base64

    import requests

    from dashboard.views import entitled_scope_filter
    from lib.cuckoo.common.artifact_storage import materialize_artifact
    from analysis.views import can_view_sample, enabledconf, integrations_cfg

    if not (enabledconf["vtupload"] and integrations_cfg.virustotal.apikey):
        return render(request, "error.html", {"error": "VirusTotal upload is not enabled"})

    if category in ("sample", "static"):
        if not can_view_sample(request.user, sha256=dlfile):
            return render(request, "error.html", {"error": "File not found"})
        relpath = "binary"
    elif category == "dropped":
        relpath = f"files/{filename}"
    elif category in ("CAPE", "procdump"):
        relpath = f"{category}/{filename}"
    else:
        return render(request, "error.html", {"error": "Category not defined"})

    scope = entitled_scope_filter(request.user)
    path, is_temp = materialize_artifact(task_id, relpath, scope=scope)
    if not path:
        return render(request, "error.html", {"error": "File not found"})
    try:
        headers = {"x-apikey": integrations_cfg.virustotal.apikey}
        with open(path, "rb") as fh:
            response = requests.post(
                "https://www.virustotal.com/api/v3/files", files={"file": (filename, fh)}, headers=headers
            )
        if response.ok:
            vid = response.json().get("data", {}).get("id")
            if vid:
                hashbytes, _ = base64.b64decode(vid).split(b":")
                return render(
                    request, "success_vtup.html",
                    {"permalink": "https://www.virustotal.com/gui/file/{id}".format(id=hashbytes.decode())},
                )
        return render(request, "error.html", {"error": "Response code: {} - {}".format(response.status_code, response.reason)})
    finally:
        if is_temp:
            try:
                os.unlink(path)
            except OSError:
                pass


def central_pcapstream(request):
    """Per-connection pcap regeneration isn't materialized in S3 (the full pcap is)."""
    return render(request, "error.html", {
        "error": "Per-connection pcap stream is not yet available in central mode — download the full pcap instead"})


def central_on_demand(request):
    """On-demand re-processing is a worker/broker concern in central mode, not a UI action."""
    return render(request, "error.html", {
        "error": "On-demand detail generation is not available in central mode (re-processing is handled by workers)"})
