# tests/test_nexthop_selection.py
import pytest
import threading
import lib.cuckoo.core.rooter as core_rooter


class _Profile:
    def __init__(self, name, interface, rt_table, priority):
        self.name, self.interface, self.rt_table, self.priority = name, interface, rt_table, priority


def _seed(monkeypatch, live=("gw1", "gw2", "gw3")):
    gws = {n: _Profile(n, f"ens{6+i}", str(201 + i), 0) for i, n in enumerate(("gw1", "gw2", "gw3"))}
    monkeypatch.setattr(core_rooter, "gateways", gws, raising=False)
    monkeypatch.setattr(core_rooter, "_gw_cursor", 0, raising=False)
    monkeypatch.setattr(core_rooter, "_gw_live", lambda p: p.name in live)  # liveness shim
    return gws


def test_explicit_id_resolves(monkeypatch):
    _seed(monkeypatch)
    assert core_rooter._select_gateway("gw2").name == "gw2"


def test_explicit_id_down_fails_closed(monkeypatch):
    _seed(monkeypatch, live=("gw1", "gw3"))
    assert core_rooter._select_gateway("gw2") is None  # named-but-down => caller drops


def test_roundrobin_cycles_over_live(monkeypatch):
    _seed(monkeypatch, live=("gw1", "gw3"))  # gw2 down
    picks = [core_rooter._select_gateway("roundrobin").name for _ in range(4)]
    assert picks == ["gw1", "gw3", "gw1", "gw3"]


def test_empty_pool_fails_closed(monkeypatch):
    monkeypatch.setattr(core_rooter, "gateways", {}, raising=False)
    assert core_rooter._select_gateway("roundrobin") is None


def test_roundrobin_threadsafe(monkeypatch):
    _seed(monkeypatch)
    out = []
    lock = threading.Lock()

    def worker():
        p = core_rooter._select_gateway("roundrobin")
        with lock:
            out.append(p.name)
    threads = [threading.Thread(target=worker) for _ in range(300)]
    for t in threads: t.start()
    for t in threads: t.join()
    # even distribution across 3 live gateways, no crash
    assert all(out.count(n) == 100 for n in ("gw1", "gw2", "gw3"))


# ---------------------------------------------------------------------------
# _FakeRouting helper shared by T7 and T8 tests
# ---------------------------------------------------------------------------

class _FakeGw1:
    """Fake [gw1] section: rt_table is an int (as config.getint would produce)."""
    name = "gw1"
    interface = "ens6"
    next_hop = "onlink"
    rt_table = 201  # int — loader must coerce to str


class _FakeNexthop:
    def __init__(self, enabled=True, route="gw1"):
        self.enabled = enabled
        self.gateways = "gw1"
        self.default_policy = "roundrobin"
        self.fail_closed = True
        self.vm_net = "192.168.100.0/24"


class _FakeRoutingSection:
    """Fake top-level routing config section (routing.routing.route)."""
    def __init__(self, route="none"):
        self.route = route


class _FakeVpn:
    enabled = False


class _FakeRouting:
    """Minimal fake routing config for T7/T8 tests.

    Exposes:
      .nexthop  — _FakeNexthop (enabled/disabled, gateways="gw1")
      .gw1      — _FakeGw1 (interface, next_hop, rt_table as int)
      .routing  — .route
      .vpn      — .enabled = False
      .get(name) -> attribute named `name`
    """
    def __init__(self, nexthop_enabled=True, route="none"):
        self.nexthop = _FakeNexthop(enabled=nexthop_enabled, route=route)
        self.gw1 = _FakeGw1()
        self.routing = _FakeRoutingSection(route=route)
        self.vpn = _FakeVpn()

    def get(self, name):
        return getattr(self, name)


# ---------------------------------------------------------------------------
# T7: [gwX] loader — populates gateways global + coerces rt_table to str
# ---------------------------------------------------------------------------

def test_gwx_loader_populates_and_coerces(monkeypatch):
    import lib.cuckoo.core.startup as startup
    recorded = []
    # startup.gateways is the dict object the loader writes into (imported reference, same object
    # as core_rooter.gateways unless replaced).  Clear it in place so both refs see the reset.
    startup.gateways.clear()
    monkeypatch.setattr(startup, "rooter", lambda cmd, *a, **k: recorded.append((cmd, a)) or {}, raising=False)
    startup.load_nexthop_profiles(_FakeRouting())
    # Check via startup.gateways (the dict the function mutated)
    assert "gw1" in startup.gateways
    assert startup.gateways["gw1"].rt_table == "201"   # coerced to str
    assert ("nexthop_init", ("201", "ens6", "onlink")) in recorded
    assert any(c == "nexthop_fail_closed_enable" for c, _ in recorded)
    assert any(c == "nexthop_teardown" for c, _ in recorded)   # re-arm sweep before arm


# ---------------------------------------------------------------------------
# T8: validate_default_route — gateway route accepted; unknown raises
# ---------------------------------------------------------------------------

def test_nexthop_default_route_boots_without_vpn(monkeypatch):
    import lib.cuckoo.core.startup as startup
    # Seed gateways so "gw1" is known
    monkeypatch.setattr(core_rooter, "gateways", {"gw1": _FakeGw1()}, raising=False)
    # routing.route = "gw1", nexthop enabled, vpn disabled -> must NOT raise
    startup.validate_default_route(_FakeRouting(route="gw1"))


def test_unknown_gateway_default_route_raises(monkeypatch):
    import lib.cuckoo.core.startup as startup
    from lib.cuckoo.common.exceptions import CuckooStartupError
    # gateways is empty — "gw9" is unknown
    monkeypatch.setattr(core_rooter, "gateways", {}, raising=False)
    with pytest.raises(CuckooStartupError):
        startup.validate_default_route(_FakeRouting(route="gw9"))
