import builtins
import pytest
from lib.cuckoo.common import tenancy_optional as topt


def _hide(monkeypatch, modname):
    real_import = builtins.__import__
    def fake(name, *a, **k):
        if name == modname or name.startswith(modname + "."):
            raise ImportError(f"simulated-absent: {modname}")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)


def test_lib_facade_present_delegates(monkeypatch):
    # MT present: multitenancy_config() returns the real config object (has .enabled)
    cfg = topt.multitenancy_config()
    assert hasattr(cfg, "enabled")


def test_lib_facade_absent_is_see_all(monkeypatch):
    _hide(monkeypatch, "lib.cuckoo.common.tenancy")
    assert topt.multitenancy_config().enabled is False
    assert topt.viewer_for(object()).is_local_admin is True
    assert topt.scope_match("acme", topt.viewer_for(object())) is None
