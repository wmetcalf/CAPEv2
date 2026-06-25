"""Unit-tests for the _mt_app_enabled() helper in web.settings.

These tests call the helper directly (monkeypatching os.environ) so they
never need to reload Django settings — making them fast and safe to run in
any environment that can import the helper without a full Django setup.
"""
import importlib


def _get_helper():
    """Return a fresh reference to _mt_app_enabled from web.settings.

    We import the module once; the helper itself re-reads os.environ at call
    time, so monkeypatching is sufficient — no reload required.
    """
    import web.settings as s
    return s._mt_app_enabled


def test_mt_app_enabled_by_default(monkeypatch):
    """Env unset -> MT app is enabled (default, unchanged behaviour)."""
    monkeypatch.delenv("CAPE_DISABLE_MT_APP", raising=False)
    assert _get_helper()() is True


def test_mt_app_disabled_by_1(monkeypatch):
    """CAPE_DISABLE_MT_APP=1 -> MT app disabled."""
    monkeypatch.setenv("CAPE_DISABLE_MT_APP", "1")
    assert _get_helper()() is False


def test_mt_app_disabled_by_true(monkeypatch):
    """CAPE_DISABLE_MT_APP=true -> MT app disabled."""
    monkeypatch.setenv("CAPE_DISABLE_MT_APP", "true")
    assert _get_helper()() is False


def test_mt_app_disabled_by_yes(monkeypatch):
    """CAPE_DISABLE_MT_APP=yes -> MT app disabled."""
    monkeypatch.setenv("CAPE_DISABLE_MT_APP", "yes")
    assert _get_helper()() is False


def test_mt_app_enabled_by_zero(monkeypatch):
    """CAPE_DISABLE_MT_APP=0 -> MT app enabled (only 1/true/yes disable it)."""
    monkeypatch.setenv("CAPE_DISABLE_MT_APP", "0")
    assert _get_helper()() is True


def test_mt_app_enabled_by_empty(monkeypatch):
    """CAPE_DISABLE_MT_APP='' -> MT app enabled."""
    monkeypatch.setenv("CAPE_DISABLE_MT_APP", "")
    assert _get_helper()() is True
