import pytest
from django.contrib.auth.models import User


class _Req:
    def __init__(self, user):
        self.user = user


@pytest.mark.django_db
def test_deny_by_hash_blocks_when_no_visible_task(cape_db, mt_enabled, monkeypatch):
    import apiv2.views as views

    class Sample:
        id = 7
    monkeypatch.setattr(views.db, "find_sample", lambda **k: Sample(), raising=False)
    monkeypatch.setattr(views.db, "list_tasks", lambda **k: [], raising=False)  # none visible

    other = User.objects.create_user("b", "b@x.com", "x")
    resp = views._deny_by_hash(_Req(other), sha256="a" * 64)
    assert resp is not None and resp.status_code == 404


@pytest.mark.django_db
def test_deny_by_hash_allows_when_visible_task_exists(cape_db, mt_enabled, monkeypatch):
    import apiv2.views as views

    class Sample:
        id = 7
    monkeypatch.setattr(views.db, "find_sample", lambda **k: Sample(), raising=False)
    monkeypatch.setattr(views.db, "list_tasks", lambda **k: [object()], raising=False)  # 1 visible
    u = User.objects.create_user("o", "o@x.com", "x")
    assert views._deny_by_hash(_Req(u), sha256="a" * 64) is None


@pytest.mark.django_db
def test_deny_by_hash_missing_sample_is_404(cape_db, mt_enabled, monkeypatch):
    import apiv2.views as views
    monkeypatch.setattr(views.db, "find_sample", lambda **k: None, raising=False)
    u = User.objects.create_user("o", "o@x.com", "x")
    resp = views._deny_by_hash(_Req(u), sha256="a" * 64)
    assert resp is not None and resp.status_code == 404
