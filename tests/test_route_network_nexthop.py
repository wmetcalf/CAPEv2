# Copyright (C) 2010-2013 Claudio Guarnieri.
# Copyright (C) 2014-2016 Cuckoo Foundation.
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.
"""Unit tests for the per-task [nexthop] resolve+bind branch in
AnalysisManager.route_network / unroute_network.

We drive the extracted helpers (_resolve_nexthop / _dispatch_nexthop /
_unroute_nexthop) through a small `_route` shim so the branch is testable
without standing up the full route_network() (DB, Config, machinery)."""
import pytest

import lib.cuckoo.core.analysis_manager as am


@pytest.fixture
def mgr(monkeypatch):
    """A minimal AnalysisManager with a recording rooter and a fake machine/route."""
    m = am.AnalysisManager.__new__(am.AnalysisManager)
    m.interface = m.rt_table = m.route = None
    m.nexthop_id = m.nexthop_interface = m.nexthop_rt_table = m.nexthop_priority = None
    m.no_local_routing = m.reject_segments = m.reject_hostports = None
    m.rooter_response = ""

    class _Mach:
        ip = "192.168.100.42"
        interface = "virbr0"

    m.machine = _Mach()
    calls = []
    monkeypatch.setattr(am, "rooter", lambda cmd, *a, **k: calls.append((cmd, a)) or {}, raising=False)
    m._calls = calls
    return m


def _fake_routing(default_policy="roundrobin", default_route="nexthop"):
    """Minimal stand-in for Config('routing') exposing .nexthop.* and .routing.route
    (the configured default route — _resolve_nexthop only falls back to default_policy
    when self.route equals it; a typo'd explicit route drops instead)."""
    return type(
        "R",
        (),
        {
            "nexthop": type("NH", (), {"enabled": True, "default_policy": default_policy})(),
            "routing": type("RT", (), {"route": default_route})(),
        },
    )()


def _route(mgr, route, nexthop_enabled, default_policy="roundrobin", default_route="nexthop"):
    """Replicate route_network's resolution + dispatch for the nexthop branch only.

    `nexthop_enabled` is the gate (mirrors _nexthop_enabled(routing)); when False the
    branch is never entered so the legacy paths are byte-for-byte unchanged.
    """
    routing = _fake_routing(default_policy, default_route)
    mgr.route = route
    # resolution chain (only the nexthop branch is modeled here; a False return
    # forces route="drop" and skips the branch body, exactly like route_network).
    if nexthop_enabled and mgr._resolve_nexthop(routing):
        pass
    # dispatch chain: forced-drop is handled by the existing none/drop/false dispatch
    if str(mgr.route).lower() in ("none", "drop", "false"):
        am.rooter("drop_enable", mgr.machine.ip, "2042")
    mgr._dispatch_nexthop()


def test_explicit_gateway_binds_and_skips_generic(mgr, monkeypatch):
    prof = type("P", (), {"name": "gw1", "interface": "ens6", "rt_table": "201", "priority": 0})()
    monkeypatch.setattr(am, "_select_gateway", lambda r: prof if r == "gw1" else None, raising=False)
    monkeypatch.setattr(am, "gateways", {"gw1": prof}, raising=False)
    _route(mgr, route="gw1", nexthop_enabled=True)
    # bound to the profile, generic forward/srcroute NOT used
    assert mgr.interface is None  # generic block skipped (H2 option b)
    assert mgr.rt_table is None  # so the srcroute_enable elif is skipped too
    assert mgr.nexthop_id == "gw1"
    assert mgr.nexthop_interface == "ens6"
    assert mgr.nexthop_rt_table == "201"
    assert mgr.nexthop_priority == "10042"  # 10000 + last octet
    cmds = [c for c, _ in mgr._calls]
    assert "nexthop_enable" in cmds
    assert "forward_enable" not in cmds and "srcroute_enable" not in cmds
    assert "drop_enable" not in cmds


def test_typo_gateway_fails_closed_to_drop(mgr, monkeypatch):
    monkeypatch.setattr(am, "_select_gateway", lambda r: None, raising=False)
    monkeypatch.setattr(am, "gateways", {}, raising=False)
    _route(mgr, route="gw9", nexthop_enabled=True)
    cmds = [c for c, _ in mgr._calls]
    assert "drop_enable" in cmds  # review B1: never fall through to no-op
    assert "nexthop_enable" not in cmds
    assert mgr.route == "drop"
    assert mgr.nexthop_id is None
    assert mgr.interface is None  # not left on host default forwarding


def test_typod_route_drops_even_when_a_gateway_is_live(mgr, monkeypatch):
    # gemini #14 HIGH: a typo'd/foreign route (e.g. "vpn9") that is NOT a gateway id, NOT a
    # policy token, and NOT the configured default route must DROP — it must never fall back
    # to default_policy and silently egress via a live gateway (isolation-boundary bypass).
    live = type("P", (), {"name": "gw1", "interface": "ens6", "rt_table": "201", "priority": 0})()
    # _select_gateway WOULD return a live profile if reached — the fix must not reach it.
    monkeypatch.setattr(am, "_select_gateway", lambda r: live, raising=False)
    monkeypatch.setattr(am, "gateways", {"gw1": live}, raising=False)
    _route(mgr, route="vpn9", nexthop_enabled=True, default_route="nexthop")
    cmds = [c for c, _ in mgr._calls]
    assert "drop_enable" in cmds and "nexthop_enable" not in cmds
    assert mgr.route == "drop" and mgr.nexthop_id is None


def test_configured_default_route_uses_default_policy(mgr, monkeypatch):
    # When the task uses the node's CONFIGURED default route (self.route == routing.routing.route),
    # fall back to default_policy (roundrobin/random) and pick from the live pool.
    prof = type("P", (), {"name": "gw2", "interface": "ens7", "rt_table": "202", "priority": 0})()
    monkeypatch.setattr(am, "_select_gateway", lambda r: prof if r in ("roundrobin", "random") else None, raising=False)
    monkeypatch.setattr(am, "gateways", {"gw2": prof}, raising=False)
    _route(mgr, route="nexthop", nexthop_enabled=True, default_policy="roundrobin", default_route="nexthop")
    cmds = [c for c, _ in mgr._calls]
    assert "nexthop_enable" in cmds and "drop_enable" not in cmds
    assert mgr.nexthop_id == "gw2"


def test_reserved_route_never_resolves_to_gateway(mgr, monkeypatch):
    # gemini #15 (security-critical): a reserved route (none/drop/false/internet/tor/inetsim) must
    # DROP, never resolve to a gateway — even if it is the node's configured default route.
    live = type("P", (), {"name": "gw1", "interface": "ens6", "rt_table": "201", "priority": 0})()
    monkeypatch.setattr(am, "_select_gateway", lambda r: live, raising=False)  # would bind if reached
    monkeypatch.setattr(am, "gateways", {"gw1": live}, raising=False)
    _route(mgr, route="internet", nexthop_enabled=True, default_route="internet")
    cmds = [c for c, _ in mgr._calls]
    assert "drop_enable" in cmds and "nexthop_enable" not in cmds
    assert mgr.route == "drop" and mgr.nexthop_id is None


def test_all_nexthop_args_are_str(mgr, monkeypatch):
    prof = type("P", (), {"name": "gw1", "interface": "ens6", "rt_table": "201", "priority": 0})()
    monkeypatch.setattr(am, "_select_gateway", lambda r: prof, raising=False)
    monkeypatch.setattr(am, "gateways", {"gw1": prof}, raising=False)
    _route(mgr, route="gw1", nexthop_enabled=True)
    found = False
    for cmd, args in mgr._calls:
        if cmd == "nexthop_enable":
            found = True
            assert all(isinstance(a, str) for a in args)
    assert found


def test_no_regress_vpn_and_none(mgr):
    # nexthop DISABLED -> _resolve_nexthop must never run; route=vpn0/none unchanged.
    # vpn0 path
    _route(mgr, route="vpn0", nexthop_enabled=False)
    assert mgr.nexthop_id is None
    cmds = [c for c, _ in mgr._calls]
    assert "nexthop_enable" not in cmds  # no nexthop calls leak into the legacy path
    # none path
    mgr._calls.clear()
    mgr.nexthop_id = None
    _route(mgr, route="none", nexthop_enabled=False)
    assert "nexthop_enable" not in [c for c, _ in mgr._calls]


def test_unroute_mirrors_persisted_tuple(mgr, monkeypatch):
    prof = type("P", (), {"name": "gw1", "interface": "ens6", "rt_table": "201", "priority": 0})()
    monkeypatch.setattr(am, "_select_gateway", lambda r: prof, raising=False)
    monkeypatch.setattr(am, "gateways", {"gw1": prof}, raising=False)
    _route(mgr, route="gw1", nexthop_enabled=True)
    enable_args = next(a for c, a in mgr._calls if c == "nexthop_enable")
    mgr._calls.clear()
    mgr._unroute_nexthop()  # extracted helper called from unroute_network
    disable_args = next(a for c, a in mgr._calls if c == "nexthop_disable")
    # disable deletes EXACTLY what enable created (vm_ip, interface, rt_table, priority).
    # The selector is NOT re-run: the persisted self.nexthop_* tuple is used (review M5).
    assert disable_args == ("192.168.100.42", "ens6", "201", "10042")
    assert disable_args == enable_args


def test_unroute_noop_when_not_nexthop(mgr):
    # no nexthop_id => _unroute_nexthop must issue nothing (legacy paths untouched,
    # and the generic disable block is skipped because self.interface is None).
    mgr.nexthop_id = None
    mgr._unroute_nexthop()
    assert [c for c, _ in mgr._calls if c == "nexthop_disable"] == []
