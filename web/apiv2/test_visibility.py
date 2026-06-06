import ast
import re

import pytest
from django.contrib.auth.models import User

# A view "enforces visibility" if its source references any of these — a read
# guard, the artifact preamble, the list filter, the web decorator, or a
# management guard (for mutation endpoints).
GUARD_MARKERS = (
    "_deny_if_hidden", "_deny_task", "_deny_manage", "_resolve_task_id", "visible_to",
    "require_task_visibility", "require_task_manage", "can_view_task", "can_manage_task",
    "can_toggle_task",
)

# Routed task_id views that legitimately need NO per-task visibility guard.
# SECURITY ALLOWLIST — keep tiny; every entry needs a real justification.
ALLOWLIST = set()


def _routed_task_views(urls_module):
    """{view_name} for every (live, non-commented) URL whose pattern captures task_id."""
    text = "\n".join(
        ln for ln in open(urls_module.__file__).read().splitlines() if not ln.lstrip().startswith("#")
    )
    out = set()
    for m in re.finditer(r"(?:re_path|path)\((.*?views\.([a-zA-Z_]+))", text, re.S):
        pattern_span, name = m.group(1), m.group(2)
        if "task_id" in pattern_span:
            out.add(name)
    return out


def _func_source(views_module, name):
    """Source of a top-level function INCLUDING its decorators (ast's
    get_source_segment on a FunctionDef starts at `def`, dropping decorators —
    we need them, since the visibility guard can be a decorator)."""
    text = open(views_module.__file__).read()
    lines = text.splitlines()
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            start = node.lineno
            if node.decorator_list:
                start = min(d.lineno for d in node.decorator_list)
            return "\n".join(lines[start - 1: node.end_lineno])
    return None


def _all_task_views():
    import apiv2.views, apiv2.urls, analysis.views, analysis.urls

    cases = []
    for urls_mod, views_mod in ((apiv2.urls, apiv2.views), (analysis.urls, analysis.views)):
        for name in sorted(_routed_task_views(urls_mod)):
            cases.append((views_mod.__name__, views_mod, name))
    return cases


_CASES = _all_task_views()


@pytest.mark.parametrize("modname,views_mod,name", _CASES, ids=[f"{m}:{n}" for m, _, n in _CASES])
def test_routed_task_view_enforces_visibility(modname, views_mod, name):
    """SECURITY GATE: every routed view that takes task_id must enforce a
    visibility/management guard, across BOTH apiv2 and analysis. Fails the build
    if a task-scoped endpoint ships without a guard (the original gate only
    checked a hardcoded apiv2 list and missed the entire analysis surface)."""
    if name in ALLOWLIST:
        pytest.skip(f"{name} explicitly allowlisted")
    src = _func_source(views_mod, name)
    if src is None:
        pytest.skip(f"{name} not found in {modname}")
    assert any(m in src for m in GUARD_MARKERS), \
        f"{modname}.{name} takes task_id but references no guard {GUARD_MARKERS} — cross-tenant leak risk"


# Aggregate/feed endpoints that return per-task data WITHOUT a task_id in their
# route, so the routed-task_id gate above can't see them. Each must still filter
# its output by the caller's visibility. Add any new cross-task feed here.
AGGREGATE_TASK_FEEDS = ("tasks_rollingsuri",)


@pytest.mark.parametrize("name", AGGREGATE_TASK_FEEDS)
def test_aggregate_feed_filters_by_viewer(name):
    """SECURITY GATE (aggregate): a feed that emits data for many tasks at once
    must reference a visibility guard, or it leaks cross-tenant task data/ids
    (the routed-task_id gate cannot catch these — no task_id in the route)."""
    import apiv2.views as views

    src = _func_source(views, name)
    if src is None:
        pytest.skip(f"{name} not found")
    assert any(m in src for m in GUARD_MARKERS), \
        f"apiv2.{name} returns cross-task data but references no guard {GUARD_MARKERS} — cross-tenant leak"


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
def test_deny_if_hidden_blocks_cross_tenant_private(mt_enabled):
    import apiv2.views as views

    other = User.objects.create_user("b", "b@x.com", "x")  # tenant None, not owner
    resp = views._deny_if_hidden(FakeReq(other), FakeTask(user_id=999, tenant_id=10, visibility="private"))
    assert resp is not None
    # indistinguishable from "not found" (H3): hidden task must NOT 403 (that
    # would confirm the task exists) — it returns the same generic 404 as a
    # missing task so other tenants' task IDs can't be enumerated.
    assert resp.status_code == 404


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
def test_toggle_visibility_authz_and_indistinguishability(cape_db, mt_enabled, monkeypatch):
    from rest_framework.test import APIClient
    from users.models import Tenant, UserProfile
    import apiv2.views as views

    ten = Tenant.objects.create(slug="t10", name="T10")

    def _in_tenant(username):
        u = User.objects.create_user(username, f"{username}@x.com", "x")
        p = UserProfile.objects.get(user=u)
        p.tenant = ten
        p.save()
        return User.objects.get(pk=u.pk)  # fresh, so request.user.userprofile is current

    owner = _in_tenant("o")
    member = _in_tenant("m")                                   # same tenant, not owner/admin
    outsider = User.objects.create_user("b", "b@x.com", "x")   # no tenant -> can't see tenant job

    state = {"vis": "tenant"}

    class T:
        id = 1

        def __init__(self):
            self.user_id = owner.id
            self.tenant_id = ten.id

        @property
        def visibility(self):
            return state["vis"]

    monkeypatch.setattr(views.db, "view_task", lambda *a, **k: T())
    monkeypatch.setattr(views.db, "set_task_visibility",
                        lambda tid, vis: state.__setitem__("vis", vis), raising=False)

    c = APIClient()

    # owner toggles their own job
    c.force_authenticate(user=owner)
    r = c.patch("/apiv2/tasks/visibility/1/", {"visibility": "public"}, format="json")
    assert r.status_code == 200, r.content
    assert state["vis"] == "public"

    # invalid visibility -> 400 (owner can already see it, so revealing this leaks nothing)
    r = c.patch("/apiv2/tasks/visibility/1/", {"visibility": "bogus"}, format="json")
    assert r.status_code == 400 and r.json().get("error") is True
    state["vis"] = "tenant"  # reset for the deny cases

    # same-tenant member CAN read the tenant job but isn't owner/admin -> 403 (no leak)
    c.force_authenticate(user=member)
    r = c.patch("/apiv2/tasks/visibility/1/", {"visibility": "public"}, format="json")
    assert r.status_code == 403
    assert state["vis"] == "tenant"  # unchanged

    # outsider can't even SEE the tenant job -> indistinguishable 404 (no enumeration oracle)
    c.force_authenticate(user=outsider)
    r = c.patch("/apiv2/tasks/visibility/1/", {"visibility": "public"}, format="json")
    assert r.status_code == 404
    assert state["vis"] == "tenant"  # unchanged
