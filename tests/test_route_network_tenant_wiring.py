"""Wiring test for the FORK tenant exit guard INSIDE AnalysisManager.route_network().

test_tenant_exit_guard.py covers _tenant_scope_nexthop as a pure function; this drives the REAL
route_network() so the integration is pinned: the getattr(self.task, "allowed_exits", None) read,
the `from lib.cuckoo.core.rooter import gateways, _gw_live` import, the positional arg order, the
assignment of the guard result back to self.route, and a "drop" flowing into the none/drop/false
fail-closed dispatch (never reaching _resolve_nexthop). A transposed/no-op wiring that fails OPEN
would pass every pure-helper test but fail here."""
import types

import lib.cuckoo.core.analysis_manager as am
import lib.cuckoo.core.rooter as rooter_mod

_MISSING = object()


class _Prof:
    def __init__(self, name):
        self.name = name
        self.interface = "ens6"
        self.rt_table = "201"
        self.priority = 0


def _routing():
    return type(
        "R",
        (),
        {
            "nexthop": type("NH", (), {"enabled": True, "default_policy": "roundrobin"})(),
            "routing": type("RT", (), {"route": "none"})(),
        },
    )()


def _drive(monkeypatch, task_route, allowed_exits, live):
    """Build a minimal AnalysisManager and run the REAL route_network() once. `live` is the set of
    gateway ids _gw_live returns True for. Returns (m, rooter_calls, resolve_calls)."""
    gws = {"gw1": _Prof("gw1"), "gw2": _Prof("gw2"), "gwG": _Prof("gwG")}
    monkeypatch.setattr(rooter_mod, "gateways", gws, raising=False)
    monkeypatch.setattr(rooter_mod, "_gw_live", lambda prof: prof.name in live, raising=False)
    monkeypatch.setattr(am, "Config", lambda name: _routing(), raising=False)
    monkeypatch.setattr(am, "vpns", {}, raising=False)
    calls = []
    monkeypatch.setattr(am, "rooter", lambda cmd, *a, **k: calls.append(cmd) or {}, raising=False)

    m = am.AnalysisManager.__new__(am.AnalysisManager)
    m.route = m.interface = m.rt_table = None
    m.nexthop_id = m.nexthop_interface = m.nexthop_rt_table = m.nexthop_priority = None
    m.no_local_routing = m.reject_segments = m.reject_hostports = None
    m.rooter_response = {}
    m.socks5s = {}
    m.log = types.SimpleNamespace(warning=lambda *a, **k: None, error=lambda *a, **k: None, info=lambda *a, **k: None)
    m.machine = types.SimpleNamespace(ip="192.168.100.42", interface="virbr0", resultserver_port=2042)
    if allowed_exits is _MISSING:
        m.task = types.SimpleNamespace(id=1, route=task_route)   # no allowed_exits attr at all
    else:
        m.task = types.SimpleNamespace(id=1, route=task_route, allowed_exits=allowed_exits)
    resolved = []
    monkeypatch.setattr(m, "_resolve_nexthop", lambda routing: resolved.append(True), raising=False)
    m.route_network()
    return m, calls, resolved


def test_foreign_gateway_drops(monkeypatch):
    m, calls, resolved = _drive(monkeypatch, "gw2", "gw1,gwG", live={"gw1", "gw2", "gwG"})
    assert m.route == "drop"          # guard rewrote the foreign exit
    assert resolved == []             # _resolve_nexthop NEVER reached
    assert "drop_enable" in calls     # fail-closed dispatch ran


def test_own_gateway_reaches_resolve(monkeypatch):
    m, calls, resolved = _drive(monkeypatch, "gw1", "gw1,gwG", live={"gw1", "gwG"})
    assert m.route == "gw1"           # kept unchanged
    assert resolved == [True]         # flows into _resolve_nexthop to bind
    assert "drop_enable" not in calls


def test_pool_with_no_live_allowed_drops(monkeypatch):
    m, calls, resolved = _drive(monkeypatch, "nexthop", "gw1", live=set())  # gw1 allowed but not live
    assert m.route == "drop"
    assert resolved == []
    assert "drop_enable" in calls


def test_unrestricted_passthrough(monkeypatch):
    m, calls, resolved = _drive(monkeypatch, "gw2", None, live={"gw1", "gw2", "gwG"})
    assert m.route == "gw2"           # allowed_exits None => no gating
    assert resolved == [True]


def test_missing_allowed_exits_attr_passthrough(monkeypatch):
    m, calls, resolved = _drive(monkeypatch, "gw2", _MISSING, live={"gw1", "gw2", "gwG"})
    assert m.route == "gw2"           # getattr default None => passthrough (legacy tasks safe)
    assert resolved == [True]
