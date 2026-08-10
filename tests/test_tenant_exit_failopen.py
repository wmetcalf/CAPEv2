"""Regression guards for the fail-OPEN gaps an xhigh review found in the tenant egress-exit ACL.

Every test here FAILS on the pre-fix code. They are deliberately separate from
test_tenant_exit_guard.py: that file only ever exercised "gw2 is not in my set -> drop", which is
why none of these bypasses were caught.

Pure-function tests against _tenant_scope_nexthop / validate_and_scope_route -- no DB, no Django
bootstrap, so they run in the `tests/` (--import-mode=append) tier.
"""
import pytest

from lib.cuckoo.core.analysis_manager import _NO_EGRESS_ROUTES, _tenant_scope_nexthop

GATEWAYS = {"gw1": "10.0.1.1", "gw2": "10.0.2.1"}
ALL_LIVE = lambda g: True  # noqa: E731


# ---------------------------------------------------------------- F3: legacy-route bypass ----
@pytest.mark.parametrize("route", ["internet", "tor", "vpn0", "socks5", "tun3", "all_exitnodes"])
def test_real_egress_legacy_routes_are_denied_under_a_restricted_acl(route):
    """THE bypass: a tenant locked to gw1 picking route=internet egressed from the worker's shared
    public IP, outside its assigned exit -- one dropdown click. Pre-fix this arm was
    `return route  # legacy route -- never gated`."""
    assert _tenant_scope_nexthop(route, "gw1", GATEWAYS, ALL_LIVE) == "drop"


@pytest.mark.parametrize("route", sorted(_NO_EGRESS_ROUTES))
def test_no_egress_routes_still_permitted_under_a_restricted_acl(route):
    """The counter-check: denying real egress must NOT deny the dispositions that egress nowhere.
    A tenant must always be able to submit route=none, and inetsim/fakenet is simulated."""
    assert _tenant_scope_nexthop(route, "gw1", GATEWAYS, ALL_LIVE) == route


def test_unrestricted_acl_still_passes_legacy_routes_through():
    """MT off / shared / local-admin => allowed_csv is None => no gating at all (upstream parity)."""
    assert _tenant_scope_nexthop("internet", None, GATEWAYS, ALL_LIVE) == "internet"
    assert _tenant_scope_nexthop("vpn0", None, GATEWAYS, ALL_LIVE) == "vpn0"


# ------------------------------------------------- F2: guard must apply with nexthop disabled ----
def test_gateway_route_drops_when_no_gateways_are_configured():
    """Models a [nexthop]=off worker: rooter.gateways is empty. The ACL must still deny, because the
    caller no longer gates the guard on _nexthop_enabled. Pre-fix the guard was skipped entirely and
    route=gwA fell through route_network's terminal else, which issues NO rooter command -- the guest
    kept the host's default forwarding (full egress)."""
    assert _tenant_scope_nexthop("gw1", "gw1", {}, ALL_LIVE) == "drop"


def test_empty_allowed_csv_denies_everything_but_no_egress():
    """The deny-all stamp ("" from a tenant with no exits, or from the fail-closed facade arm)."""
    assert _tenant_scope_nexthop("gw1", "", GATEWAYS, ALL_LIVE) == "drop"
    assert _tenant_scope_nexthop("internet", "", GATEWAYS, ALL_LIVE) == "drop"
    assert _tenant_scope_nexthop("none", "", GATEWAYS, ALL_LIVE) == "none"


def test_pool_token_with_no_live_allowed_gateway_drops():
    assert _tenant_scope_nexthop("nexthop", "gw1", GATEWAYS, lambda g: False) == "drop"


# ------------------------------------------- F3/F12/F15: submit-side validator, deny-by-default ----
def _validate(route, allowed):
    from submission.views import validate_and_scope_route

    return validate_and_scope_route(route, allowed)


@pytest.mark.parametrize("route", ["internet", "tor", "vpn0", "socks5"])
def test_submit_rejects_real_egress_legacy_routes(route):
    with pytest.raises(ValueError):
        _validate(route, {"gw1"})


def test_submit_permits_no_egress_and_own_exits():
    assert _validate("none", {"gw1"}) == "none"
    assert _validate("inetsim", {"gw1"}) == "inetsim"
    assert _validate("gw1", {"gw1"}) == "gw1"


def test_submit_error_does_not_leak_whether_the_exit_exists():
    """F15 oracle: a real-but-foreign slug and a nonexistent one must be indistinguishable. Pre-fix
    the former raised "exit 'globex-uk-residential' is not permitted" while the latter was ACCEPTED,
    letting any tenant dictionary-walk the whole Exit table."""
    with pytest.raises(ValueError) as foreign:
        _validate("globex-uk-residential", {"gw1"})
    with pytest.raises(ValueError) as absent:
        _validate("globex-uk-resxdential", {"gw1"})
    assert str(foreign.value) == str(absent.value)
    assert "globex" not in str(foreign.value)


def test_submit_unrestricted_is_a_noop():
    assert _validate("internet", None) == "internet"
