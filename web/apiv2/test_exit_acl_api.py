"""apiv2-side gaps the xhigh review found in the tenant egress-exit ACL.

  F6   _strip_mt_task_fields did not pop "allowed_exits", so an MT-DISABLED install started emitting
       a task column upstream never had -- the exact compatibility break that helper exists to stop.
  F8   tenant_exits_list shipped with no api.conf gate, unlike every sibling endpoint in the module.
  F13  tasks_create_static was the only submit endpoint that never stamped allowed_exits, leaving the
       column NULL (== UNRESTRICTED) on the task row.

All three fail on the pre-fix code.
"""
from types import SimpleNamespace
from unittest import mock

import pytest
from django.contrib.auth.models import User
from rest_framework.test import APIRequestFactory, force_authenticate

from apiv2 import views

pytest_plugins = ("mt_test_fixtures",)


# ----------------------------------------------------------------------------------- F6 ----
def test_strip_mt_task_fields_drops_allowed_exits_when_mt_is_off(mt_disabled):
    d = {"id": 1, "tenant_id": 3, "visibility": "private", "allowed_exits": "gw1", "target": "x"}
    out = views._strip_mt_task_fields(dict(d))
    assert "allowed_exits" not in out, "MT-off responses must stay byte-identical to upstream"
    assert "tenant_id" not in out and "visibility" not in out
    assert out["id"] == 1 and out["target"] == "x"


def test_strip_mt_task_fields_preserves_allowed_exits_when_mt_is_on(mt_enabled):
    d = {"id": 1, "tenant_id": 3, "visibility": "private", "allowed_exits": "gw1"}
    out = views._strip_mt_task_fields(dict(d))
    assert out["allowed_exits"] == "gw1"


# ----------------------------------------------------------------------------------- F8 ----
@pytest.mark.django_db
def test_tenant_exits_list_honours_the_api_gate(monkeypatch):
    """Mirrors exit_nodes_list: with [list_exitnodes] disabled the endpoint must refuse, not enumerate
    the caller's exits. It previously had no gate of any kind, so it stayed reachable on an install
    where the operator had deliberately turned exit-node enumeration off."""
    monkeypatch.setattr(views, "apiconf", SimpleNamespace(list_exitnodes={"enabled": False}))
    user = User.objects.create_user("gatetest")
    req = APIRequestFactory().get("/apiv2/tenant_exits/")
    force_authenticate(req, user=user)

    resp = views.tenant_exits_list(req)
    assert resp.data["error"] is True
    assert "disabled" in resp.data["error_value"].lower()
    assert "data" not in resp.data


@pytest.mark.django_db
def test_tenant_exits_list_still_works_when_enabled(monkeypatch):
    monkeypatch.setattr(views, "apiconf", SimpleNamespace(list_exitnodes={"enabled": True}))
    monkeypatch.setattr(views, "allowed_exit_slugs", lambda viewer: {"gw1", "gwGlobal"})
    user = User.objects.create_user("gatetest2")
    req = APIRequestFactory().get("/apiv2/tenant_exits/")
    force_authenticate(req, user=user)

    resp = views.tenant_exits_list(req)
    assert resp.data["error"] is False
    assert resp.data["data"] == ["gw1", "gwGlobal"]


# ---------------------------------------------------------------------------------- F13 ----
@pytest.mark.django_db
def test_tasks_create_static_stamps_allowed_exits(monkeypatch):
    """The static endpoint left allowed_exits NULL. NULL means UNRESTRICTED at the worker, and the row
    is an ordinary task that can be rescheduled as a dynamic one -- demux passes route=None, which the
    worker resolves to its node default. Every sibling endpoint stamps it; this one did not."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    captured = {}

    def _fake_demux(path, **kw):
        captured.update(kw)
        return [7], {}

    # views.db is a lazy Database proxy that raises CuckooDatabaseInitializationError on ANY
    # attribute access until init_database() runs, so swap the whole proxy rather than patching
    # an attribute on it.
    monkeypatch.setattr(views, "db", SimpleNamespace(demux_sample_and_add_to_db=_fake_demux))
    monkeypatch.setattr(views, "allowed_exit_slugs", lambda viewer: {"gw1", "gwGlobal"})
    monkeypatch.setattr(views, "submission_scope", lambda req: (3, "private"))
    monkeypatch.setattr(views, "store_temp_file", lambda content, name: "/tmp/static-acl-test")
    monkeypatch.setattr(
        views,
        "apiconf",
        SimpleNamespace(staticextraction={"enabled": True}, filecreate={"status": False}, api={"url": ""}),
    )

    user = User.objects.create_user("statictest")
    req = APIRequestFactory().post(
        "/apiv2/tasks/create/static/",
        {"file": SimpleUploadedFile("s.bin", b"MZ"), "options": "", "priority": "1"},
    )
    force_authenticate(req, user=user)

    views.tasks_create_static(req)
    assert captured.get("allowed_exits") == "gw1,gwGlobal"


@pytest.mark.django_db
def test_tasks_create_static_leaves_null_for_an_unrestricted_caller(monkeypatch):
    """Counter-check: an unrestricted caller (MT off/shared/local-admin) must still stamp NULL, not
    the empty string -- "" is the deny-all stamp and would strip network from every static task."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    captured = {}

    def _fake_demux(path, **kw):
        captured.update(kw)
        return [7], {}

    # views.db is a lazy Database proxy that raises CuckooDatabaseInitializationError on ANY
    # attribute access until init_database() runs, so swap the whole proxy rather than patching
    # an attribute on it.
    monkeypatch.setattr(views, "db", SimpleNamespace(demux_sample_and_add_to_db=_fake_demux))
    monkeypatch.setattr(views, "allowed_exit_slugs", lambda viewer: None)
    monkeypatch.setattr(views, "submission_scope", lambda req: (None, "public"))
    monkeypatch.setattr(views, "store_temp_file", lambda content, name: "/tmp/static-acl-test")
    monkeypatch.setattr(
        views,
        "apiconf",
        SimpleNamespace(staticextraction={"enabled": True}, filecreate={"status": False}, api={"url": ""}),
    )

    user = User.objects.create_user("statictest2")
    req = APIRequestFactory().post(
        "/apiv2/tasks/create/static/", {"file": SimpleUploadedFile("s.bin", b"MZ"), "options": ""}
    )
    force_authenticate(req, user=user)

    views.tasks_create_static(req)
    assert "allowed_exits" in captured and captured["allowed_exits"] is None


# ------------------------------------- control-plane relay of the exit ACL (central mode) ----
# In central mode the broker re-submits a UI task to a worker whose Django auth DB is empty and
# whose apiv2 is AllowAny. viewer_for() there is anonymous+tenantless, so allowed_exit_slugs()
# returns DENY-ALL -- correct for a real anonymous caller, fatal for the relay: every central
# submission would be refused with error=True/HTTP 200, which the dispatcher's raise_for_status
# cannot detect. The relay forwards the stamp the central UI computed instead, trusted ONLY behind
# the [api] control_plane_token.
_CP_SECRET = "cp-secret-for-tests"


def _cp_request(monkeypatch, token=None, body=None):
    monkeypatch.setattr(views, "apiconf", SimpleNamespace(api={"control_plane_token": _CP_SECRET}))
    req = APIRequestFactory().post("/apiv2/tasks/create/url/", body or {})
    if token is not None:
        req.META["HTTP_AUTHORIZATION"] = f"token {token}"
    # DRF Request wrapper so .data works the way the endpoints use it
    from rest_framework.request import Request
    from rest_framework.parsers import FormParser, MultiPartParser

    return Request(req, parsers=[FormParser(), MultiPartParser()])


def test_relay_stamp_is_honoured_with_a_valid_token(monkeypatch):
    r = _cp_request(monkeypatch, token=_CP_SECRET, body={"allowed_exits": "gw1,gwGlobal"})
    allowed, csv = views._submission_exit_acl(r)
    assert allowed == {"gw1", "gwGlobal"}
    assert csv == "gw1,gwGlobal"


def test_relay_deny_all_stamp_survives_the_round_trip(monkeypatch):
    """A tenant with zero exits stamps "" (deny-all). It must NOT collapse to None/unrestricted."""
    r = _cp_request(monkeypatch, token=_CP_SECRET, body={"allowed_exits": ""})
    allowed, csv = views._submission_exit_acl(r)
    assert allowed == set() and csv == ""


def test_an_unauthenticated_caller_cannot_self_assert_an_acl(monkeypatch):
    """THE new trust surface. The worker's apiv2 is AllowAny, so if a bare allowed_exits field were
    honoured, any caller could widen its own ACL and bypass the whole feature. Without a valid
    token the field must be ignored and the ACL derived from the caller's own viewer."""
    monkeypatch.setattr(views, "allowed_exit_slugs", lambda viewer: {"only-mine"})
    r = _cp_request(monkeypatch, token=None, body={"allowed_exits": "gw-i-should-not-have"})
    allowed, csv = views._submission_exit_acl(r)
    assert allowed == {"only-mine"} and csv == "only-mine"


def test_a_wrong_token_cannot_self_assert_an_acl(monkeypatch):
    monkeypatch.setattr(views, "allowed_exit_slugs", lambda viewer: {"only-mine"})
    r = _cp_request(monkeypatch, token="not-the-secret", body={"allowed_exits": "gw-nope"})
    allowed, _ = views._submission_exit_acl(r)
    assert allowed == {"only-mine"}


def test_an_empty_configured_secret_disables_the_relay_path(monkeypatch):
    """FAIL-CLOSED: an unset [api] control_plane_token must never match a presented empty bearer."""
    monkeypatch.setattr(views, "apiconf", SimpleNamespace(api={"control_plane_token": ""}))
    monkeypatch.setattr(views, "allowed_exit_slugs", lambda viewer: {"only-mine"})
    req = APIRequestFactory().post("/apiv2/tasks/create/url/", {"allowed_exits": "gw-nope"})
    req.META["HTTP_AUTHORIZATION"] = "token "
    from rest_framework.request import Request
    from rest_framework.parsers import FormParser, MultiPartParser

    allowed, _ = views._submission_exit_acl(Request(req, parsers=[FormParser(), MultiPartParser()]))
    assert allowed == {"only-mine"}
    assert views._control_plane_authenticated(Request(req, parsers=[FormParser()])) is False


def _mt(monkeypatch, enabled=True, mode="locked"):
    """Point views' multitenancy_config binding at a chosen config."""
    from lib.cuckoo.common.tenancy import MTConfig

    cfg = MTConfig(enabled=enabled, mode=mode, default_visibility="",
                   local_admins_manage_all_tenants=True)
    monkeypatch.setattr(views, "multitenancy_config", lambda: cfg)


def test_relay_without_a_stamp_DENIES_on_a_locked_node(monkeypatch):
    """INVERTED from test_relay_without_a_stamp_is_unrestricted_for_rolling_upgrades, which asserted
    (None, None).

    That was a fail-OPEN arm: a bridge/worker skew where the bridge forwards no allowed_exits handed
    the task UNRESTRICTED egress, and the only thing standing between that and a silent cross-tenant
    leak was a deploy-time check documented in another repo's runbook. On an MT-enabled, LOCKED node
    an absent stamp now means deny-all -- the tenant's tasks blackhole, loudly, instead of quietly
    getting everything."""
    _mt(monkeypatch, enabled=True, mode="locked")
    r = _cp_request(monkeypatch, token=_CP_SECRET, body={})
    allowed, csv = views._submission_exit_acl(r)
    assert allowed == set(), "absent stamp on a locked node must be deny-all, not unrestricted"
    assert csv == "", "deny-all must stamp the empty CSV, not NULL (NULL == unrestricted)"


def test_relay_without_a_stamp_is_unrestricted_when_mt_is_off(monkeypatch):
    """The ACL only exists under MT-locked. A single-tenant / shared install must be untouched."""
    for enabled, mode in ((False, "shared"), (True, "shared")):
        _mt(monkeypatch, enabled=enabled, mode=mode)
        r = _cp_request(monkeypatch, token=_CP_SECRET, body={})
        allowed, csv = views._submission_exit_acl(r)
        assert allowed is None and csv is None, f"enabled={enabled} mode={mode} must stay unrestricted"


def test_the_rolling_upgrade_tolerance_is_off_by_default_and_opt_in(monkeypatch):
    """The old fail-open behaviour survives ONLY behind an explicit, default-OFF [api] key, so an
    operator mid-upgrade opts in knowingly and can revert."""
    _mt(monkeypatch, enabled=True, mode="locked")

    # default: key absent => tolerance OFF => deny
    r = _cp_request(monkeypatch, token=_CP_SECRET, body={})
    assert views._cp_missing_exit_acl_tolerated() is False
    assert views._submission_exit_acl(r)[0] == set()

    # opt in => the old unrestricted behaviour returns
    monkeypatch.setattr(
        views, "apiconf",
        SimpleNamespace(api={"control_plane_token": _CP_SECRET,
                             views._CP_EXIT_ACL_TOLERANCE_KEY: True}))
    assert views._cp_missing_exit_acl_tolerated() is True
    req = APIRequestFactory().post("/apiv2/tasks/create/url/", {})
    req.META["HTTP_AUTHORIZATION"] = f"token {_CP_SECRET}"
    # MultiPartParser is required, not optional: APIRequestFactory().post defaults to multipart
    # encoding, so a FormParser-only Request raises UnsupportedMediaType on .data. Mirrors the
    # parser list _cp_request uses.
    from rest_framework.parsers import FormParser, MultiPartParser
    from rest_framework.request import Request

    allowed, csv = views._submission_exit_acl(Request(req, parsers=[FormParser(), MultiPartParser()]))
    assert allowed is None and csv is None, "the opt-in must restore unrestricted, not something else"


def test_a_forwarded_stamp_still_wins_over_the_deny_default(monkeypatch):
    """The deny default applies only to an ABSENT stamp; a real forwarded stamp is still honoured."""
    _mt(monkeypatch, enabled=True, mode="locked")
    r = _cp_request(monkeypatch, token=_CP_SECRET, body={"allowed_exits": "gw0"})
    allowed, csv = views._submission_exit_acl(r)
    assert allowed == {"gw0"} and csv == "gw0"


def test_anonymous_locked_worker_would_deny_without_the_relay(monkeypatch):
    """The regression this whole mechanism exists to prevent: prove the un-relayed path really is
    deny-all for an anonymous caller on an MT-enabled+locked node, so nobody 'simplifies' the relay
    away later."""
    from lib.cuckoo.common.tenancy import exit_route_permitted

    monkeypatch.setattr(views, "apiconf", SimpleNamespace(api={"control_plane_token": _CP_SECRET}))
    monkeypatch.setattr(views, "allowed_exit_slugs", lambda viewer: set())  # locked + tenantless
    req = APIRequestFactory().post("/apiv2/tasks/create/url/", {})
    from rest_framework.request import Request
    from rest_framework.parsers import FormParser

    allowed, _ = views._submission_exit_acl(Request(req, parsers=[FormParser()]))
    assert allowed == set()
    # ...and vpn0 (what the dispatcher sends today) would be refused outright
    assert exit_route_permitted("vpn0", allowed) is False
