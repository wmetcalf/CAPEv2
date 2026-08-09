import pytest

from submission.views import validate_and_scope_route

# The gateway-slug universe. Passed explicitly so these stay hermetic unit tests (no DB): in the
# real web process it defaults to the active Exit slugs (rooter.gateways is empty in the web tier).
_KNOWN = {"gw1", "gw2", "gwGlobal"}


def test_cross_tenant_route_rejected():
    # allowed set = {gw1, gwGlobal}; requesting gw2 (another tenant's gateway) must raise
    with pytest.raises(ValueError):
        validate_and_scope_route("gw2", {"gw1", "gwGlobal"}, known_slugs=_KNOWN)


def test_own_and_global_and_pool_ok():
    assert validate_and_scope_route("gw1", {"gw1", "gwGlobal"}, known_slugs=_KNOWN) == "gw1"
    assert validate_and_scope_route("gwGlobal", {"gw1", "gwGlobal"}, known_slugs=_KNOWN) == "gwGlobal"
    assert validate_and_scope_route("nexthop", {"gw1"}, known_slugs=_KNOWN) == "nexthop"     # pool ok when tenant has >=1 exit
    assert validate_and_scope_route("roundrobin", {"gw1"}, known_slugs=_KNOWN) == "roundrobin"


def test_pool_with_empty_allowed_set_rejected():
    with pytest.raises(ValueError):
        validate_and_scope_route("nexthop", set(), known_slugs=_KNOWN)


def test_unrestricted_passes_through():
    # allowed is None (MT off/shared) => any route accepted, legacy routes untouched
    assert validate_and_scope_route("gw2", None, known_slugs=_KNOWN) == "gw2"
    assert validate_and_scope_route("vpn0", None, known_slugs=_KNOWN) == "vpn0"


def test_non_nexthop_routes_untouched():
    # a legacy route (vpn/tor/none) is never gated by exit ACLs even when restricted
    assert validate_and_scope_route("tor", {"gw1"}, known_slugs=_KNOWN) == "tor"
    assert validate_and_scope_route("none", {"gw1"}, known_slugs=_KNOWN) == "none"


@pytest.mark.django_db
def test_default_known_slugs_from_db():
    # Exercise the DEFAULT known_slugs path (_known_gateway_slugs -> Exit table union), the one that
    # actually runs in production when index() calls the validator without an explicit universe.
    from users.models import Exit

    Exit.objects.create(slug="gw1", active=True)
    Exit.objects.create(slug="gw2", active=True)
    Exit.objects.create(slug="gwOld", active=False)
    with pytest.raises(ValueError):
        validate_and_scope_route("gw2", {"gw1"})        # foreign active exit rejected via the DB universe
    with pytest.raises(ValueError):
        validate_and_scope_route("gwOld", {"gw1"})      # deactivated exit is STILL gated (fail-closed)
    assert validate_and_scope_route("gw1", {"gw1"}) == "gw1"
    assert validate_and_scope_route("nonexit-legacy-route", {"gw1"}) == "nonexit-legacy-route"
