"""Regression guards for the fail-OPEN gaps an xhigh review found in the tenant egress-exit ACL.

Every test here FAILS on the pre-fix code. They are deliberately separate from
test_tenant_exit_guard.py: that file only ever exercised "gw2 is not in my set -> drop", which is
why none of these bypasses were caught.

Pure-function tests against _tenant_scope_nexthop / validate_and_scope_route -- no DB, no Django
bootstrap, so they run in the `tests/` (--import-mode=append) tier.
"""
import pytest

from lib.cuckoo.common.tenancy import NO_EGRESS_ROUTES
from lib.cuckoo.core.analysis_manager import _tenant_scope_nexthop

GATEWAYS = {"gw1": "10.0.1.1", "gw2": "10.0.2.1"}
ALL_LIVE = lambda g: True  # noqa: E731


# ---------------------------------------------------------------- F3: legacy-route bypass ----
@pytest.mark.parametrize("route", ["internet", "tor", "vpn0", "socks5", "tun3", "all_exitnodes"])
def test_real_egress_legacy_routes_are_denied_under_a_restricted_acl(route):
    """THE bypass: a tenant locked to gw1 picking route=internet egressed from the worker's shared
    public IP, outside its assigned exit -- one dropdown click. Pre-fix this arm was
    `return route  # legacy route -- never gated`."""
    assert _tenant_scope_nexthop(route, "gw1", GATEWAYS, ALL_LIVE) == "drop"


@pytest.mark.parametrize("route", sorted(NO_EGRESS_ROUTES))
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


# ------------------------------------------- unspecified route: the submit/worker tier split ----
# route defaults to "" (parse_request_arguments), which means "no preference", NOT "no network". The
# two tiers must treat it DIFFERENTLY, and getting this wrong breaks in opposite directions:
#   * submit: refusing "" would reject the ordinary no-route submission of every restricted tenant.
#   * worker: permitting "" would let the task reach route_network's terminal else, which issues no
#     rooter command at all and leaves the guest on the host's default forwarding (full egress) --
#     the same shape as the F2 bypass.
def test_unspecified_route_is_accepted_at_submit():
    assert _validate("", {"gw1"}) == ""


def test_unspecified_route_drops_at_the_worker():
    assert _tenant_scope_nexthop("", "gw1", GATEWAYS, ALL_LIVE) == "drop"


def test_worker_evaluates_the_resolved_node_default_not_the_empty_string():
    """Why deferring at submit is still fail-closed: route_network resolves "" to the node default
    BEFORE calling the guard, so a node whose routing.conf default is `internet` drops for a
    restricted tenant rather than silently egressing."""
    assert _tenant_scope_nexthop("internet", "gw1", GATEWAYS, ALL_LIVE) == "drop"


# ------------------------------------------------------ the shared policy is ONE definition ----
def test_both_tiers_consult_the_same_predicate():
    """The submit tier and the worker each used to carry their own copy of the route policy behind a
    KEEP IN SYNC comment, and they had drifted -- that drift IS the F3 bypass. Assert they agree on
    the whole interesting surface rather than trusting the comment."""
    from lib.cuckoo.common.tenancy import exit_route_permitted

    allowed = {"gw1"}
    for route in ["gw1", "gw2", "internet", "tor", "vpn0", "socks5", "nexthop", "none", "inetsim", "bogus"]:
        permitted = exit_route_permitted(route, allowed)
        try:
            _validate(route, allowed)
            submit_ok = True
        except ValueError:
            submit_ok = False
        worker_ok = _tenant_scope_nexthop(route, "gw1", GATEWAYS, ALL_LIVE) != "drop"
        assert submit_ok == permitted, f"submit tier disagrees with the policy on {route!r}"
        assert worker_ok == permitted, f"worker tier disagrees with the policy on {route!r}"


def test_parse_allowed_exits_distinguishes_none_from_empty():
    """None (no ACL applies) and "" (a tenant with zero exits => deny-all) must not collapse."""
    from lib.cuckoo.common.tenancy import parse_allowed_exits

    assert parse_allowed_exits(None) is None
    assert parse_allowed_exits("") == set()
    assert parse_allowed_exits(" gw1 , gw2 ") == {"gw1", "gw2"}


def test_facade_denies_a_stamped_task_when_the_mt_layer_is_absent():
    """Skewed deploy: a task stamped with allowed_exits by an MT-enabled UI reaches an MT-free
    worker. With no policy module we cannot evaluate the stamp, so deny -- never treat the absence
    of the ACL implementation as absence of the ACL."""
    import sys
    from unittest import mock

    from lib.cuckoo.common import tenancy_optional as facade

    with mock.patch.dict(sys.modules, {"lib.cuckoo.common.tenancy": None}):
        assert facade.exit_route_permitted("internet", {"gw1"}) is False
        assert facade.exit_route_permitted("internet", set()) is False
        # MT genuinely absent => nothing is ever stamped => allowed is None => unrestricted.
        assert facade.exit_route_permitted("internet", None) is True


# ------------------------------------------ F11: download_file decides its OWN route ----
def test_download_file_rejects_a_randomly_INVENTED_vpn_route(monkeypatch):
    """F11. download_file re-parses the route from the request and, when none was given, INVENTS one
    from random_vpn/random_socks5. Neither value is the one the caller validated, so a restricted
    tenant that submitted no route acquired a real-egress VPN route on any node with random_vpn on --
    past every upstream check. The choke point has to re-validate what it actually decided."""
    from types import SimpleNamespace

    from lib.cuckoo.common import web_utils

    args = [""] * 18
    args[16] = ""  # route unspecified -> the random-route block fires
    monkeypatch.setattr(web_utils, "parse_request_arguments", lambda *a, **k: tuple(args))
    monkeypatch.setattr(web_utils, "_load_socks5_operational", lambda: {})
    monkeypatch.setattr(web_utils, "vpns", {"vpn-uk-residential": object()})
    monkeypatch.setattr(
        web_utils,
        "routing_conf",
        SimpleNamespace(vpn=SimpleNamespace(random_vpn=True), socks5=SimpleNamespace(random_socks5=False)),
    )

    status, detail = web_utils.download_file(request=object(), errors=[], service="unit-test", allowed_exits="gw1")
    assert status == "error"
    assert "not permitted" in detail["error"]


def test_download_file_lets_an_invented_route_through_when_it_is_allowed(monkeypatch):
    """Counter-check: the guard must not reject a randomly-chosen route the tenant DOES hold, and
    must not fire at all for an unrestricted tenant."""
    from types import SimpleNamespace

    from lib.cuckoo.common import web_utils

    args = [""] * 18
    args[16] = ""
    monkeypatch.setattr(web_utils, "parse_request_arguments", lambda *a, **k: tuple(args))
    monkeypatch.setattr(web_utils, "_load_socks5_operational", lambda: {})
    monkeypatch.setattr(web_utils, "vpns", {"vpn-uk-residential": object()})
    monkeypatch.setattr(
        web_utils,
        "routing_conf",
        SimpleNamespace(vpn=SimpleNamespace(random_vpn=True), socks5=SimpleNamespace(random_socks5=False)),
    )

    for allowed in ("vpn-uk-residential", None):
        status, detail = web_utils.download_file(request=object(), errors=[], service="unit-test", allowed_exits=allowed)
        # download_file goes on to fail for unrelated reasons (no content, no url); all we assert is
        # that it did NOT fail on the exit ACL.
        assert "not permitted for this tenant" not in str(detail), f"ACL wrongly fired for {allowed!r}"


# ------------------------------------ F4: the legacy distributed relay stripped the stamp ----
class _FakeTask:
    def __init__(self, options="", allowed_exits=None, id=1):
        self.options = options
        self.allowed_exits = allowed_exits
        self.id = id


def _mt(enabled):
    from types import SimpleNamespace
    from unittest import mock

    return mock.patch(
        "lib.cuckoo.core.analysis_manager.multitenancy_config",
        return_value=SimpleNamespace(enabled=enabled, mode="locked"),
    )


def test_dist_relay_task_without_a_stamp_denies_egress_when_mt_is_on():
    """utils/dist.py forwards no tenant context, so the worker's row is created by the RELAY's api
    principal -- normally an admin, whose allowed_exit_slugs is None -> UNRESTRICTED. The docs list
    dist as fail-closed, but that only ever covered report visibility; egress failed OPEN."""
    from lib.cuckoo.core.analysis_manager import _task_allowed_exits

    task = _FakeTask(options="main_task_id=4321,route=internet")
    with _mt(True):
        assert _task_allowed_exits(task) == ""  # deny-all
    assert _tenant_scope_nexthop("internet", "", GATEWAYS, ALL_LIVE) == "drop"


def test_dist_relay_task_is_untouched_when_mt_is_off():
    """Non-MT distributed installs (i.e. every upstream one) must keep today's behaviour exactly --
    this fix must not silently kill egress for them."""
    from lib.cuckoo.core.analysis_manager import _task_allowed_exits

    with _mt(False):
        assert _task_allowed_exits(_FakeTask(options="main_task_id=4321")) is None


def test_a_real_stamp_always_wins_over_the_dist_fallback():
    from lib.cuckoo.core.analysis_manager import _task_allowed_exits

    with _mt(True):
        assert _task_allowed_exits(_FakeTask(options="main_task_id=9", allowed_exits="gw1")) == "gw1"
        # A NON-relay task with no stamp stays unrestricted (MT off/shared/local-admin submissions).
        assert _task_allowed_exits(_FakeTask(options="route=internet")) is None


def test_dist_relay_detection_does_not_match_a_lookalike_option():
    """main_task_id= must be matched as a whole option, not a substring: an option merely CONTAINING
    the text (e.g. a custom field) must not flip an ordinary task into the deny-all fallback."""
    from lib.cuckoo.core.analysis_manager import _is_dist_relay_task

    assert _is_dist_relay_task(_FakeTask(options="main_task_id=7")) is True
    assert _is_dist_relay_task(_FakeTask(options="foo=1,main_task_id=7,bar=2")) is True
    assert _is_dist_relay_task(_FakeTask(options="not_main_task_id=7")) is False
    assert _is_dist_relay_task(_FakeTask(options="")) is False
    assert _is_dist_relay_task(_FakeTask(options={"main_task_id": 7})) is True


# ---------------------------------------------- F14: a denied route must not drop SILENTLY ----
@pytest.mark.parametrize(
    "route,allowed,gateways",
    [
        ("internet", "gw1", GATEWAYS),        # policy-denied
        ("gw2", "gw1", GATEWAYS),             # foreign gateway configured here
        ("gwGlobal", "gwGlobal", {}),         # assigned exit NOT configured on this worker
        ("nexthop", "gw1", GATEWAYS),         # pool with no live allowed gateway (live_filter False)
    ],
)
def test_every_acl_drop_logs_why(route, allowed, gateways, caplog):
    """An operator debugging "the analysis had no network" previously got nothing in the log: the
    guard returned 'drop' silently and the cause (foreign exit / unconfigured gateway / dead gateway)
    was indistinguishable from an ordinary route=none run."""
    live = (lambda g: False) if route == "nexthop" else ALL_LIVE
    with caplog.at_level("WARNING", logger="lib.cuckoo.core.analysis_manager"):
        assert _tenant_scope_nexthop(route, allowed, gateways, live) == "drop"
    assert any("exit ACL" in r.getMessage() for r in caplog.records), \
        f"no diagnostic logged for a denied {route!r}"


def test_a_permitted_route_logs_nothing(caplog):
    with caplog.at_level("WARNING", logger="lib.cuckoo.core.analysis_manager"):
        assert _tenant_scope_nexthop("gw1", "gw1", GATEWAYS, ALL_LIVE) == "gw1"
        assert _tenant_scope_nexthop("none", "gw1", GATEWAYS, ALL_LIVE) == "none"
    assert not [r for r in caplog.records if "exit ACL" in r.getMessage()]
