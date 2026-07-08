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


def test_nexthop_init_skips_reserved_table(rec):
    # codex P1 / gemini critical: load_nexthop_profiles feeds each [gwX] rt_table straight into
    # nexthop_init's flush at startup, so a misconfigured reserved table (main/254/...) must NOT
    # be flushed — doing so would wipe the host's own routing. nexthop_teardown already guards
    # this set; the init path must too. No ip command runs at all for a reserved table.
    for bad in ("main", "local", "default", "254", "255", "253", "0"):
        rec["run"].clear()
        rooter.nexthop_init(bad, "ens6", "onlink")
        assert rec["run"] == [], f"nexthop_init flushed reserved table {bad!r}: {rec['run']}"


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
    # The forward ACCEPT goes into CAPE_ACCEPTED_SEGMENTS (jumped first in FORWARD), NOT a tail
    # `-A FORWARD` rule — otherwise a libvirt default `-i virbr* -j REJECT` in front of it would
    # shadow the accept on libvirt-backed guests (codex P1).
    assert rec["iptables"] == [
        ("-t", "nat", "-A", "POSTROUTING", "-s", "192.168.100.42", "-o", "ens6", "-j", "MASQUERADE"),
        ("-I", "CAPE_ACCEPTED_SEGMENTS", "-s", "192.168.100.42", "-o", "ens6", "-j", "ACCEPT"),
    ]
    # never a raw tail FORWARD append (regression guard for the shadowing bug)
    assert not any(a[:2] == ("-A", "FORWARD") for a in rec["iptables"])


# ---------------------------------------------------------------------------
# Task 3: nexthop_disable
# ---------------------------------------------------------------------------

def test_nexthop_disable_argv(rec):
    rooter.nexthop_disable("192.168.100.42", "ens6", "201", "10042")
    assert rec["run"] == [
        ("ip", "rule", "del", "from", "192.168.100.42", "lookup", "201", "priority", "10042"),
        ("conntrack", "-D", "-s", "192.168.100.42"),
    ]
    # mirror-delete from the same chain enable inserted into (CAPE_ACCEPTED_SEGMENTS)
    assert rec["iptables"] == [
        ("-t", "nat", "-D", "POSTROUTING", "-s", "192.168.100.42", "-o", "ens6", "-j", "MASQUERADE"),
        ("-D", "CAPE_ACCEPTED_SEGMENTS", "-s", "192.168.100.42", "-o", "ens6", "-j", "ACCEPT"),
    ]


# ---------------------------------------------------------------------------
# Task 4: nexthop_fail_closed_enable
# ---------------------------------------------------------------------------

def test_nexthop_fail_closed_argv(rec):
    rooter.nexthop_fail_closed_enable("192.168.100.0/24", "250", "30000")
    assert rec["run"] == [
        ("ip", "route", "replace", "blackhole", "default", "table", "250"),
        # intra-subnet exception (priority just above the blackhole) so host<->guest
        # (agent init + ResultServer) and guest<->guest stay on main and aren't dropped.
        ("ip", "rule", "del", "from", "192.168.100.0/24", "to", "192.168.100.0/24", "lookup", "main", "priority", "29999"),
        ("ip", "rule", "add", "from", "192.168.100.0/24", "to", "192.168.100.0/24", "lookup", "main", "priority", "29999"),
        ("ip", "rule", "del", "from", "192.168.100.0/24", "lookup", "250", "priority", "30000"),
        ("ip", "rule", "add", "from", "192.168.100.0/24", "lookup", "250", "priority", "30000"),
    ]


# ---------------------------------------------------------------------------
# Task 5: nexthop_teardown + nexthop_configure
# ---------------------------------------------------------------------------

def test_nexthop_teardown_sweeps_policy_routing(rec, monkeypatch):
    # Make `ip rule show` return two in-band per-task rules + one out-of-band rule.
    def fake_run(*args):
        rec["run"].append(tuple(str(a) for a in args))
        if args[:3] == ("ip", "rule", "show"):
            return ("10042: from 192.168.100.42 lookup 201\n"
                    "10043: from 192.168.100.43 lookup 202\n"
                    "32766: from all lookup main\n", "")
        return ("", "")
    monkeypatch.setattr(rooter, "run", fake_run)

    rooter.nexthop_teardown("201,202", "192.168.100.0/24", "250", "30000", "10000", "10255")

    assert ("ip", "route", "flush", "table", "201") in rec["run"]
    assert ("ip", "route", "flush", "table", "202") in rec["run"]
    assert ("ip", "route", "del", "blackhole", "default", "table", "250") in rec["run"]
    assert ("ip", "rule", "del", "from", "192.168.100.0/24", "lookup", "250", "priority", "30000") in rec["run"]
    # intra-subnet exception rule also removed on teardown
    assert ("ip", "rule", "del", "from", "192.168.100.0/24", "to", "192.168.100.0/24", "lookup", "main", "priority", "29999") in rec["run"]
    # in-band per-task rules swept; the 32766 main rule untouched
    assert ("ip", "rule", "del", "priority", "10042") in rec["run"]
    assert ("ip", "rule", "del", "priority", "10043") in rec["run"]
    assert ("ip", "rule", "del", "priority", "32766") not in rec["run"]


def test_nexthop_teardown_skips_reserved_tables(rec):
    # gemini #14 HIGH: even if a gateway profile is misconfigured with a reserved/system table
    # id, teardown must NOT flush it — doing so (at startup + SIGTERM) would wipe the host's own
    # routing and take the box offline. The real gateway table IS still flushed.
    rooter.nexthop_teardown("main,254,201", "192.168.100.0/24", "250", "30000", "10000", "10255")
    assert ("ip", "route", "flush", "table", "201") in rec["run"]
    assert ("ip", "route", "flush", "table", "main") not in rec["run"]
    assert ("ip", "route", "flush", "table", "254") not in rec["run"]


def test_nexthop_configure_sets_globals(rec):
    rooter.nexthop_configure("201,202", "192.168.100.0/24", "250", "30000", "10000", "10255")
    assert rooter.GATEWAY_TABLES_CSV == "201,202"
    assert rooter.NEXTHOP_VM_NET == "192.168.100.0/24"
    assert rooter.NEXTHOP_FAIL_TABLE == "250"
    assert rooter.NEXTHOP_PRIORITY_LOW == "30000"
    assert rooter.NEXTHOP_BAND_LO == "10000"
    assert rooter.NEXTHOP_BAND_HI == "10255"
