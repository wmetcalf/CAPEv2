import lib.cuckoo.core.analysis_manager as am


def _scope(route, allowed_csv, gateways, live):
    """Drive the fork guard: _tenant_scope_nexthop returns the (possibly rewritten) route."""
    return am._tenant_scope_nexthop(route, allowed_csv, gateways, live_filter=lambda g: g in live)


def test_explicit_foreign_gateway_drops():
    assert _scope("gw2", "gw1,gwG", {"gw1", "gw2", "gwG"}, {"gw1", "gw2", "gwG"}) == "drop"


def test_explicit_own_gateway_kept():
    assert _scope("gw1", "gw1,gwG", {"gw1", "gwG"}, {"gw1", "gwG"}) == "gw1"


def test_pool_picks_within_allowed_live():
    out = _scope("roundrobin", "gw1,gwG", {"gw1", "gwG", "gw2"}, {"gw1", "gwG", "gw2"})
    assert out in {"gw1", "gwG"}          # never gw2 (not allowed)


def test_pool_with_no_live_allowed_drops():
    assert _scope("nexthop", "gw1", {"gw1"}, set()) == "drop"   # gw1 allowed but not live


def test_unrestricted_passthrough():
    assert _scope("gw2", None, {"gw1", "gw2"}, {"gw1", "gw2"}) == "gw2"   # allowed_csv None => no gating


def test_legacy_route_untouched():
    assert _scope("tor", "gw1", {"gw1"}, {"gw1"}) == "tor"      # not a gateway/pool token
