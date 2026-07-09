"""HTTP-level exit-ACL smoke for the submit view (Task 7). Drives the REAL /submit/ endpoint
through the Django test client (per the "test real endpoints, not functions" lesson) so the full
wiring is exercised: allowed_exit_slugs(viewer_for(request.user)) -> validate_and_scope_route ->
error render. A cross-tenant exit must be rejected; the tenant's own exit must pass the ACL."""
import pytest
from django.contrib.auth.models import User


def _tenant_user(username, tenant):
    from users.models import UserProfile

    u = User.objects.create_user(username, f"{username}@x.com", "x")
    p = UserProfile.objects.get(user=u)  # auto-created by signal
    p.tenant = tenant
    p.save()
    return User.objects.get(pk=u.pk)  # refetch so request.user.userprofile reflects the tenant


@pytest.mark.django_db
def test_submit_rejects_cross_tenant_exit(cape_db, mt_enabled, client):
    from users.models import Exit, Tenant

    acme = Tenant.objects.create(slug="acme", name="Acme")
    gw1 = Exit.objects.create(slug="gw1", name="Acme dedicated")
    Exit.objects.create(slug="gw2", name="Someone else's exit")  # a known exit NOT assigned to acme
    acme.exits.add(gw1)
    client.force_login(_tenant_user("alice", acme))
    r = client.post("/submit/", {"url": "http://example.com", "route": "gw2"})
    assert r.status_code == 200
    assert b"not permitted" in r.content  # the exit ACL rejected the foreign gateway at submit


@pytest.mark.django_db
def test_submit_allows_own_exit(cape_db, mt_enabled, client):
    from users.models import Exit, Tenant

    acme = Tenant.objects.create(slug="acme", name="Acme")
    gw1 = Exit.objects.create(slug="gw1", name="Acme dedicated")
    acme.exits.add(gw1)
    client.force_login(_tenant_user("bob", acme))
    r = client.post("/submit/", {"url": "http://example.com", "route": "gw1"})
    assert r.status_code == 200
    assert b"not permitted" not in r.content  # own exit passes the ACL (downstream may vary)


@pytest.mark.django_db
def test_submit_rejects_pool_when_tenant_has_no_exits(cape_db, mt_enabled, client):
    from users.models import Tenant

    globex = Tenant.objects.create(slug="globex", name="Globex")  # zero assigned exits, no globals
    client.force_login(_tenant_user("carol", globex))
    r = client.post("/submit/", {"url": "http://example.com", "route": "nexthop"})
    assert r.status_code == 200
    assert b"no egress exits assigned" in r.content  # pool with an empty allowed set is rejected
