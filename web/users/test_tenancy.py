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
