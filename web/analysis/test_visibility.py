import pytest
from django.contrib.auth.models import User


class ForeignTask:
    id = 1
    user_id = 999      # owned by someone else
    tenant_id = 10
    visibility = "private"


def _report_url():
    try:
        from django.urls import reverse
        return reverse("report", kwargs={"task_id": 1})
    except Exception:
        return "/analysis/1/"


@pytest.mark.django_db
def test_report_denies_cross_tenant_private(cape_db, mt_enabled, monkeypatch, client):
    """A cross-tenant private task is not shown. The denial is the generic
    "no analysis found" page (HTTP 200), INDISTINGUISHABLE from a missing task,
    so another tenant's task IDs can't be enumerated by status code."""
    import analysis.views as av

    monkeypatch.setattr(av.db, "view_task", lambda *a, **k: ForeignTask())
    other = User.objects.create_user("b", "b@x.com", "x")
    client.force_login(other)

    r = client.get(_report_url())
    assert r.status_code == 200
    assert b"No analysis found" in r.content


@pytest.mark.django_db
def test_report_missing_task_renders_error_200(cape_db, monkeypatch, client):
    """A missing/deleted task renders the same generic error page at HTTP 200 as
    a hidden task (upstream parity + indistinguishability) — not a 403."""
    import analysis.views as av
    monkeypatch.setattr(av.db, "view_task", lambda *a, **k: None)
    u = User.objects.create_user("c", "c@x.com", "x")
    client.force_login(u)
    r = client.get(_report_url())
    assert r.status_code == 200
    assert b"No analysis found" in r.content


@pytest.mark.django_db
def test_full_memory_denies_cross_tenant(cape_db, mt_enabled, monkeypatch, client):
    """full_memory_dump_file routes via the analysis_number group (not task_id);
    the guard decorator must resolve that group and deny a cross-tenant viewer."""
    import analysis.views as av

    monkeypatch.setattr(av.db, "view_task", lambda *a, **k: ForeignTask())
    client.force_login(User.objects.create_user("fm", "fm@x.com", "x"))
    assert client.get("/full_memory/1/").status_code == 403


@pytest.mark.django_db
def test_vtupload_denies_cross_tenant(cape_db, mt_enabled, monkeypatch, client):
    """vtupload reads + exfiltrates a sample to VirusTotal — require_task_manage."""
    import analysis.views as av

    monkeypatch.setattr(av.db, "view_task", lambda *a, **k: ForeignTask())
    client.force_login(User.objects.create_user("vt", "vt@x.com", "x"))
    assert client.get("/vtupload/CAPE/1/evil.bin/abc/").status_code == 403


@pytest.mark.django_db
def test_tag_tasks_skips_unmanageable_cross_tenant(cape_db, mt_enabled, client):
    """A tenant-less user must not be able to tag another tenant's private task."""
    import json as _json
    import analysis.views as av
    from lib.cuckoo.core.data.task import Task

    t = Task(target="x.exe")
    t.category = "file"
    t.user_id, t.tenant_id, t.visibility = 999, 10, "private"
    av.db.session.add(t)
    av.db.session.commit()
    tid = t.id

    client.force_login(User.objects.create_user("tg", "tg@x.com", "x"))  # tenant-less
    r = client.post(
        "/analysis/hunt/tag/",
        data=_json.dumps({"task_ids": [tid], "tag": "pwned"}),
        content_type="application/json",
    )
    assert r.status_code == 200
    av.db.session.expire_all()
    assert "pwned" not in (av.db.session.get(Task, tid).tags_tasks or "")


@pytest.mark.django_db
def test_file_search_all_files_drops_cross_tenant_paths(cape_db, mt_enabled, monkeypatch):
    """CRITICAL leak regression (capeyarazipall): _file_search_all_files must NOT
    return artifact paths belonging to analyses the requester can't read — else
    file() streams another tenant's dropped/payload/sample bytes. The per-path
    owning-task gate drops paths under storage/analyses/<foreign_tid>/."""
    from django.test import RequestFactory
    from django.contrib.auth.models import User
    import analysis.views as av

    class OwnTask:      # task 2 — visible to the requester (public)
        id = 2; user_id = 0; tenant_id = 10; visibility = "public"

    class ForeignTask:  # task 3 — another tenant's private analysis
        id = 3; user_id = 999; tenant_id = 20; visibility = "private"

    monkeypatch.setattr(av, "perform_search", lambda *a, **k: [{"info": {"id": 2}}, {"info": {"id": 3}}])
    # yara_detected yields (kind, filepath, block, fileobj) — one own, one foreign
    monkeypatch.setattr(av, "yara_detected", lambda term, recs: [
        ("dropped", "/opt/CAPEv2/storage/analyses/2/files/own.bin", {}, {}),
        ("dropped", "/opt/CAPEv2/storage/analyses/3/files/secret.bin", {}, {}),
    ])
    monkeypatch.setattr(av, "path_exists", lambda p: True)
    monkeypatch.setattr(av.db, "view_task", lambda tid: OwnTask() if int(tid) == 2 else ForeignTask())

    req = RequestFactory().get("/file/capeyarazipall/2/Emotet/")
    req.user = User.objects.create_user("fs", "fs@x.com", "x")  # tenant-less, non-admin

    paths = av._file_search_all_files("capeyara", "Emotet", req)
    assert "/opt/CAPEv2/storage/analyses/2/files/own.bin" in paths      # readable kept
    assert "/opt/CAPEv2/storage/analyses/3/files/secret.bin" not in paths  # foreign dropped


# --- T1: filereport (downloadable full report) full-stack cross-tenant denial ---

@pytest.mark.django_db
@pytest.mark.parametrize("category", ["json", "html", "maec"])
def test_filereport_denies_cross_tenant(cape_db, mt_enabled, monkeypatch, client, category):
    """filereport serves the full DOWNLOADABLE analysis report (json/html/maec/...)
    — a critical exfil surface gated by @require_task_visibility. A cross-tenant
    viewer must get 403 and no report bytes. Sibling report() (the HTML page) has
    an HTTP test; filereport (the download) was only marker-scanned."""
    import analysis.views as av

    monkeypatch.setattr(av.db, "view_task", lambda *a, **k: ForeignTask())
    client.force_login(User.objects.create_user("fr", "fr@x.com", "x"))  # tenant-less
    r = client.get(f"/filereport/1/{category}/")
    assert r.status_code == 403
    assert b'"target"' not in r.content   # no report payload leaked


@pytest.mark.django_db
def test_filereport_allows_visible_past_gate(cape_db, mt_enabled, monkeypatch, client):
    """Positive control: a PUBLIC task passes require_task_visibility (status != 403),
    proving the denial is conditional on visibility, not blanket."""
    import analysis.views as av

    class _Pub:
        id = 1; user_id = 0; tenant_id = 10; visibility = "public"

    monkeypatch.setattr(av.db, "view_task", lambda *a, **k: _Pub())
    client.force_login(User.objects.create_user("fp", "fp@x.com", "x"))
    assert client.get("/filereport/1/json/").status_code != 403


# --- T3: analysis.search per-row cross-tenant result suppression ---

@pytest.mark.django_db
def test_search_suppresses_cross_tenant_hits(cape_db, mt_enabled, monkeypatch, client):
    """search() runs perform_search(viewer=) then a PER-ROW can_view_task before
    the heavy get_analysis_info. A foreign-tenant hit returned by the (mongo/es)
    search must be dropped from the rendered results — proving the per-row gate
    executes on every hit, not merely that viewer= is wired (what the AST gate
    can verify). This is the report()/existent_tasks leak class at the search view."""
    import analysis.views as av

    class OwnPub:    # task 2 — public, viewer-visible
        id = 2; user_id = 0; tenant_id = 10; visibility = "public"

    class Foreign:   # task 3 — another tenant's private analysis
        id = 3; user_id = 999; tenant_id = 20; visibility = "private"

    monkeypatch.setitem(av.enabledconf, "mongodb", True)
    monkeypatch.setitem(av.enabledconf, "elasticsearchdb", False)
    monkeypatch.setattr(av, "essearch", False, raising=False)
    monkeypatch.setattr(av, "es_as_db", False, raising=False)
    monkeypatch.setattr(av, "perform_search",
                        lambda *a, **k: [{"info": {"id": 2}}, {"info": {"id": 3}}])
    monkeypatch.setattr(av.db, "view_task", lambda tid: OwnPub() if int(tid) == 2 else Foreign())
    monkeypatch.setattr(av, "get_analysis_info", lambda db, task=None: {"id": task.id, "info": {"id": task.id}})

    client.force_login(User.objects.create_user("se", "se@x.com", "x"))  # tenant-less
    r = client.post("/analysis/search/", {"search": "malfamily:Emotet"})
    assert r.status_code == 200
    ids = [a.get("id") for a in (r.context.get("analyses") or [])]
    assert 2 in ids, ids            # own/public hit kept
    assert 3 not in ids, ids        # foreign-tenant hit suppressed (per-row can_view_task)


# --- T7: full_memory_strings (VM memory strings dump) cross-tenant 403 parity ---

@pytest.mark.django_db
def test_full_memory_strings_denies_cross_tenant(cape_db, mt_enabled, monkeypatch, client):
    """full_memory_dump_strings is the analysis_number-routed sibling of
    full_memory_dump_file (byte-exfil of the VM memory strings dump). It carries
    the same @require_task_visibility but had no HTTP test — only its _file
    sibling did. Cross-tenant viewer -> 403."""
    import analysis.views as av

    monkeypatch.setattr(av.db, "view_task", lambda *a, **k: ForeignTask())
    client.force_login(User.objects.create_user("fms", "fms@x.com", "x"))
    assert client.get("/full_memory_strings/1/").status_code == 403


# --- T6: hunt $facet aggregation is tenant-scoped before the facet stage ---

@pytest.mark.django_db
def test_hunt_pipeline_scopes_before_facet(cape_db, mt_enabled, monkeypatch, client, settings):
    """hunt() must $and the viewer's entitled_scope_filter $match into the mongo
    aggregation pipeline BEFORE the $facet/$group stages, or the threat-hunt
    aggregation spans every tenant. Captures the mongo_aggregate pipeline and
    asserts the scope $match is present and precedes $facet — a data-flow property
    the AST/allowlist gate cannot verify."""
    import analysis.views as av
    import dashboard.views as dv

    settings.HUNT_ENABLED = True
    SCOPE = {"$or": [{"info.visibility": "public"}, {"info.tenant_id": 10}]}
    HUNT_MAP = {"domains": {"form_key": "domains", "db_unwind": None,
                            "db_group": "$x", "validator": lambda v: True}}

    monkeypatch.setitem(av.enabledconf, "mongodb", True)
    monkeypatch.setattr(av, "load_hunt_map", lambda min_count: (HUNT_MAP, {}), raising=False)
    monkeypatch.setattr(dv, "entitled_scope_filter", lambda user: SCOPE, raising=False)

    captured = {}
    monkeypatch.setattr(av, "mongo_aggregate",
                        lambda coll, pipeline, *a, **k: captured.update(pipeline=pipeline) or [{}],
                        raising=False)

    client.force_login(User.objects.create_user("hu", "hu@x.com", "x"))
    r = client.get("/analysis/hunt/?days_back=14")
    assert r.status_code == 200, r.content[:200]
    pipeline = captured.get("pipeline")
    assert pipeline, "mongo_aggregate was not called"
    match = pipeline[0]["$match"]
    assert "$and" in match and SCOPE in match["$and"], match     # scope $and-ed into the $match
    facet_idx = next((i for i, s in enumerate(pipeline) if "$facet" in s), None)
    assert facet_idx is not None and facet_idx > 0, pipeline      # $facet comes AFTER the scoped $match
