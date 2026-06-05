import pytest


def _mk_task(category="file", target="x.exe"):
    from lib.cuckoo.core.data.task import Task
    t = Task(target=target)
    t.category = category
    return t


@pytest.mark.usefixtures("tmp_cuckoo_root")
def test_task_has_tenant_and_visibility(db):
    from lib.cuckoo.core.data.task import Task
    t = _mk_task()
    t.user_id = 1
    t.tenant_id = 10
    t.visibility = "tenant"
    db.session.add(t)
    db.session.commit()
    row = db.session.get(Task, t.id)
    assert row.tenant_id == 10
    assert row.visibility == "tenant"


@pytest.mark.usefixtures("tmp_cuckoo_root")
def test_visibility_defaults_private(db):
    from lib.cuckoo.core.data.task import Task
    t = _mk_task()
    db.session.add(t)
    db.session.commit()
    assert db.session.get(Task, t.id).visibility == "private"
    assert db.session.get(Task, t.id).tenant_id is None


@pytest.mark.usefixtures("tmp_cuckoo_root")
def test_list_tasks_visible_filter(db):
    from lib.cuckoo.common.tenancy import Viewer

    def mk(owner, tenant, vis):
        t = _mk_task()
        t.user_id, t.tenant_id, t.visibility = owner, tenant, vis
        db.session.add(t)
        db.session.commit()
        return t.id

    pub = mk(1, 10, "public")
    ten = mk(1, 10, "tenant")
    priv = mk(1, 10, "private")
    other = mk(3, 20, "tenant")

    # viewer: member of tenant 10, not owner of priv
    v = Viewer(user_id=2, tenant_id=10)
    ids = {t.id for t in db.list_tasks(visible_to=v)}
    assert pub in ids and ten in ids
    assert priv not in ids        # private, not owner
    assert other not in ids       # other tenant

    # break-glass sees everything
    allv = Viewer(user_id=9, tenant_id=None, is_superuser=True, is_local_admin=True)
    allids = {t.id for t in db.list_tasks(visible_to=allv)}
    assert {pub, ten, priv, other} <= allids


@pytest.mark.usefixtures("tmp_cuckoo_root")
def test_add_url_stamps_tenant_and_visibility(db):
    from lib.cuckoo.core.data.task import Task
    tid = db.add_url("http://example.com", tenant_id=10, visibility="tenant")
    t = db.session.get(Task, tid)
    assert t.tenant_id == 10
    assert t.visibility == "tenant"


@pytest.mark.usefixtures("tmp_cuckoo_root")
def test_add_url_defaults_private(db):
    from lib.cuckoo.core.data.task import Task
    tid = db.add_url("http://example.com")
    assert db.session.get(Task, tid).visibility == "private"
