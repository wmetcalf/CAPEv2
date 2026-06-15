import pytest
from django.contrib.auth.models import User


@pytest.mark.django_db
def test_submit_form_renders_visibility_control(cape_db, client):
    u = User.objects.create_user("a", "a@x.com", "x")
    client.force_login(u)
    try:
        from django.urls import reverse
        url = reverse("submission")
    except Exception:
        url = "/submit/"
    r = client.get(url)
    assert r.status_code == 200
    assert b'name="visibility"' in r.content


# --- T4: status / remote_session guac token-mint full-stack cross-tenant denial ---

class _ForeignRunning:
    id = 1
    user_id = 999
    tenant_id = 10
    visibility = "private"
    status = "running"
    machine = "win10-1"
    options = ""
    target = "x.exe"
    sample = None


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["/submit/status/1/", "/submit/remote_session/1/"])
def test_guac_session_denies_cross_tenant(cape_db, mt_enabled, monkeypatch, client, path):
    """status() and remote_session() emit the base64 guac session_data that
    authorizes a live-VM tunnel into a running analysis VM. A cross-tenant viewer
    must be denied (the generic "doesn't seem to exist" page) and NO session token
    may be minted. guac.index had an HTTP test; these token-mint twins were only
    AST-gated."""
    import submission.views as sv

    monkeypatch.setattr(sv.db, "view_task", lambda *a, **k: _ForeignRunning())
    client.force_login(User.objects.create_user("gx", "gx@x.com", "x"))  # tenant-less
    r = client.get(path)
    assert r.status_code == 200
    # error.html renders "The specified task doesn't seem to exist." — match an
    # apostrophe-free fragment (Django HTML-escapes the apostrophe in "doesn't").
    assert b"specified task" in r.content              # denied, indistinguishable
    assert (r.context or {}).get("session_data", "") == ""   # no tunnel token minted


@pytest.mark.django_db
def test_remote_session_allows_owner(cape_db, mt_enabled, monkeypatch, client):
    """Positive control: the OWNER of a (non-running) private task passes the gate
    (no "doesn't seem to exist"), proving the denial is conditional on visibility."""
    import submission.views as sv

    u = User.objects.create_user("ow", "ow@x.com", "x")

    class _Own:
        id = 1; tenant_id = None; visibility = "private"; status = "completed"
    own = _Own()
    own.user_id = u.id

    monkeypatch.setattr(sv.db, "view_task", lambda *a, **k: own)
    client.force_login(u)
    r = client.get("/submit/remote_session/1/")
    assert r.status_code == 200
    assert b"specified task" not in r.content                # gate allowed (not denied)
    assert (r.context or {}).get("session_data", "") == ""   # not running -> empty (no leak either)
