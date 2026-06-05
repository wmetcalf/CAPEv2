import pytest
from django.contrib.auth.models import User


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
def test_viewer_for_maps_user():
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
def test_submission_scope(monkeypatch):
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
