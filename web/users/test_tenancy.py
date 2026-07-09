from types import SimpleNamespace
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.test import TestCase

from lib.cuckoo.common.tenancy import MTConfig
from users.tenancy import allowed_exit_slugs


@pytest.mark.django_db
def test_tenant_and_profile_fields():
    from users.models import Tenant, UserProfile

    t = Tenant.objects.create(
        slug="acme", name="Acme", idp_groups=["acme-soc"], admin_idp_groups=["acme-admins"]
    )
    u = User.objects.create_user("a", "a@acme.com", "x")
    prof = UserProfile.objects.get(user=u)  # auto-created by signal
    prof.tenant = t
    prof.is_tenant_admin = True
    prof.save()

    refreshed = UserProfile.objects.get(user=u)
    assert refreshed.tenant.slug == "acme"
    assert refreshed.is_tenant_admin is True


@pytest.mark.django_db
def test_resolve_tenant_from_groups():
    from users.models import Tenant, UserProfile
    from web.allauth_adapters import reconcile_tenant

    t = Tenant.objects.create(
        slug="acme", name="Acme", idp_groups=["acme-soc"], admin_idp_groups=["acme-admins"]
    )
    u = User.objects.create_user("a", "a@acme.com", "x")

    reconcile_tenant(u, {"acme-soc", "acme-admins"})
    p = UserProfile.objects.get(user=u)
    assert p.tenant_id == t.id and p.is_tenant_admin is True

    reconcile_tenant(u, {"acme-soc"})  # demoted from admin, still a member
    p.refresh_from_db()
    assert p.tenant_id == t.id and p.is_tenant_admin is False

    reconcile_tenant(u, set())  # no matching groups -> no tenant
    p.refresh_from_db()
    assert p.tenant_id is None and p.is_tenant_admin is False


@pytest.mark.django_db
def test_resolve_tenant_multi_match_fails_closed():
    from users.models import Tenant, UserProfile
    from web.allauth_adapters import reconcile_tenant

    Tenant.objects.create(slug="a", name="A", idp_groups=["shared-grp"])
    Tenant.objects.create(slug="b", name="B", idp_groups=["shared-grp"])
    u = User.objects.create_user("m", "m@x.com", "x")
    reconcile_tenant(u, {"shared-grp"})
    p = UserProfile.objects.get(user=u)
    assert p.tenant_id is None  # ambiguous -> fail closed


@pytest.mark.django_db
def test_viewer_for_maps_user(mt_enabled):
    from users.models import Tenant, UserProfile
    from users.tenancy import viewer_for

    t = Tenant.objects.create(slug="acme", name="Acme")
    u = User.objects.create_user("a", "a@acme.com", "x")
    p = UserProfile.objects.get(user=u)
    p.tenant = t
    p.is_tenant_admin = True
    p.save()

    # re-fetch so the user's cached userprofile reflects the saved tenant
    # (a real request loads request.user.userprofile fresh)
    fresh = User.objects.get(pk=u.pk)
    v = viewer_for(fresh)
    assert v.user_id == u.id
    assert v.tenant_id == t.id
    assert v.is_tenant_admin is True


@pytest.mark.django_db
def test_disabled_is_backcompat_see_all(monkeypatch):
    """H1 back-compat: with multitenancy OFF (the default / existing `sb`
    deployment), any authenticated user sees every task — including a legacy
    private job owned by someone else — exactly like today. The feature must be
    fully opt-in and must not retroactively hide existing rows."""
    from lib.cuckoo.common.tenancy import MTConfig
    from users.tenancy import can_view_task, viewer_for
    import users.tenancy as ut

    monkeypatch.setattr(ut, "multitenancy_config", lambda: MTConfig(False, "shared", "", True))

    nonowner = User.objects.create_user("n", "n@x.com", "x")

    class LegacyTask:  # owned by a different user, marked private
        user_id = 999
        tenant_id = None
        visibility = "private"

    v = viewer_for(nonowner)
    assert v.is_local_admin is True          # short-circuit to legacy see-all
    assert v.tenant_id is None
    assert can_view_task(nonowner, LegacyTask()) is True  # not hidden


@pytest.mark.django_db
def test_disabled_anonymous_is_backcompat_see_all(monkeypatch):
    """B0 back-compat (the headline gating regression): on a DISABLED install a
    no-auth public deployment (WEB_AUTHENTICATION off) or apiv2 with token-auth
    off (DRF AllowAny) serves every request as AnonymousUser. viewer_for MUST
    short-circuit the disabled case BEFORE the is_authenticated check so an
    anonymous viewer is is_local_admin=True and can_read a private-default task,
    exactly like upstream. Regression guard: previously the anon branch returned
    is_local_admin=False before reading cfg, so ~45 guards denied non-public
    tasks on a plain public install."""
    from django.contrib.auth.models import AnonymousUser
    from lib.cuckoo.common.tenancy import MTConfig
    from users.tenancy import can_view_task, can_manage_task, viewer_for
    import users.tenancy as ut

    monkeypatch.setattr(ut, "multitenancy_config", lambda: MTConfig(False, "shared", "", True))

    class PrivateTask:  # private-default, owned by someone (no anon owner)
        user_id = 999
        tenant_id = 10
        visibility = "private"

    anon = AnonymousUser()
    v = viewer_for(anon)
    assert v.is_local_admin is True          # disabled => see-all even for anon
    assert v.user_id is None
    assert can_view_task(anon, PrivateTask()) is True   # not hidden (upstream parity)
    assert can_manage_task(anon, PrivateTask()) is True # mutations also unblocked when disabled


@pytest.mark.django_db
def test_enabled_anonymous_stays_public_only(monkeypatch):
    """Counterpart to B0: when MT is ENABLED (locked), an anonymous viewer must
    remain public-only — the disabled short-circuit must NOT leak into enabled
    mode."""
    from django.contrib.auth.models import AnonymousUser
    from lib.cuckoo.common.tenancy import MTConfig
    from users.tenancy import can_view_task, viewer_for
    import users.tenancy as ut

    monkeypatch.setattr(ut, "multitenancy_config", lambda: MTConfig(True, "locked", "", True))

    class PrivateTask:
        user_id = 999
        tenant_id = 10
        visibility = "private"

    class PublicTask:
        user_id = 999
        tenant_id = 10
        visibility = "public"

    anon = AnonymousUser()
    v = viewer_for(anon)
    assert v.is_local_admin is False
    assert can_view_task(anon, PrivateTask()) is False  # enabled => restricted
    assert can_view_task(anon, PublicTask()) is True     # public still readable


@pytest.mark.django_db
def test_viewer_for_local_admin_gate(monkeypatch):
    from lib.cuckoo.common.tenancy import MTConfig
    import users.tenancy as ut

    u = User.objects.create_superuser("root", "root@x.com", "x")  # local superuser, no SocialAccount

    # flag ON -> local superuser is break-glass
    monkeypatch.setattr(ut, "multitenancy_config",
                        lambda: MTConfig(True, "locked", "", True))
    assert ut.viewer_for(u).is_local_admin is True

    # flag OFF -> local (non-IdP) superuser is NOT break-glass
    monkeypatch.setattr(ut, "multitenancy_config",
                        lambda: MTConfig(True, "locked", "", False))
    assert ut.viewer_for(u).is_local_admin is False

    # anonymous -> empty viewer
    from django.contrib.auth.models import AnonymousUser
    assert ut.viewer_for(AnonymousUser()).user_id is None


@pytest.mark.django_db
def test_submission_scope(mt_enabled, monkeypatch):
    import pytest as _pytest
    from lib.cuckoo.common.tenancy import MTConfig
    from users.models import Tenant, UserProfile
    import users.tenancy as ut

    t = Tenant.objects.create(slug="acme", name="Acme")
    u = User.objects.create_user("a", "a@x.com", "x")
    p = UserProfile.objects.get(user=u)
    p.tenant = t
    p.save()
    u = User.objects.get(pk=u.pk)

    class Req:
        pass

    # explicit visibility honoured + tenant from user
    r = Req()
    r.user = u
    r.data = {"visibility": "tenant"}
    assert ut.submission_scope(r) == (t.id, "tenant")

    # omitted -> per-mode default (shared -> public)
    monkeypatch.setattr(ut, "multitenancy_config", lambda: MTConfig(True, "shared", "", True))
    r2 = Req()
    r2.user = u
    r2.data = {}
    assert ut.submission_scope(r2)[1] == "public"

    # invalid -> ValueError (view turns this into a 400)
    r3 = Req()
    r3.user = u
    r3.data = {"visibility": "bogus"}
    with _pytest.raises(ValueError):
        ut.submission_scope(r3)


def _mk_member(name, tenant=None, admin=False):
    """Create a user + set their UserProfile tenant/admin, returning a FRESH user
    (so request.user.userprofile reflects the saved tenant, as a real request would)."""
    from users.models import UserProfile

    u = User.objects.create_user(name, f"{name}@x.com", "x")
    p = UserProfile.objects.get(user=u)  # auto-created by signal
    p.tenant = tenant
    p.is_tenant_admin = admin
    p.save()
    return User.objects.get(pk=u.pk)


@pytest.mark.django_db
def test_can_ban_user_tenant_admin_delegation(mt_enabled):
    """ban delegation predicate (users.tenancy.can_ban_user): global staff/superuser
    ban anyone; a tenant-admin bans ONLY a target in their OWN tenant; non-admins and
    cross-tenant/tenant-less targets are denied."""
    from users.models import Tenant
    from users.tenancy import can_ban_user

    acme = Tenant.objects.create(slug="acme", name="Acme")
    globex = Tenant.objects.create(slug="globex", name="Globex")

    superu = User.objects.create_superuser("root", "root@x.com", "x")
    staff = User.objects.create_user("staff", "staff@x.com", "x")
    staff.is_staff = True
    staff.save()
    staff = User.objects.get(pk=staff.pk)
    admin_acme = _mk_member("aadm", acme, admin=True)
    member_acme = _mk_member("amem", acme, admin=False)
    target_acme = _mk_member("atgt", acme)
    target_globex = _mk_member("gtgt", globex)
    target_tenantless = _mk_member("ntgt", None)

    # GLOBAL break-glass: staff/superuser ban across any tenant
    assert can_ban_user(superu, target_acme.id) is True
    assert can_ban_user(superu, target_globex.id) is True
    assert can_ban_user(staff, target_globex.id) is True

    # TENANT delegation: acme admin bans an acme target, NOT a globex/tenant-less one
    assert can_ban_user(admin_acme, target_acme.id) is True
    assert can_ban_user(admin_acme, target_globex.id) is False   # cross-tenant denied
    assert can_ban_user(admin_acme, target_tenantless.id) is False

    # non-admin member can't ban even within their own tenant
    assert can_ban_user(member_acme, target_acme.id) is False
    # nonexistent target -> denied (no crash)
    assert can_ban_user(admin_acme, 99999999) is False


@pytest.mark.django_db
def test_can_ban_user_disabled_is_staff_only(monkeypatch):
    """MT OFF (single-node): the gate stays exactly the legacy is_staff/is_superuser —
    NOT the see-all is_local_admin viewer_for returns when disabled (which would let any
    authenticated user ban). A plain member must NOT be able to ban when MT is off."""
    from lib.cuckoo.common.tenancy import MTConfig
    import users.tenancy as ut

    monkeypatch.setattr(ut, "multitenancy_config", lambda: MTConfig(False, "shared", "", True))
    staff = User.objects.create_user("s2", "s2@x.com", "x")
    staff.is_staff = True
    staff.save()
    member = User.objects.create_user("m2", "m2@x.com", "x")
    target = User.objects.create_user("t2", "t2@x.com", "x")
    assert ut.can_ban_user(User.objects.get(pk=staff.pk), target.id) is True
    assert ut.can_ban_user(member, target.id) is False  # MT-off does NOT grant ban to non-staff


@pytest.mark.django_db
def test_ban_user_view_is_tenant_scoped(mt_enabled, cape_db, client):
    """End-to-end via the ban_user view: an acme tenant-admin disables an acme user but
    is denied a globex user (who stays active). Catches a regression that re-broadened
    the gate back to is_staff-only or dropped the per-target tenant check."""
    from django.urls import reverse
    from users.models import Tenant

    acme = Tenant.objects.create(slug="acme", name="Acme")
    globex = Tenant.objects.create(slug="globex", name="Globex")
    admin_acme = _mk_member("vaadm", acme, admin=True)
    target_acme = _mk_member("vatgt", acme)
    target_globex = _mk_member("vgtgt", globex)

    client.force_login(admin_acme)
    # same-tenant ban -> target disabled
    client.get(reverse("ban_user", args=[target_acme.id]))
    target_acme.refresh_from_db()
    assert target_acme.is_active is False

    # cross-tenant ban -> denied, target stays active
    client.get(reverse("ban_user", args=[target_globex.id]))
    target_globex.refresh_from_db()
    assert target_globex.is_active is True


def test_extract_groups_reads_userinfo_nested_claims():
    """REGRESSION (live-Keycloak e2e, 2026-06-15): allauth's openid_connect
    provider stores extra_data as {"id_token": ..., "userinfo": {...claims...}},
    so the groups claim is nested under 'userinfo', NOT top-level. _extract_groups
    + the login wiring must read it there — otherwise EVERY OIDC user resolves to
    no tenant (silent MT break). The direct reconcile_tenant unit tests pass a
    group set, so they never exercised the token-shape parsing that broke."""
    from web.allauth_adapters import _extract_groups, _claims

    assert _extract_groups({"groups": ["acme"]}) == {"acme"}  # top-level (flat providers)
    # the real allauth openid_connect shape — groups nested under userinfo
    assert _extract_groups({"id_token": "x", "userinfo": {"groups": ["acme", "acme-admins"]}}) == {"acme", "acme-admins"}
    assert _extract_groups({"id_token": "x", "userinfo": {"sub": "1"}}) == set()  # absent -> fail-closed
    # _claims flattens the openid_connect shape (id_token + userinfo) to the claim dict
    assert _claims({"id_token": "x", "userinfo": {"email": "a@x", "groups": ["g"]}}).get("email") == "a@x"


class ExitModelTests(TestCase):
    def test_global_and_assigned_exits(self):
        from users.models import Tenant, Exit

        t = Tenant.objects.create(slug="acme", name="Acme")
        Exit.objects.create(slug="gwGlobal", name="Shared", is_global=True)
        d = Exit.objects.create(slug="gw1", name="Acme dedicated")
        t.exits.add(d)
        assert set(t.exits.values_list("slug", flat=True)) == {"gw1"}
        assert set(Exit.objects.filter(is_global=True).values_list("slug", flat=True)) == {"gwGlobal"}
        # inactive exits still stored but flagged
        d.active = False
        d.save()
        assert Exit.objects.filter(active=True, is_global=False).count() == 0


def _viewer(tenant_id=None, is_local_admin=False):
    return SimpleNamespace(tenant_id=tenant_id, is_local_admin=is_local_admin)


class AllowedExitTests(TestCase):
    def setUp(self):
        from users.models import Tenant, Exit

        self.t = Tenant.objects.create(slug="acme", name="Acme")
        self.other = Tenant.objects.create(slug="globex", name="Globex")
        Exit.objects.create(slug="gwGlobal", name="Shared", is_global=True)
        self.dedic = Exit.objects.create(slug="gw1", name="Acme dedicated")
        self.inactive = Exit.objects.create(slug="gwOld", name="Retired", active=False)
        self.t.exits.add(self.dedic)
        self.t.exits.add(self.inactive)

    def _cfg(self, enabled=True, mode="locked"):
        return MTConfig(enabled=enabled, mode=mode, default_visibility="", local_admins_manage_all_tenants=True)

    def test_locked_tenant_gets_globals_plus_assigned(self):
        with mock.patch("users.tenancy.multitenancy_config", return_value=self._cfg()):
            assert allowed_exit_slugs(_viewer(tenant_id=self.t.id)) == {"gwGlobal", "gw1"}

    def test_tenant_without_assigned_gets_only_globals(self):
        with mock.patch("users.tenancy.multitenancy_config", return_value=self._cfg()):
            assert allowed_exit_slugs(_viewer(tenant_id=self.other.id)) == {"gwGlobal"}

    def test_locked_tenantless_gets_globals_only(self):
        # tenant_id=None exercises the `tid is not None` guard: globals only, no M2M lookup
        with mock.patch("users.tenancy.multitenancy_config", return_value=self._cfg()):
            assert allowed_exit_slugs(_viewer(tenant_id=None)) == {"gwGlobal"}

    def test_inactive_assigned_exit_excluded(self):
        # gwOld is assigned to acme but inactive -> never offered
        with mock.patch("users.tenancy.multitenancy_config", return_value=self._cfg()):
            assert "gwOld" not in allowed_exit_slugs(_viewer(tenant_id=self.t.id))

    def test_shared_mode_is_unrestricted(self):
        with mock.patch("users.tenancy.multitenancy_config", return_value=self._cfg(mode="shared")):
            assert allowed_exit_slugs(_viewer(tenant_id=self.t.id)) is None

    def test_mt_disabled_is_unrestricted(self):
        with mock.patch("users.tenancy.multitenancy_config", return_value=self._cfg(enabled=False)):
            assert allowed_exit_slugs(_viewer(tenant_id=self.t.id)) is None

    def test_local_admin_is_unrestricted(self):
        with mock.patch("users.tenancy.multitenancy_config", return_value=self._cfg()):
            assert allowed_exit_slugs(_viewer(tenant_id=self.t.id, is_local_admin=True)) is None
