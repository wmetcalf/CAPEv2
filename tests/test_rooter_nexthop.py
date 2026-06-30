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


# ---------------------------------------------------------------------------
# Task 1: nexthop_init
# ---------------------------------------------------------------------------

def test_nexthop_init_onlink(rec):
    rooter.nexthop_init("201", "ens6", "onlink")
    assert rec["run"] == [
        ("ip", "route", "flush", "table", "201"),
        ("ip", "route", "replace", "default", "dev", "ens6", "onlink", "table", "201"),
    ]


def test_nexthop_init_via(rec):
    rooter.nexthop_init("202", "ens7", "10.30.72.1")
    assert rec["run"] == [
        ("ip", "route", "flush", "table", "202"),
        ("ip", "route", "replace", "default", "via", "10.30.72.1", "dev", "ens7", "table", "202"),
    ]


# ---------------------------------------------------------------------------
# Task 2: nexthop_enable
# ---------------------------------------------------------------------------

def test_nexthop_enable_argv(rec):
    rooter.nexthop_enable("192.168.100.42", "ens6", "201", "10042")
    # iproute2 + conntrack go through run(); nat/filter through run_iptables()
    assert rec["run"] == [
        ("conntrack", "-D", "-s", "192.168.100.42"),                                  # pre-bind flush
        ("ip", "rule", "del", "from", "192.168.100.42", "lookup", "201", "priority", "10042"),  # idempotent pre-clean
        ("ip", "rule", "add", "from", "192.168.100.42", "lookup", "201", "priority", "10042"),
    ]
    assert rec["iptables"] == [
        ("-t", "nat", "-A", "POSTROUTING", "-s", "192.168.100.42", "-o", "ens6", "-j", "MASQUERADE"),
        ("-A", "FORWARD", "-s", "192.168.100.42", "-o", "ens6", "-j", "ACCEPT"),
    ]


# ---------------------------------------------------------------------------
# Task 3: nexthop_disable
# ---------------------------------------------------------------------------

def test_nexthop_disable_argv(rec):
    rooter.nexthop_disable("192.168.100.42", "ens6", "201", "10042")
    assert rec["run"] == [
        ("ip", "rule", "del", "from", "192.168.100.42", "lookup", "201", "priority", "10042"),
        ("conntrack", "-D", "-s", "192.168.100.42"),
    ]
    assert rec["iptables"] == [
        ("-t", "nat", "-D", "POSTROUTING", "-s", "192.168.100.42", "-o", "ens6", "-j", "MASQUERADE"),
        ("-D", "FORWARD", "-s", "192.168.100.42", "-o", "ens6", "-j", "ACCEPT"),
    ]


# ---------------------------------------------------------------------------
# Task 4: nexthop_fail_closed_enable
# ---------------------------------------------------------------------------

def test_nexthop_fail_closed_argv(rec):
    rooter.nexthop_fail_closed_enable("192.168.100.0/24", "250", "30000")
    assert rec["run"] == [
        ("ip", "route", "replace", "blackhole", "default", "table", "250"),
        ("ip", "rule", "del", "from", "192.168.100.0/24", "lookup", "250", "priority", "30000"),
        ("ip", "rule", "add", "from", "192.168.100.0/24", "lookup", "250", "priority", "30000"),
    ]
