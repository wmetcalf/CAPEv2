import pytest
from django.contrib.auth.models import User


class FakeTask:
    def __init__(self, user_id, tenant_id, visibility):
        self.id = 1
        self.user_id = user_id
        self.tenant_id = tenant_id
        self.visibility = visibility


class FakeReq:
    def __init__(self, user):
        self.user = user


@pytest.mark.django_db
def test_deny_if_hidden_blocks_cross_tenant_private():
    import apiv2.views as views

    other = User.objects.create_user("b", "b@x.com", "x")  # tenant None, not owner
    resp = views._deny_if_hidden(FakeReq(other), FakeTask(user_id=999, tenant_id=10, visibility="private"))
    assert resp is not None
    assert resp.status_code == 403


@pytest.mark.django_db
def test_deny_if_hidden_missing_task():
    import apiv2.views as views
    other = User.objects.create_user("b", "b@x.com", "x")
    assert views._deny_if_hidden(FakeReq(other), None) is not None  # not found


@pytest.mark.django_db
def test_deny_if_hidden_owner_allowed():
    import apiv2.views as views
    owner = User.objects.create_user("o", "o@x.com", "x")
    # owner of a private job -> allowed (None == no denial)
    assert views._deny_if_hidden(FakeReq(owner), FakeTask(user_id=owner.id, tenant_id=10, visibility="private")) is None


@pytest.mark.django_db
def test_deny_if_hidden_public_allowed():
    import apiv2.views as views
    other = User.objects.create_user("b", "b@x.com", "x")
    assert views._deny_if_hidden(FakeReq(other), FakeTask(user_id=999, tenant_id=10, visibility="public")) is None


@pytest.mark.django_db
def test_toggle_visibility_owner_allowed_other_denied(monkeypatch):
    from rest_framework.test import APIClient
    import apiv2.views as views

    state = {"vis": "private"}
    owner = User.objects.create_user("o", "o@x.com", "x")

    class T:
        id = 1
        tenant_id = 10

        def __init__(self):
            self.user_id = owner.id

        @property
        def visibility(self):
            return state["vis"]

    monkeypatch.setattr(views.db, "view_task", lambda *a, **k: T())
    monkeypatch.setattr(views.db, "set_task_visibility",
                        lambda tid, vis: state.__setitem__("vis", vis), raising=False)

    c = APIClient()

    # owner toggles their own job
    c.force_authenticate(user=owner)
    r = c.patch("/apiv2/tasks/visibility/1/", {"visibility": "tenant"}, format="json")
    assert r.status_code == 200, r.content
    assert state["vis"] == "tenant"

    # invalid visibility rejected
    r = c.patch("/apiv2/tasks/visibility/1/", {"visibility": "bogus"}, format="json")
    assert r.json().get("error") is True

    # a different user (other tenant) cannot toggle
    other = User.objects.create_user("b", "b@x.com", "x")
    c.force_authenticate(user=other)
    r = c.patch("/apiv2/tasks/visibility/1/", {"visibility": "public"}, format="json")
    assert r.status_code == 403
