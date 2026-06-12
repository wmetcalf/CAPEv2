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
