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
    # scope-filtering primitives for aggregate / mongo surfaces (dashboard,
    # statistics, hunt, compare): restrict an aggregation to the viewer's
    # entitled scopes instead of gating a single task_id.
    "scope_match", "entitled_scope_filter",
)

# Routed task_id views that legitimately need NO per-task visibility guard.
# SECURITY ALLOWLIST — keep tiny; every entry needs a real justification.
ALLOWLIST = set()


# URL capture-group names that identify a single task/analysis — any of these in
# a route means the view resolves tenant-scoped data and needs a guard. Note:
# `sample_id` is intentionally excluded — sample/hash-addressed routes are owned
# by the hash gate (test_hash_routed_view_enforces_visibility, which knows about
# _deny_by_hash); putting it here would double-flag those under the task gate.
ID_GROUPS = ("task_id", "analysis_number", "left_id", "right_id")


def _routed_task_views(urls_module, alias="views"):
    """{view_name} for every (live, non-commented) URL routed via ``<alias>.NAME``
    whose pattern captures one of ID_GROUPS. ``alias`` lets us scan the root
    urlconf (web/web/urls.py), where analysis views are referenced as
    ``analysis_views.NAME``. The negative lookbehind stops ``views.`` from also
    matching inside ``analysis_views.`` when scanning with alias="views"."""
    text = "\n".join(
        ln for ln in open(urls_module.__file__).read().splitlines() if not ln.lstrip().startswith("#")
    )
    out = set()
    pat = r"(?:re_path|path)\((.*?)(?<![\w.])" + re.escape(alias) + r"\.([a-zA-Z_]+)"
    for m in re.finditer(pat, text, re.S):
        pattern_span, name = m.group(1), m.group(2)
        if any(g in pattern_span for g in ID_GROUPS):
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
    import compare.views, compare.urls
    from web import urls as web_urls

    # (urls module, views module the matched names resolve to, alias used there).
    # The root urlconf (web.urls) routes file/filereport/vtupload/full_memory*
    # into analysis.views under the `analysis_views` alias — historically unscanned.
    specs = (
        (apiv2.urls, apiv2.views, "views"),
        (analysis.urls, analysis.views, "views"),
        (compare.urls, compare.views, "views"),
        (web_urls, analysis.views, "analysis_views"),
    )
    cases = []
    for urls_mod, views_mod, alias in specs:
        for name in sorted(_routed_task_views(urls_mod, alias)):
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


# Aggregate/feed views that return per-task data WITHOUT a task_id in their
# route, so the routed-task_id gate above can't see them. Each must still filter
# its output by the caller's visibility. Add any new cross-task feed here — they
# may live in EITHER apiv2.views or analysis.views (e.g. `pending`).
AGGREGATE_TASK_FEEDS = ("tasks_rollingsuri", "pending", "hunt", "search")


@pytest.mark.parametrize("name", AGGREGATE_TASK_FEEDS)
def test_aggregate_feed_filters_by_viewer(name):
    """SECURITY GATE (aggregate): a feed that emits data for many tasks at once
    must reference a visibility guard, or it leaks cross-tenant task data/ids
    (the routed-task_id gate cannot catch these — no task_id in the route).
    Scans BOTH apiv2.views and analysis.views since feeds live in either."""
    import apiv2.views, analysis.views

    src = _func_source(apiv2.views, name) or _func_source(analysis.views, name)
    assert src is not None, f"{name} not found in apiv2.views or analysis.views"
    assert any(m in src for m in GUARD_MARKERS), \
        f"{name} returns cross-task data but references no guard {GUARD_MARKERS} — cross-tenant leak"


# Mutating endpoints that take task ids from the request BODY (not the URL), so
# neither the routed-id gate nor the aggregate gate can see them. Each must gate
# every targeted id through a management guard or one tenant can delete/modify
# another tenant's tasks.
BODY_KEYED_MUTATIONS = (
    ("apiv2.views", "tasks_delete_many"),
    ("analysis.views", "tag_tasks"),
)


@pytest.mark.parametrize("modname,name", BODY_KEYED_MUTATIONS)
def test_body_keyed_mutation_enforces_manage(modname, name):
    """SECURITY GATE (body-keyed mutation): a POST view that mutates tasks by
    ids supplied in the request body must reference a management/visibility
    guard, or one tenant can act on another tenant's tasks."""
    import importlib

    mod = importlib.import_module(modname)
    src = _func_source(mod, name)
    assert src is not None, f"{name} not found in {modname}"
    assert any(m in src for m in GUARD_MARKERS), \
        f"{modname}.{name} mutates tasks by body ids but references no guard {GUARD_MARKERS} — cross-tenant integrity risk"


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


# Views that resolve/serve a sample or file by hash or sample_id (NOT routed by
# task_id, so the routed-task_id gate above can't see them). Each must reference
# _deny_by_hash (or _deny_task for a task-id variant) or it leaks samples/metadata
# across tenants.
HASH_SERVING_VIEWS = ("file", "files_view")


@pytest.mark.parametrize("name", HASH_SERVING_VIEWS)
def test_hash_addressed_view_enforces_visibility(name):
    """SECURITY GATE: hash-addressed sample/file/metadata views must reference a
    visibility guard (_deny_by_hash / _deny_task), or they leak across tenants."""
    import apiv2.views as views
    src = _func_source(views, name)
    assert src is not None, f"{name} not found in apiv2.views"
    assert ("_deny_by_hash" in src) or ("_deny_task" in src), \
        f"apiv2.{name} serves by hash/sample but references no _deny_by_hash/_deny_task guard"


def _hash_routed_views(urls_module):
    """Return the set of view names for every (live, non-commented) URL whose
    pattern captures a hash or sample-id group: md5, sha1, sha256, or sample_id.
    Mirrors _routed_task_views but for hash/sample-id groups instead of task_id."""
    HASH_GROUPS = ("md5", "sha1", "sha256", "sample_id")
    text = "\n".join(
        ln for ln in open(urls_module.__file__).read().splitlines() if not ln.lstrip().startswith("#")
    )
    out = set()
    for m in re.finditer(r"(?:re_path|path)\((.*?views\.([a-zA-Z_]+))", text, re.S):
        pattern_span, name = m.group(1), m.group(2)
        if any(f"(?P<{g}>" in pattern_span or f"<{g}>" in pattern_span for g in HASH_GROUPS):
            out.add(name)
    return out


def _all_hash_views():
    import apiv2.urls, apiv2.views
    discovered = _hash_routed_views(apiv2.urls)
    # Explicitly pin tasks_search (it filters via visible_to= in list_tasks,
    # which is in GUARD_MARKERS) so it remains covered even if its URL pattern
    # changes to a non-hash-group form in the future.
    names = discovered | {"tasks_search"}
    return [(apiv2.views, n) for n in sorted(names)]


_HASH_CASES = _all_hash_views()


HASH_GUARD_MARKERS = GUARD_MARKERS + ("_deny_by_hash",)


@pytest.mark.parametrize("views_mod,name", _HASH_CASES, ids=[n for _, n in _HASH_CASES])
def test_hash_routed_view_enforces_visibility(views_mod, name):
    """SECURITY GATE (auto-discover): every view whose URL pattern captures a
    hash or sample-id group must reference a visibility guard from GUARD_MARKERS
    or _deny_by_hash, or it leaks cross-tenant sample/task metadata. tasks_search
    is pinned here regardless of future URL-pattern changes."""
    src = _func_source(views_mod, name)
    if src is None:
        pytest.skip(f"{name} not found in {views_mod.__name__}")
    assert any(m in src for m in HASH_GUARD_MARKERS), (
        f"{views_mod.__name__}.{name} is hash/sample-id routed but references "
        f"no guard {HASH_GUARD_MARKERS} — cross-tenant leak risk"
    )


def test_hash_routed_discovery_catches_unguarded(tmp_path, monkeypatch):
    """Negative regression: confirm _hash_routed_views would flag a
    fictitious unguarded view if one were added to urls.py."""
    fake_urls = tmp_path / "fake_urls.py"
    # A URL that captures sha256 but calls a view with no guard
    fake_urls.write_text(
        'from apiv2 import views\n'
        'urlpatterns = [\n'
        '    __import__("django.urls", fromlist=["re_path"]).re_path(\n'
        '        r"^unguarded/(?P<sha256>[a-fA-F\\d]{64})/$", views.cuckoo_status\n'
        '    ),\n'
        ']\n'
    )
    import types, importlib.util
    spec = importlib.util.spec_from_file_location("fake_urls", fake_urls)
    fake_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fake_mod)
    discovered = _hash_routed_views(fake_mod)
    assert "cuckoo_status" in discovered, (
        "_hash_routed_views failed to detect a sha256-routed unguarded view"
    )
