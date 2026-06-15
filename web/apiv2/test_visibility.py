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
    import guac.views, guac.urls
    from web import urls as web_urls

    # (urls module, views module the matched names resolve to, alias used there).
    # The root urlconf (web.urls) routes file/filereport/vtupload/full_memory*
    # into analysis.views under the `analysis_views` alias — historically unscanned.
    specs = (
        (apiv2.urls, apiv2.views, "views"),
        (analysis.urls, analysis.views, "views"),
        (compare.urls, compare.views, "views"),
        (guac.urls, guac.views, "views"),
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
AGGREGATE_TASK_FEEDS = ("tasks_rollingsuri", "pending", "hunt", "search", "cuckoo_status", "task_x_hours")


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


# Endpoints that emit the base64 session_data used to mint a Guacamole live-VM
# session, or otherwise gate a remote-desktop tunnel into a running analysis VM.
# Each must gate the task — a tunnel into another tenant's live malware VM is the
# highest-severity leak class.
GUAC_SESSION_VIEWS = (
    ("submission.views", "status"),
    ("submission.views", "remote_session"),
)


@pytest.mark.parametrize("modname,name", GUAC_SESSION_VIEWS)
def test_guac_session_view_enforces_visibility(modname, name):
    """SECURITY GATE (live-VM tunnel): a view that emits a guac session token
    must gate the task, or a cross-tenant user can open a keyboard/mouse/frame-
    buffer tunnel into another tenant's running VM."""
    import importlib

    mod = importlib.import_module(modname)
    src = _func_source(mod, name)
    assert src is not None, f"{name} not found in {modname}"
    assert any(m in src for m in GUARD_MARKERS), \
        f"{modname}.{name} emits a guac session token but references no guard {GUARD_MARKERS} — cross-tenant live-VM tunnel risk"


def test_guac_websocket_consumer_rechecks_visibility():
    """SECURITY GATE (websocket): the guac tunnel consumer is not URL-routed, so
    the routed gates can't see it. It must re-check task visibility (defense in
    depth behind the mint-time gate)."""
    import guac.consumers

    src = open(guac.consumers.__file__).read()
    assert "can_view_task" in src, \
        "guac websocket consumer must re-check task visibility (can_view_task) — defense-in-depth for the live-VM tunnel"


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


@pytest.mark.django_db
def test_tasks_delete_many_skips_unmanageable_cross_tenant(cape_db, mt_enabled, monkeypatch):
    """A tenant-less user POSTing another tenant's private task id to the bulk-
    delete endpoint must NOT delete it (the worst confirmed critical)."""
    from rest_framework.test import APIRequestFactory, force_authenticate
    import apiv2.views as views

    deleted = []
    monkeypatch.setattr(views.db, "view_task",
                        lambda tid: FakeTask(user_id=999, tenant_id=10, visibility="private"))
    monkeypatch.setattr(views.db, "delete_task", lambda tid: deleted.append(tid) or True)
    monkeypatch.setattr(views, "mongo_delete_data", lambda *a, **k: None, raising=False)

    u = User.objects.create_user("dm", "dm@x.com", "x")  # tenant-less -> can't manage
    req = APIRequestFactory().post("/apiv2/tasks/delete_many/", {"ids": "1"})
    force_authenticate(req, user=u)
    resp = views.tasks_delete_many(req)

    assert deleted == []                       # cross-tenant task NOT deleted
    assert resp.data.get(1) == "not exists"    # indistinguishable from missing


@pytest.mark.django_db
def test_pcap_create_stamps_submission_scope(cape_db, mt_enabled, monkeypatch):
    """REGRESSION (T5): the apiv2 file-create PCAP branch must stamp the caller's
    submission scope onto the task — tenant_id / visibility / user_id — exactly
    like the adjacent add_static branch. Before the fix it called db.add_pcap(
    file_path=...) with NO scope kwargs, so API-submitted PCAP tasks landed
    unscoped (tenant_id None, user_id 0) instead of the submitter's tenant: a
    cross-tenant visibility defect on the API PCAP path (the web form already
    stamped correctly). Captures the add_pcap kwargs — the scope keys are absent
    entirely before the fix and present after."""
    from rest_framework.test import APIRequestFactory, force_authenticate
    from django.core.files.uploadedfile import SimpleUploadedFile
    from users.models import Tenant, UserProfile
    import apiv2.views as views

    ten = Tenant.objects.create(slug="acme", name="Acme")
    u = User.objects.create_user("pcapal", "pcapal@x.com", "x")
    prof = UserProfile.objects.get(user=u)
    prof.tenant = ten
    prof.save()
    u = User.objects.get(pk=u.pk)  # refresh so request.user.userprofile is current

    captured = {}
    monkeypatch.setattr(views.db, "list_machines", lambda *a, **k: [])
    monkeypatch.setattr(views.db, "add_pcap", lambda **kw: captured.update(kw) or 7, raising=False)
    # Skip real demux/copy: hand the view one ready pcap entry (content, tmp_path, _).
    monkeypatch.setattr(
        views, "process_new_task_files",
        lambda request, files, details, opt_filename, unique: ([(b"data", b"/tmp/x.pcap", None)], details),
    )

    req = APIRequestFactory().post(
        "/apiv2/tasks/create/file/",
        {"pcap": "1", "file": SimpleUploadedFile("x.pcap", b"\xd4\xc3\xb2\xa1pcapdata")},
        format="multipart",
    )
    force_authenticate(req, user=u)
    resp = views.tasks_create_file(req)

    assert resp.status_code == 200, getattr(resp, "data", resp)
    # The scope kwargs must reach add_pcap (absent entirely before the fix).
    assert captured.get("tenant_id") == ten.id, f"PCAP task not tenant-scoped: {captured}"
    assert captured.get("user_id") == u.id, f"PCAP task owner not stamped: {captured}"
    assert "visibility" in captured, f"PCAP task visibility not stamped: {captured}"


def _build_recovery_chain(views):
    """A foreign (tenant-20 private) analysis N plus a PUBLIC recovery wrapper R
    whose custom='Recovery_<N>'. validate_task() transparently resolves R -> N.
    Returns (recovery_id, foreign_id)."""
    from lib.cuckoo.core.data.task import Task, TASK_REPORTED, TASK_RECOVERED

    foreign = Task(target="secret.exe")
    foreign.category = "file"
    foreign.user_id, foreign.tenant_id, foreign.visibility = 999, 20, "private"
    foreign.status = TASK_REPORTED
    views.db.session.add(foreign)
    views.db.session.commit()
    nid = foreign.id

    rec = Task(target="wrapper.exe")
    rec.category = "file"
    rec.user_id, rec.tenant_id, rec.visibility = 0, None, "public"   # viewer B can see the wrapper
    rec.status = TASK_RECOVERED
    rec.custom = f"Recovery_{nid}"
    views.db.session.add(rec)
    views.db.session.commit()
    return rec.id, nid


@pytest.mark.django_db
def test_reauthorize_rtid_denies_cross_tenant_recovery(mt_enabled, monkeypatch):
    """UNIT (T12): _reauthorize_rtid re-checks the RESOLVED task. A recovery chain
    resolving to a task the viewer can't read -> denial; to one they can -> switch
    to rtid; no rtid -> passthrough."""
    import apiv2.views as views

    other = User.objects.create_user("rtu", "rtu@x.com", "x")  # tenant-less
    monkeypatch.setattr(views.db, "view_task",
                        lambda *a, **k: FakeTask(user_id=999, tenant_id=20, visibility="private"))
    tid, denied = views._reauthorize_rtid(FakeReq(other), 1, {"rtid": 2})
    assert denied is not None and denied.status_code == 404 and tid == 1  # foreign -> denied

    monkeypatch.setattr(views.db, "view_task",
                        lambda *a, **k: FakeTask(user_id=999, tenant_id=20, visibility="public"))
    tid, denied = views._reauthorize_rtid(FakeReq(other), 1, {"rtid": 2})
    assert denied is None and tid == 2  # visible resolved task -> switch to rtid

    tid, denied = views._reauthorize_rtid(FakeReq(other), 5, {})
    assert denied is None and tid == 5  # no rtid -> passthrough


@pytest.mark.django_db
@pytest.mark.parametrize("name,call", [
    ("tasks_view", lambda views, req, rid: views.tasks_view(req, rid)),
    ("tasks_iocs", lambda views, req, rid: views.tasks_iocs(req, rid)),
    ("tasks_pcap_variant", lambda views, req, rid: views.tasks_pcap_variant(req, rid, "decrypted")),
])
def test_recovery_chain_reauthorizes_resolved_task(cape_db, mt_enabled, monkeypatch, name, call):
    """REGRESSION (T12): a TASK_RECOVERED task (custom='Recovery_<N>') makes
    validate_task resolve to the underlying task N. The viewer was authorized
    against the PUBLIC recovery wrapper, not N — so serving N without re-checking
    lets a recovery chain read another tenant's analysis. Every artifact endpoint
    must re-authorize the resolved task. Covers all three code paths: the separate
    tasks_view recovery branch, the inline rtid switch, and the shared
    _resolve_task_id preamble. Without the fix the resolved foreign task leaks
    back (non-404); with it the resolved task is denied (indistinguishable 404)."""
    from rest_framework.test import APIRequestFactory, force_authenticate
    import apiv2.views as views

    rid, nid = _build_recovery_chain(views)
    other = User.objects.create_user("recov", "recov@x.com", "x")  # tenant-less, can't see N
    req = APIRequestFactory().get("/x/")
    force_authenticate(req, user=other)

    resp = call(views, req, rid)
    assert resp.status_code == 404, (
        f"{name}: recovery chain to foreign task {nid} was NOT re-authorized "
        f"(status={resp.status_code}, body={getattr(resp, 'data', '')!r})"
    )


# --- T2: apiv2 file() by-hash sample download full-stack cross-tenant denial ---

class _Sample:
    id = 7


@pytest.mark.django_db
def test_file_by_hash_denies_cross_tenant(cape_db, mt_enabled, monkeypatch):
    """apiv2 file() streams a sample by hash. The by-hash gate (_deny_by_hash ->
    can_view_sample) must withhold a sample with NO viewer-visible task: an
    indistinguishable 404, no sample bytes, no Content-Disposition. The predicate
    is unit-tested, but no test drove file() end-to-end through routing+DRF auth."""
    from rest_framework.test import APIClient
    import apiv2.views as views
    import lib.cuckoo.core.database as dbmod

    served = {"path": False}
    monkeypatch.setattr(views.apiconf, "sampledl", {"enabled": True}, raising=False)
    monkeypatch.setattr(dbmod._DATABASE, "find_sample", lambda **k: _Sample(), raising=False)
    monkeypatch.setattr(dbmod._DATABASE, "list_tasks", lambda **k: [], raising=False)  # none visible to B
    monkeypatch.setattr(views.db, "sample_path_by_hash",
                        lambda *a, **k: served.update(path=True) or ["/x"], raising=False)

    c = APIClient()
    c.force_authenticate(user=User.objects.create_user("fb", "fb@x.com", "x"))  # tenant-less
    r = c.get("/apiv2/files/get/sha256/" + "a" * 64 + "/")
    assert r.status_code == 404, r.content
    assert served["path"] is False           # gate fired before any path resolution -> no bytes
    assert "Content-Disposition" not in r


@pytest.mark.django_db
def test_file_by_hash_allows_when_visible(cape_db, mt_enabled, monkeypatch):
    """Positive control: when the viewer HAS a visible task referencing the sample,
    file() passes the by-hash gate and proceeds to resolve a path — proving denial
    is conditional on entitlement, not blanket."""
    from rest_framework.test import APIClient
    import apiv2.views as views
    import lib.cuckoo.core.database as dbmod

    served = {"path": False}
    monkeypatch.setattr(views.apiconf, "sampledl", {"enabled": True}, raising=False)
    monkeypatch.setattr(dbmod._DATABASE, "find_sample", lambda **k: _Sample(), raising=False)
    monkeypatch.setattr(dbmod._DATABASE, "list_tasks", lambda **k: [object()], raising=False)  # one visible
    monkeypatch.setattr(views.db, "sample_path_by_hash",
                        lambda *a, **k: served.update(path=True) or [], raising=False)

    c = APIClient()
    c.force_authenticate(user=User.objects.create_user("fv", "fv@x.com", "x"))
    r = c.get("/apiv2/files/get/sha256/" + "a" * 64 + "/")
    assert served["path"] is True            # passed the gate -> attempted to resolve a path


# Task-CREATING db calls in the submission views must stamp the caller's
# submission scope (tenant_id= + visibility=); otherwise the created task lands
# unscoped (tenant_id None) and becomes cross-tenant visible — the T5 PCAP
# regression. AST-checked (not substring) so multi-line call sites and any NEW
# add_* call can't ship without scope.
SCOPE_STAMPING_METHODS = {"add_pcap", "add_static", "add_url", "add_path"}


def test_submission_add_calls_stamp_scope():
    """SECURITY GATE (scope stamping): every db.add_{pcap,static,url,path}() call
    in the submission views must pass tenant_id= and visibility=, or the created
    task is unscoped and leaks across tenants. Locks the T5 fix structurally."""
    import ast
    import importlib

    offenders = []
    for modname in ("apiv2.views", "submission.views"):
        mod = importlib.import_module(modname)
        tree = ast.parse(open(mod.__file__).read())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in SCOPE_STAMPING_METHODS):
                kws = {kw.arg for kw in node.keywords}
                missing = {"tenant_id", "visibility"} - kws
                if missing:
                    offenders.append(f"{modname}:{node.lineno} {node.func.attr} missing {sorted(missing)}")
    assert not offenders, (
        f"Submission task-creating call(s) without scope stamping: {offenders}. "
        f"Pass tenant_id= and visibility= (from submission_scope) so the task is "
        f"not created cross-tenant visible."
    )


# --- T9: tasks_search by-hash existence-oracle indistinguishability ---

@pytest.mark.django_db
def test_tasks_search_hash_is_indistinguishable_oracle(cape_db, mt_enabled, monkeypatch):
    """tasks_search by hash must return a response BYTE-IDENTICAL for a foreign-
    but-existing sample (a Sample row exists, but no viewer-visible task) and a
    never-seen hash ({data:[],error:False}) — else the data/error shape becomes a
    cross-tenant existence oracle. Drives the real view as a tenant-less viewer."""
    from rest_framework.test import APIClient
    import apiv2.views as views

    class _S:
        id = 7

        def to_dict(self):
            return {"id": 7}

    FOREIGN = "a" * 64
    NEVER = "b" * 64

    def _find(**k):
        if k.get("sha256") == FOREIGN:
            return _S()              # sample exists
        if "parent" in k:
            return []                # no child samples
        return None                  # never-seen hash

    monkeypatch.setattr(views.db, "find_sample", _find, raising=False)
    monkeypatch.setattr(views.db, "list_tasks", lambda **k: [], raising=False)  # no visible task for B

    c = APIClient()
    c.force_authenticate(user=User.objects.create_user("ts", "ts@x.com", "x"))  # tenant-less
    r_foreign = c.get(f"/apiv2/tasks/search/sha256/{FOREIGN}/")
    r_never = c.get(f"/apiv2/tasks/search/sha256/{NEVER}/")
    assert r_foreign.status_code == r_never.status_code == 200
    # foreign-existing and never-seen must be byte-identical (no existence oracle)
    assert r_foreign.json() == r_never.json() == {"data": [], "error": False}


def test_every_perform_search_caller_passes_viewer():
    """SECURITY GATE: perform_search() is unscoped by default (no tenant filter
    at the mongo/ES layer). Every web caller MUST pass viewer= so the query is
    tenant-scoped — otherwise it leaks cross-tenant task ids / hashes / detections
    / artifact paths (this is exactly how the capeyarazipall + report-existent_tasks
    leaks happened). Fails the build if any caller omits viewer=."""
    import ast
    import importlib

    offenders = []
    for modname in ("analysis.views", "apiv2.views", "submission.views"):
        mod = importlib.import_module(modname)
        tree = ast.parse(open(mod.__file__).read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "perform_search":
                if "viewer" not in {kw.arg for kw in node.keywords}:
                    offenders.append(f"{modname}:{node.lineno}")
    assert not offenders, f"perform_search() called without viewer= (cross-tenant leak risk) at: {offenders}"


def test_viewer_scope_match_locked_vs_disabled(monkeypatch):
    """The systemic perform_search scope helper: a locked-mode tenant viewer gets
    a public/tenant/mine $or; break-glass and MT-disabled get None (no filter).
    _viewer_scope_match reads lib.cuckoo.common.tenancy.multitenancy_config at
    call time, so patch THAT (not users.tenancy's copy)."""
    from lib.cuckoo.common import web_utils
    from lib.cuckoo.common.tenancy import Viewer, MTConfig
    import lib.cuckoo.common.tenancy as t

    monkeypatch.setattr(t, "multitenancy_config", lambda: MTConfig(True, "locked", "", True))
    v = Viewer(user_id=2, tenant_id=10)              # locked, non-admin
    f = web_utils._viewer_scope_match(v)
    assert "$or" in f
    assert {"info.visibility": "public"} in f["$or"]
    assert {"info.tenant_id": 10, "info.visibility": "tenant"} in f["$or"]
    assert {"info.user_id": 2} in f["$or"]

    # break-glass -> no filter
    assert web_utils._viewer_scope_match(Viewer(user_id=9, tenant_id=None, is_local_admin=True)) is None
    # MT disabled -> no filter
    monkeypatch.setattr(t, "multitenancy_config", lambda: MTConfig(False, "shared", "", True))
    assert web_utils._viewer_scope_match(v) is None
    # no viewer -> no filter
    assert web_utils._viewer_scope_match(None) is None


def test_viewer_scope_es_filter_locked_vs_disabled(monkeypatch):
    """ES analogue of the scope helper: locked-mode tenant viewer gets a
    public/own-tenant/mine bool-should; break-glass / disabled / None -> no
    filter (so the ES search branches are scoped the same as the mongo ones)."""
    from lib.cuckoo.common import web_utils
    from lib.cuckoo.common.tenancy import Viewer, MTConfig
    import lib.cuckoo.common.tenancy as t

    monkeypatch.setattr(t, "multitenancy_config", lambda: MTConfig(True, "locked", "", True))
    v = Viewer(user_id=2, tenant_id=10)
    f = web_utils._viewer_scope_es_filter(v)
    shoulds = f["bool"]["should"]
    assert {"term": {"info.visibility": "public"}} in shoulds
    assert {"term": {"info.user_id": 2}} in shoulds
    assert any(c.get("bool", {}).get("filter") == [{"term": {"info.tenant_id": 10}}, {"term": {"info.visibility": "tenant"}}] for c in shoulds)
    assert f["bool"]["minimum_should_match"] == 1

    # anon/tenant-less in locked mode -> public only (never global)
    anon = web_utils._viewer_scope_es_filter(Viewer(user_id=None, tenant_id=None))
    assert anon["bool"]["should"] == [{"term": {"info.visibility": "public"}}]

    # break-glass / disabled / None -> no filter
    assert web_utils._viewer_scope_es_filter(Viewer(user_id=9, tenant_id=None, is_local_admin=True)) is None
    monkeypatch.setattr(t, "multitenancy_config", lambda: MTConfig(False, "shared", "", True))
    assert web_utils._viewer_scope_es_filter(v) is None
    assert web_utils._viewer_scope_es_filter(None) is None


# Multi-doc mongo pivots (mongo_aggregate / mongo_find) can span tenants — unlike
# task_id-keyed mongo_find_one / es.search reads gated by the view decorator. Each
# web view issuing one MUST be reviewed to be tenant-scoped; a NEW caller trips
# the gate below so a *secondary* unscoped cross-task query (the leak class behind
# report()/existent_tasks and the compare/hunt pivots) can't land silently. The
# marker gates can't catch this — they pass as long as ANY guard string appears in
# the function, even when a second query in the same function is unscoped.
# perform_search pivots are covered by test_every_perform_search_caller_passes_viewer.
CROSS_TASK_MONGO_PIVOTS = {"mongo_aggregate", "mongo_find"}
REVIEWED_MONGO_PIVOTS = {
    "analysis.views:index": "mongo_find by info.id $in IDs from list_tasks(visible_to=) — scoped upstream",
    "analysis.views:search_behavior": "mongo_find('calls', _id $in) — ObjectIds from the gated task's own behavior doc",
    "analysis.views:report": "mongo_aggregate $match info.id == the can_view_task-gated task_id",
    "analysis.views:hunt": "mongo_aggregate $facet pinned by entitled_scope_filter()",
    "apiv2.views:tasks_rollingsuri": "mongo_find then per-row can_view_task (+ is_local_admin fast-path)",
    "compare.views:left": "mongo_find md5-pivot AND-ed with entitled_scope_filter()",
    "compare.views:hash": "mongo_find md5-pivot AND-ed with entitled_scope_filter()",
}


def _functions_calling(modname, names):
    import ast
    import importlib

    mod = importlib.import_module(modname)
    with open(mod.__file__, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    out = set()
    # Include async views — an async def issuing an unscoped pivot must not bypass the gate.
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                nm = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if nm in names:
                    out.add(f"{modname}:{fn.name}")
    return out


def test_cross_task_mongo_pivots_are_reviewed():
    """SECURITY GATE: every web view issuing a multi-doc mongo pivot
    (mongo_aggregate/mongo_find) must be in REVIEWED_MONGO_PIVOTS — i.e. proven
    tenant-scoped. A new (or newly-added) pivot trips this gate so a secondary
    unscoped cross-task query can't ship without review. Closes the marker-gate
    blind spot that let report()'s existent_tasks pivot leak while its primary
    read was gated."""
    # Import directly (no silent skip): a module that can't be scanned is a
    # coverage hole, not a pass — the gate must fail loudly rather than miss a
    # module's pivots. These all import in the test env (same set the routed gate
    # scans via _all_task_views()).
    found = set()
    for modname in ("analysis.views", "apiv2.views", "compare.views", "dashboard.views", "submission.views", "guac.views"):
        found |= _functions_calling(modname, CROSS_TASK_MONGO_PIVOTS)

    unreviewed = sorted(found - set(REVIEWED_MONGO_PIVOTS))
    assert not unreviewed, (
        f"Unreviewed cross-task mongo pivot(s): {unreviewed}. A multi-doc "
        f"mongo_aggregate/mongo_find can span tenants — verify it is tenant-scoped "
        f"(scope_match / entitled_scope_filter / per-row can_view_task / task_id-keyed "
        f"after a gate) and add it to REVIEWED_MONGO_PIVOTS with the reason."
    )
    # Keep the allowlist tight: drop entries whose function no longer exists.
    stale = sorted(set(REVIEWED_MONGO_PIVOTS) - found)
    assert not stale, f"Stale REVIEWED_MONGO_PIVOTS entries (function gone): {stale}"


# By-hash access to the global content-addressed sample store (storage/binaries
# + db.sample_path_by_hash) is shared across tenants, so it MUST go through the
# visible-task boundary (tenancy.can_view_sample / _deny_by_hash / sample_path_
# by_hash(visible_to=)). A web view that resolves a sample by attacker-supplied
# hash WITHOUT one of those markers streams another tenant's bytes (the deep-hunt
# capeyarazipall + resubmit + download-services criticals). This gate trips on any
# such function lacking a by-hash guard.
BYHASH_RESOLVERS = ("sample_path_by_hash",)            # call markers
BYHASH_GUARDS = ("can_view_sample", "_deny_by_hash", "visible_to")


def test_byhash_sample_resolution_is_gated():
    """SECURITY GATE: any web view that resolves a sample by hash (calls
    sample_path_by_hash, or builds a storage/binaries/<hash> path) must reference
    a by-hash entitlement guard (can_view_sample / _deny_by_hash / visible_to).
    Locks the deep-hunt byte-exfil fixes so a new by-hash surface can't ship
    ungated."""
    import ast
    import importlib

    offenders = []
    for modname in ("analysis.views", "apiv2.views", "submission.views", "compare.views"):
        mod = importlib.import_module(modname)
        with open(mod.__file__, encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src)
        lines = src.splitlines()
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            body = "\n".join(lines[fn.lineno - 1: fn.end_lineno])
            resolves_byhash = any(r in body for r in BYHASH_RESOLVERS) or (
                '"binaries"' in body or "'binaries'" in body
            )
            if resolves_byhash and not any(g in body for g in BYHASH_GUARDS):
                offenders.append(f"{modname}:{fn.name}")
    assert not offenders, (
        f"By-hash sample resolution without an entitlement guard {BYHASH_GUARDS}: "
        f"{offenders}. Gate via can_view_sample / _deny_by_hash / sample_path_by_hash(visible_to=) "
        f"— an attacker-supplied hash must not stream another tenant's sample bytes."
    )


# --- T10: download_from_3rdparty local-cache reuse is scoped by viewer ---

@pytest.mark.django_db
def test_download_from_3rdparty_scopes_cache_reuse(cape_db, mt_enabled, monkeypatch):
    """download_from_3rdparty reuses a locally-cached sample only when the
    requester is entitled (db.sample_path_by_hash(h, visible_to=viewer)). A
    non-entitled tenant must NOT receive another tenant's cached bytes: the call
    must pass the viewer and, when not entitled, fall through to the external
    downloader as if uncached. This boundary lives in web_utils (outside the
    by-hash VIEW gate's scan), so it had no automated test."""
    from lib.cuckoo.common import web_utils
    from lib.cuckoo.common.tenancy import Viewer

    H = "a" * 64
    seen = {}
    monkeypatch.setattr(web_utils.db, "sample_path_by_hash",
                        lambda h, visible_to=None: seen.update(h=h, visible_to=visible_to) or [],
                        raising=False)

    class _DL:   # external downloader returns a sentinel distinct from any local copy
        def download(self, h, apikey=None):
            return (b"EXTERNAL", "VirusTotal")

    monkeypatch.setattr(web_utils, "downloader_services", _DL(), raising=False)
    monkeypatch.setattr(web_utils, "download_file", lambda **d: ("ok", {"task_ids": [1]}), raising=False)

    viewer = Viewer(user_id=5, tenant_id=99)
    details = {"errors": [], "viewer": viewer, "apikey": None}
    web_utils.download_from_3rdparty(H, "", details)

    assert seen.get("visible_to") is viewer            # entitlement consulted with the requester's viewer
    assert details.get("content") == b"EXTERNAL"       # not entitled -> NOT reused local; fetched external
    assert details.get("service") == "VirusTotal"


@pytest.mark.django_db
def test_download_from_3rdparty_reuses_when_entitled(cape_db, mt_enabled, monkeypatch):
    """Positive control: when the viewer IS entitled (sample_path_by_hash returns
    a path), the local cache is reused and the external downloader is NOT hit —
    proving the gate is conditional, not blanket-blocking."""
    from lib.cuckoo.common import web_utils
    from lib.cuckoo.common.tenancy import Viewer

    dl = {"n": 0}
    monkeypatch.setattr(web_utils.db, "sample_path_by_hash",
                        lambda h, visible_to=None: ["/x/sample"], raising=False)
    monkeypatch.setattr(web_utils, "get_file_content", lambda paths: b"LOCALBYTES", raising=False)

    class _DL:
        def download(self, h, apikey=None):
            dl["n"] += 1
            return (b"EXTERNAL", "VirusTotal")

    monkeypatch.setattr(web_utils, "downloader_services", _DL(), raising=False)
    monkeypatch.setattr(web_utils, "download_file", lambda **d: ("ok", {"task_ids": [1]}), raising=False)

    details = {"errors": [], "viewer": Viewer(user_id=5, tenant_id=99), "apikey": None}
    web_utils.download_from_3rdparty("a" * 64, "", details)

    assert details.get("service") == "Local" and details.get("content") == b"LOCALBYTES"
    assert dl["n"] == 0     # entitled -> reused local cache, never fetched external


# --- T11: aggregate-count endpoints reflect only the viewer's entitled scope ---

def _seed_one_public_two_foreign(views):
    """1 public task (visible to everyone) + 2 tenant-20 private tasks (foreign)."""
    from lib.cuckoo.core.data.task import Task
    for vis, ten, uid in [("public", None, 0), ("private", 20, 999), ("private", 20, 999)]:
        t = Task(target="x.exe")
        t.category = "file"
        t.user_id, t.tenant_id, t.visibility = uid, ten, vis
        views.db.session.add(t)
    views.db.session.commit()


@pytest.mark.django_db
def test_cuckoo_status_task_counts_are_scoped(cape_db, mt_enabled, monkeypatch):
    """cuckoo_status totals come from db.get_tasks_status_count(visible_to=viewer).
    A tenant-less viewer must see only the public task (total 1), not all 3."""
    from rest_framework.test import APIClient
    import apiv2.views as views

    monkeypatch.setattr(views.apiconf, "cuckoostatus", {"enabled": True}, raising=False)
    _seed_one_public_two_foreign(views)

    c = APIClient()
    c.force_authenticate(user=User.objects.create_user("cs", "cs@x.com", "x"))  # tenant-less
    r = c.get("/apiv2/cuckoo/status/")
    assert r.status_code == 200, r.content
    assert r.json()["data"]["tasks"]["total"] == 1   # only the public task is visible


@pytest.mark.django_db
def test_task_x_hours_counts_are_scoped(cape_db, mt_enabled, monkeypatch):
    """task_x_hours buckets the last-24h tasks, filtering each through can_view_task.
    A tenant-less viewer's buckets must sum to only the visible (public) task."""
    from rest_framework.test import APIClient
    import apiv2.views as views

    _seed_one_public_two_foreign(views)
    c = APIClient()
    c.force_authenticate(user=User.objects.create_user("tx", "tx@x.com", "x"))  # tenant-less
    r = c.get("/apiv2/tasks/stats/")
    assert r.status_code == 200, r.content
    assert sum(r.json().get("stats", {}).values()) == 1   # foreign-tenant tasks excluded


@pytest.mark.django_db
def test_statistics_data_passes_viewer_per_scope(cape_db, mt_enabled, monkeypatch):
    """statistics_data: a locked-mode tenant viewer gets PER-SCOPE stats, each
    scoped to the viewer (not the flat global call). Asserts statistics() is
    invoked for the viewer's entitled scopes with viewer= set."""
    from rest_framework.test import APIClient
    from users.models import Tenant, UserProfile
    import apiv2.views as views

    calls = []
    monkeypatch.setattr(views, "statistics",
                        lambda days, scope=None, viewer=None: calls.append((scope, viewer)) or {},
                        raising=False)
    ten = Tenant.objects.create(slug="acme", name="Acme")
    u = User.objects.create_user("sd", "sd@x.com", "x")
    p = UserProfile.objects.get(user=u)
    p.tenant = ten
    p.save()
    u = User.objects.get(pk=u.pk)

    c = APIClient()
    c.force_authenticate(user=u)
    r = c.get("/apiv2/tasks/statistics/7/")
    assert r.status_code == 200, r.content
    assert calls, "statistics() was not called"
    assert all(scope in ("public", "tenant", "mine") for scope, _ in calls)  # per-scope, not flat global
    assert all(viewer is not None for _, viewer in calls)                    # every call scoped to the viewer
