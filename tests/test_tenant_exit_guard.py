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


def test_real_egress_legacy_route_is_denied():
    """INVERTED (was test_legacy_route_untouched, asserting == "tor").

    "not a gateway/pool token" was the wrong question to ask. tor/internet/vpnX/socks5/tunN are all
    REAL egress from an address the tenant was never granted, so passing them through left the whole
    ACL bypassable by one dropdown change. Only no-egress dispositions stay permitted."""
    assert _scope("tor", "gw1", {"gw1"}, {"gw1"}) == "drop"
    assert _scope("internet", "gw1", {"gw1"}, {"gw1"}) == "drop"


def test_no_egress_dispositions_still_pass():
    """Counter-check to the above: routes that egress nowhere must NOT be gated, or a restricted
    tenant could not submit anything at all."""
    assert _scope("none", "gw1", {"gw1"}, {"gw1"}) == "none"
    assert _scope("inetsim", "gw1", {"gw1"}, {"gw1"}) == "inetsim"


def test_empty_csv_denies_all():
    # "" (empty CSV) == zero allowed exits => fail-closed DENY-ALL, distinct from None (unrestricted).
    # Guards against a future `if not allowed_csv: return route` regression flipping it to fail-open.
    assert _scope("gw1", "", {"gw1", "gwG"}, {"gw1", "gwG"}) == "drop"       # explicit gateway dropped
    assert _scope("nexthop", "", {"gw1", "gwG"}, {"gw1", "gwG"}) == "drop"   # pool token dropped


def test_whitespace_in_csv_tolerated():
    # a "gw1, gwG" style join (spaces after commas) must still match clean gateway ids
    assert _scope("gwG", "gw1, gwG", {"gw1", "gwG"}, {"gw1", "gwG"}) == "gwG"


def test_allowed_slug_not_local_drops():
    # a slug in the tenant's allowed set but NOT configured on THIS worker cannot be honored -> drop
    # (fail-closed; prevents a node-default escalation to the global pool via _resolve_nexthop)
    assert _scope("gwG", "gw1,gwG", {"gw1"}, {"gw1"}) == "drop"   # gwG allowed but absent locally


def test_legacy_route_not_in_allowed_is_denied():
    """INVERTED (was test_legacy_route_not_in_allowed_still_passes, asserting == "tor").

    Being "not a gateway slug and not in allowed" is precisely the condition that must DENY under a
    restricted ACL, not the condition that exempts a route from it. This assertion and
    test_real_egress_legacy_route_is_denied above are why the F3 bypass survived a green suite."""
    assert _scope("tor", "gw1,gwG", {"gw1"}, {"gw1"}) == "drop"
