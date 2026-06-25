"""Tenancy-optional central scope shim (web/analysis/central_scope.py).

Verifies the three deployment states (MT layer absent / present+disabled / present+
enabled) collapse to the right scope, AND the security-critical FAIL-CLOSED contract:
when the MT layer IS deployed, a RUNTIME error in the scope/visibility resolution must
PROPAGATE — never be swallowed into a see-all result (which would silently bypass tenant
isolation). The shim catches ImportError ONLY (= MT layer not deployed); these tests lock
that down so a later broad-`except` regression is caught.
"""
import pytest

from analysis.central_scope import viewer_scope, viewer_can_view_sample


def test_viewer_scope_mt_layer_absent_is_see_all(monkeypatch):
    # Simulate the MT layer not being deployed: `from dashboard.views import
    # entitled_scope_filter` raises ImportError when the symbol is gone.
    monkeypatch.delattr("dashboard.views.entitled_scope_filter", raising=False)
    assert viewer_scope(object()) is None


def test_viewer_scope_delegates_when_mt_present(monkeypatch):
    sentinel = {"$or": [{"info.tenant_slug": "acme"}]}
    monkeypatch.setattr("dashboard.views.entitled_scope_filter", lambda user: sentinel)
    assert viewer_scope(object()) is sentinel


def test_viewer_scope_fail_closed_on_runtime_error(monkeypatch):
    # MT deployed but resolution blows up at runtime -> MUST propagate, not see-all.
    def boom(user):
        raise RuntimeError("scope resolution failed")

    monkeypatch.setattr("dashboard.views.entitled_scope_filter", boom)
    with pytest.raises(RuntimeError):
        viewer_scope(object())


def test_viewer_can_view_sample_mt_layer_absent_is_true(monkeypatch):
    monkeypatch.delattr("users.tenancy.can_view_sample", raising=False)
    assert viewer_can_view_sample(object(), sha256="abc") is True


def test_viewer_can_view_sample_delegates_when_mt_present(monkeypatch):
    seen = {}

    def fake(user, *, sha256=None, sha1=None, md5=None, sample_id=None):
        seen.update(sha256=sha256, sha1=sha1, md5=md5, sample_id=sample_id)
        return False

    monkeypatch.setattr("users.tenancy.can_view_sample", fake)
    assert viewer_can_view_sample(object(), sha256="deadbeef") is False
    assert seen == {"sha256": "deadbeef", "sha1": None, "md5": None, "sample_id": None}


def test_viewer_can_view_sample_fail_closed_on_runtime_error(monkeypatch):
    def boom(user, **kw):
        raise RuntimeError("visibility check failed")

    monkeypatch.setattr("users.tenancy.can_view_sample", boom)
    with pytest.raises(RuntimeError):
        viewer_can_view_sample(object(), sha256="abc")
