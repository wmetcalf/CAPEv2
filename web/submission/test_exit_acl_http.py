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


@pytest.mark.django_db
def test_submit_stamps_allowed_exits_on_task(cape_db, mt_enabled, client):
    # The load-bearing carrier: the created Task row must carry the tenant's allowed set as CSV,
    # else the worker guard (which reads task.allowed_exits) is a no-op.
    import submission.views as sv
    from users.models import Exit, Tenant

    acme = Tenant.objects.create(slug="acme", name="Acme")
    gw1 = Exit.objects.create(slug="gw1", name="Acme dedicated")
    acme.exits.add(gw1)
    client.force_login(_tenant_user("dave", acme))
    r = client.post("/submit/", {"url": "http://example.com", "route": "gw1"})
    assert r.status_code == 200
    assert b"not permitted" not in r.content
    tasks = sv.db.list_tasks(limit=1)
    assert tasks, "submit created no task"
    assert tasks[0].allowed_exits == "gw1"   # tenant's allowed set stamped as CSV


@pytest.mark.django_db
def test_gateways_picker_filtered_to_tenant(cape_db, mt_enabled, client, monkeypatch):
    # The GET submit form must offer only the tenant's exits (global + assigned), not every [gwX].
    import submission.views as sv
    from django.http import HttpResponse
    from users.models import Exit, Tenant

    acme = Tenant.objects.create(slug="acme", name="Acme")
    gw1 = Exit.objects.create(slug="gw1")
    acme.exits.add(gw1)
    Exit.objects.create(slug="gw2")   # another tenant's exit -- must NOT appear in acme's picker

    class _NH:
        enabled = True
        gateways = "gw1,gw2"

    monkeypatch.setattr(sv.routing, "nexthop", _NH(), raising=False)

    captured = {}

    def _cap_render(request, template, ctx=None, *a, **k):
        captured["ctx"] = ctx or {}
        return HttpResponse("ok")

    monkeypatch.setattr(sv, "render", _cap_render)
    client.force_login(_tenant_user("erin", acme))
    r = client.get("/submit/")
    assert r.status_code == 200
    names = [g["name"] for g in captured.get("ctx", {}).get("gateways", [])]
    assert names == ["gw1"]   # gw2 (foreign) filtered out of the picker


@pytest.mark.django_db
def test_apiv2_tenant_exits_list_scoped(cape_db, mt_enabled):
    # The read endpoint lists the caller's usable exits: own assigned + global, never another tenant's.
    from rest_framework.test import APIRequestFactory, force_authenticate
    import apiv2.views as av
    from users.models import Exit, Tenant

    acme = Tenant.objects.create(slug="acme", name="Acme")
    gw1 = Exit.objects.create(slug="gw1")
    acme.exits.add(gw1)
    Exit.objects.create(slug="gwG", is_global=True)
    Exit.objects.create(slug="gw3")  # another tenant's dedicated exit -> must be excluded
    req = APIRequestFactory().get("/apiv2/tenant_exits/")
    force_authenticate(req, user=_tenant_user("frank", acme))
    resp = av.tenant_exits_list(req)
    assert resp.data["error"] is False
    assert set(resp.data["data"]) == {"gw1", "gwG"}
