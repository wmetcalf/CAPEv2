# tests/test_rooter_nexthop.py
import pytest
import utils.rooter as rooter


@pytest.fixture
def rec(monkeypatch):
    """Record every run()/run_iptables() invocation as a list of argv tuples.

    utils.rooter functions reference settings.ip / ServicePaths.ip (both the same
    path at runtime, defined only in __main__), so inject both for unit tests.
    """
    calls = {"run": [], "iptables": []}

    def fake_run(*args):
        calls["run"].append(tuple(str(a) for a in args))
        return ("", "")  # (stdout, stderr); real run() never raises

    def fake_run_iptables(*args, **kwargs):
        calls["iptables"].append(tuple(str(a) for a in args))
        return ("", "")

    class _Settings:
        ip = "ip"
    monkeypatch.setattr(rooter, "settings", _Settings, raising=False)
    monkeypatch.setattr(rooter.ServicePaths, "ip", "ip", raising=False)
    monkeypatch.setattr(rooter.ServicePaths, "iptables", "iptables", raising=False)
    monkeypatch.setattr(rooter, "run", fake_run)
    monkeypatch.setattr(rooter, "run_iptables", fake_run_iptables)
    return calls


def test_recorder_captures(rec):
    rooter.run("ip", "route", "show")
    assert rec["run"] == [("ip", "route", "show")]
