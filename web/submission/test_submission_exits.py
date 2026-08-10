"""Submit-tier tenant egress-exit ACL (submission.views.validate_and_scope_route).

The validator is DENY-BY-DEFAULT: under a restricted ACL the permitted universe is exactly the
tenant's own exit slugs + the pool tokens + _NO_EGRESS_ROUTES. Everything else raises, whether or
not it names a real Exit row.

These assertions were rewritten when the xhigh review found the original
`route in known_slugs and route not in allowed_slugs` formulation fail-open (real-egress legacy
routes like internet/tor/vpn0 were never in known_slugs, so they sailed past the ACL) and an
existence oracle. The old file pinned that behaviour as CORRECT -- see
test_non_nexthop_routes_untouched below, now inverted -- which is why the suite went green on a
bypassable ACL. The gateway-universe parameter (and the _known_gateway_slugs helper that computed
it) no longer exists: deny-by-default needs no universe.
"""
import pytest

from submission.views import _NO_EGRESS_ROUTES, validate_and_scope_route


def test_cross_tenant_route_rejected():
    # allowed set = {gw1, gwGlobal}; requesting gw2 (another tenant's gateway) must raise
    with pytest.raises(ValueError):
        validate_and_scope_route("gw2", {"gw1", "gwGlobal"})


def test_own_and_global_and_pool_ok():
    assert validate_and_scope_route("gw1", {"gw1", "gwGlobal"}) == "gw1"
    assert validate_and_scope_route("gwGlobal", {"gw1", "gwGlobal"}) == "gwGlobal"
    assert validate_and_scope_route("nexthop", {"gw1"}) == "nexthop"  # pool ok when tenant has >=1 exit
    assert validate_and_scope_route("roundrobin", {"gw1"}) == "roundrobin"


def test_pool_with_empty_allowed_set_rejected():
    with pytest.raises(ValueError):
        validate_and_scope_route("nexthop", set())


def test_unrestricted_passes_through():
    # allowed is None (MT off/shared/local-admin) => any route accepted, legacy routes untouched
    assert validate_and_scope_route("gw2", None) == "gw2"
    assert validate_and_scope_route("vpn0", None) == "vpn0"


@pytest.mark.parametrize("route", ["internet", "tor", "vpn0", "socks5", "tun3", "all_exitnodes"])
def test_real_egress_legacy_routes_are_gated(route):
    """INVERTED from the original test_non_nexthop_routes_untouched, which asserted
    `validate_and_scope_route("tor", {"gw1"}) == "tor"`. A tenant confined to gw1 that can pick
    route=tor/internet/vpn0 egresses outside its assigned exit -- the ACL meant nothing."""
    with pytest.raises(ValueError):
        validate_and_scope_route(route, {"gw1"})


@pytest.mark.parametrize("route", sorted(_NO_EGRESS_ROUTES))
def test_no_egress_routes_remain_permitted(route):
    """The half of the original assertion that was right: route=none et al egress nowhere, so
    gating them would just break submission for every restricted tenant."""
    assert validate_and_scope_route(route, {"gw1"}) == route


def test_unknown_slug_is_denied_not_accepted():
    """Replaces test_default_known_slugs_from_db, which needed the DB to decide whether a route was
    'known' (and passed an unknown one straight through: the old file asserted
    `validate_and_scope_route("nonexit-legacy-route", {"gw1"}) == "nonexit-legacy-route"`).
    Deny-by-default needs no DB at all, so this is hermetic."""
    with pytest.raises(ValueError):
        validate_and_scope_route("nonexit-legacy-route", {"gw1"})


def test_no_existence_oracle():
    """A real-but-foreign slug and a nonexistent one must be indistinguishable to the caller,
    otherwise the endpoint enumerates the Exit inventory (incl. other tenants' dedicated exits)."""
    with pytest.raises(ValueError) as foreign:
        validate_and_scope_route("gw2", {"gw1"})
    with pytest.raises(ValueError) as absent:
        validate_and_scope_route("gw2-does-not-exist", {"gw1"})
    assert str(foreign.value) == str(absent.value)
