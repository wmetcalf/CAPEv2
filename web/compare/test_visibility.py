import pytest
from django.contrib.auth.models import User


class ForeignTask:
    id = 1
    user_id = 999      # owned by another tenant's user
    tenant_id = 10
    visibility = "private"


@pytest.mark.django_db
def test_compare_both_denies_cross_tenant(cape_db, mt_enabled, monkeypatch, client):
    """compare.both reads two analyses by info.id; the seed gate must deny a
    viewer who can't read them (hidden == "No analysis found")."""
    import compare.views as cv

    class FakeDB:
        def view_task(self, tid):
            return ForeignTask()

    monkeypatch.setattr(cv, "Database", lambda: FakeDB())
    client.force_login(User.objects.create_user("cmp", "cmp@x.com", "x"))  # tenant-less

    r = client.get("/compare/1/2/")
    assert r.status_code == 200
    assert b"No analysis found" in r.content


@pytest.mark.django_db
def test_compare_left_denies_cross_tenant(cape_db, mt_enabled, monkeypatch, client):
    import compare.views as cv

    class FakeDB:
        def view_task(self, tid):
            return ForeignTask()

    monkeypatch.setattr(cv, "Database", lambda: FakeDB())
    client.force_login(User.objects.create_user("cl", "cl@x.com", "x"))

    r = client.get("/compare/1/")
    assert r.status_code == 200
    assert b"No analysis found" in r.content
