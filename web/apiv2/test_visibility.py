import inspect

import pytest
from django.contrib.auth.models import User

# Every per-task READ endpoint must route through _deny_if_hidden (or a list
# call with visible_to=). Update this set deliberately when adding a new
# task-reading endpoint — it's the anti-cross-tenant-leak gate.
TASK_READ_VIEWS = [
    "tasks_view",
    "tasks_report",
    "tasks_iocs",
    "tasks_screenshot",
    "tasks_pcap",
    "tasks_tlspcap",
    "tasks_evtx",
    "tasks_dropped",
    "tasks_surifile",
    "tasks_procmemory",
    "tasks_fullmemory",
    "tasks_payloadfiles",
    "tasks_procdumpfiles",
    "tasks_config",
    "tasks_mitmdump",
    "tasks_selfextracted",
]

# A view enforces visibility if it calls _deny_if_hidden directly, or routes
# through the _resolve_task_id artifact preamble (which calls _deny_if_hidden).
GUARD_MARKERS = ("_deny_if_hidden", "_resolve_task_id")


def _func_source(name):
    """Source of a top-level function by name, read from the views.py FILE.
    (inspect.getsource on an @api_view-decorated view returns the DRF wrapper,
    not the handler — so read the file and pull the def out with ast.)"""
    import ast
    import apiv2.views as views

    text = open(views.__file__).read()
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(text, node)
    return None


@pytest.mark.parametrize("name", TASK_READ_VIEWS)
def test_read_view_enforces_visibility(name):
    src = _func_source(name)
    if src is None:
        pytest.skip(f"{name} not present in this build")
    assert any(m in src for m in GUARD_MARKERS), \
        f"{name} enforces no visibility guard ({GUARD_MARKERS}) — cross-tenant leak risk"


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
