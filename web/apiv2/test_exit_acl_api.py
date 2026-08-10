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

    monkeypatch.setattr(views.db, "demux_sample_and_add_to_db", _fake_demux)
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

    monkeypatch.setattr(views.db, "demux_sample_and_add_to_db", _fake_demux)
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
