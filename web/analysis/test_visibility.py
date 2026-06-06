import pytest
from django.contrib.auth.models import User


class ForeignTask:
    id = 1
    user_id = 999      # owned by someone else
    tenant_id = 10
    visibility = "private"


@pytest.mark.django_db
def test_report_denies_cross_tenant_private(cape_db, mt_enabled, monkeypatch, client):
    import analysis.views as av

    monkeypatch.setattr(av.db, "view_task", lambda *a, **k: ForeignTask())
    other = User.objects.create_user("b", "b@x.com", "x")
    client.force_login(other)

    try:
        from django.urls import reverse
        url = reverse("report", kwargs={"task_id": 1})
    except Exception:
        url = "/analysis/1/"
    r = client.get(url)
    assert r.status_code == 403


@pytest.mark.django_db
def test_report_missing_task_forbidden(cape_db, monkeypatch, client):
    import analysis.views as av
    monkeypatch.setattr(av.db, "view_task", lambda *a, **k: None)
    u = User.objects.create_user("c", "c@x.com", "x")
    client.force_login(u)
    try:
        from django.urls import reverse
        url = reverse("report", kwargs={"task_id": 1})
    except Exception:
        url = "/analysis/1/"
    assert client.get(url).status_code == 403
