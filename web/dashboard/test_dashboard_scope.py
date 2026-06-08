import pytest
from django.contrib.auth.models import User


@pytest.mark.django_db
def test_dashboard_entitled_scopes(cape_db, mt_enabled, monkeypatch):
    from dashboard.views import entitled_scopes
    from users.models import Tenant, UserProfile
    t = Tenant.objects.create(slug="acme", name="Acme")
    u = User.objects.create_user("a", "a@x.com", "x")
    p = UserProfile.objects.get(user=u); p.tenant = t; p.save()
    u = User.objects.get(pk=u.pk)
    assert entitled_scopes(u) == ["public", "tenant", "mine"]
    tl = User.objects.create_user("b", "b@x.com", "x")  # tenant-less
    assert entitled_scopes(tl) == ["public", "mine"]
