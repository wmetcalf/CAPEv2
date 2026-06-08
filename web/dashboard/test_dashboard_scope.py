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


@pytest.mark.django_db
def test_disabled_shows_single_global_scope():
    """Back-compat: with multitenancy disabled (the default / basic public install),
    every user gets a single Global panel == today's dashboard. No mt_enabled fixture
    here, so multitenancy_config() reads the default (disabled) -> viewer_for() returns
    is_local_admin=True -> entitled_scopes short-circuits to ['global']."""
    from dashboard.views import entitled_scopes
    u = User.objects.create_user("c", "c@x.com", "x")
    assert entitled_scopes(u) == ["global"]
