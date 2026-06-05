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
