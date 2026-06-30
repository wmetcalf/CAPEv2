# tests/test_nexthop_selection.py
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
