"""Security tests for the direct-VNC console (upstream #3076 feat-vncclient), hardened
for multi-tenancy. The direct console addresses VMs by NAME / host:port and mints a
task_id=0 session the websocket consumer does NOT tenant-check, so it bypasses task-scoped
access by design — it must be restricted to operators when MT is on (codex P1 / task #172)."""
import ast
import re

import pytest
from django.contrib.auth.models import User


def _func_src(name):
    import guac.views as v

    text = open(v.__file__).read()
    lines = text.splitlines()
    for node in ast.parse(text).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            return "\n".join(lines[start - 1: node.end_lineno])
    return None


def _direct_vnc_views():
    import guac.urls

    text = open(guac.urls.__file__).read()
    return sorted(set(re.findall(r"views\.(direct_vnc_\w+)", text)))


# ── SECURITY GATE: every by-name/host direct-VNC view must be operator-gated ──
@pytest.mark.parametrize("name", _direct_vnc_views())
def test_direct_vnc_view_is_operator_gated(name):
    """Every direct_vnc_* view (routed by vm_name/host:port, which the task_id coverage
    gate can't see) must consult _vnc_console_denied_reason — a new endpoint that only
    checked is_vnc_console_enabled() would re-open the cross-tenant console hole."""
    src = _func_src(name)
    assert src is not None, f"guac.views.{name} not found"
    assert "_vnc_console_denied_reason" in src, (
        f"guac.views.{name} routes by VM name/host but doesn't call "
        f"_vnc_console_denied_reason — direct-console cross-tenant risk (#172)"
    )


def test_direct_vnc_views_exist():
    # guard against the route names silently changing out from under the gate above
    assert "direct_vnc_vm" in _direct_vnc_views()


@pytest.mark.parametrize("name", ["direct_vnc_vm", "direct_vnc_vm_start"])
def test_active_analysis_guard_covers_get_and_post(name):
    """codex P1: both the GET console (direct_vnc_vm) and the POST start/revert
    (direct_vnc_vm_start) must refuse a VM hosting a live analysis — and direct_vnc_vm must
    do it BEFORE the running/not-running branch (else a momentarily not-running busy VM falls
    through to the start page). Assert both reference _vm_has_active_analysis."""
    src = _func_src(name)
    assert src is not None, f"guac.views.{name} not found"
    assert "_vm_has_active_analysis" in src, (
        f"guac.views.{name} must call _vm_has_active_analysis — direct console must not touch "
        f"a VM owned by a live analysis (codex P1 / #172)"
    )


# ── BEHAVIOURAL: the operator gate itself ──
class _Req:
    def __init__(self, user):
        self.user = user


@pytest.mark.django_db
def test_vnc_console_denied_when_disabled(monkeypatch):
    import guac.views as v

    monkeypatch.setattr(v, "is_vnc_console_enabled", lambda: False)
    assert v._vnc_console_denied_reason(_Req(User.objects.create_superuser("r", "r@x.com", "x"))) is not None


@pytest.mark.django_db
def test_vnc_console_allowed_single_node(monkeypatch):
    """MT disabled (default/single-node): the console works for any authenticated user —
    is_local_admin is True for everyone, so the operator gate is a no-op (back-compat)."""
    import guac.views as v
    from lib.cuckoo.common.tenancy import MTConfig
    import users.tenancy as ut

    monkeypatch.setattr(v, "is_vnc_console_enabled", lambda: True)
    monkeypatch.setattr(ut, "multitenancy_config", lambda: MTConfig(False, "shared", "", True))
    member = User.objects.create_user("vm", "vm@x.com", "x")
    assert v._vnc_console_denied_reason(_Req(member)) is None


@pytest.mark.django_db
def test_vnc_console_operator_only_when_mt_enabled(monkeypatch, mt_enabled):
    """MT enabled: a tenant member is DENIED the direct console; a break-glass operator
    (local-admin) is allowed. This is the codex-P1 / #172 fix — without it any tenant could
    drive another tenant's VM (or a live analysis) by name."""
    import guac.views as v

    monkeypatch.setattr(v, "is_vnc_console_enabled", lambda: True)
    member = User.objects.create_user("vm2", "vm2@x.com", "x")          # tenant-less member
    operator = User.objects.create_superuser("vroot", "vroot@x.com", "x")  # break-glass (mt_enabled: local_admins=True)
    assert v._vnc_console_denied_reason(_Req(member)) is not None       # denied
    assert v._vnc_console_denied_reason(_Req(operator)) is None          # operator allowed


@pytest.mark.django_db
def test_vnc_console_no_request_user_does_not_crash(monkeypatch):
    """codex P2 / #172: when served by the standalone Guacamole ASGI app (web.guac_settings
    has no AuthenticationMiddleware), the request has no .user. The gate must not
    AttributeError; in single-node (MT off) it stays a no-op (allowed)."""
    import guac.views as v
    from lib.cuckoo.common.tenancy import MTConfig
    import users.tenancy as ut

    monkeypatch.setattr(v, "is_vnc_console_enabled", lambda: True)
    monkeypatch.setattr(ut, "multitenancy_config", lambda: MTConfig(False, "shared", "", True))

    class NoUserReq:  # request object with NO .user attribute (standalone guac app)
        pass

    assert v._vnc_console_denied_reason(NoUserReq()) is None  # MT off -> allowed, no crash


def test_active_analysis_detected_without_machine_lock(cape_db):
    """codex High / #172: the active-analysis guard MUST key off the active task/guest, NOT
    machine.locked — CAPE marks a task TASK_RUNNING before it locks the machine, so a .locked
    gate races (a live analysis runs while still unlocked). An active Guest for a VM whose
    machine is NOT locked must still be detected. (Semantic check — codex noted the structural
    grep above wouldn't catch a guard that stayed present but keyed off the wrong thing.)"""
    import guac.views as v
    from lib.cuckoo.core.database import Database
    from lib.cuckoo.core.data.guests import Guest

    db = Database()
    assert v._vm_has_active_analysis("vm-idle") is False  # no analysis -> direct console allowed

    # a task + an ACTIVE guest for 'vm-live' — NO machine lock involved anywhere
    tid = db.add_url("http://example.com/")
    sess = db.session()
    g = Guest(name="vm-live", label="vm-live", platform="windows", manager="kvm", task_id=tid)
    g.status = "running"  # non-null; the guard keys off label + shutdown_on, not status
    sess.add(g)
    sess.commit()
    assert v._vm_has_active_analysis("vm-live") is True   # detected via active Guest, lock irrelevant
