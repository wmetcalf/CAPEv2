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


@pytest.mark.usefixtures("tmp_cuckoo_root")
def test_set_task_visibility_validates_enum(db):
    """Defense-in-depth (M1): the DB setter rejects unknown levels so a bogus
    value can never be persisted even if a caller skips the view-layer check."""
    from lib.cuckoo.core.data.task import Task
    tid = db.add_url("http://example.com", tenant_id=10, visibility="tenant")

    with pytest.raises(ValueError):
        db.set_task_visibility(tid, "bogus")
    # unchanged after the rejected write
    assert db.session.get(Task, tid).visibility == "tenant"

    # a valid level still works
    assert db.set_task_visibility(tid, "public") is not None
    assert db.session.get(Task, tid).visibility == "public"


@pytest.mark.usefixtures("tmp_cuckoo_root")
def test_count_tasks_scope(db):
    from lib.cuckoo.common.tenancy import Viewer

    def mk(owner, tenant, vis):
        t = _mk_task(); t.user_id, t.tenant_id, t.visibility = owner, tenant, vis
        db.session.add(t); db.session.commit()

    mk(1, 10, "public"); mk(1, 10, "tenant"); mk(2, 10, "private"); mk(3, 20, "public")
    v = Viewer(user_id=2, tenant_id=10)
    assert db.count_tasks(scope="public", viewer=v) == 2     # the two public ones
    assert db.count_tasks(scope="tenant", viewer=v) == 1     # tenant-vis in tenant 10
    assert db.count_tasks(scope="mine", viewer=v) == 1       # owner==2
    assert db.count_tasks(scope="global", viewer=v) == 4     # break-glass / no filter

    # the other scope-aware methods apply the same filter / execute cleanly
    assert sum(db.get_tasks_status_count(scope="public", viewer=v).values()) == 2
    assert sum(db.get_tasks_status_count(scope="global", viewer=v).values()) == 4
    assert db.minmax_tasks(scope="mine", viewer=v) is not None      # runs scoped, returns a tuple
    assert isinstance(db.count_samples(scope="tenant", viewer=v), int)  # scoped distinct-sample count


@pytest.mark.usefixtures("tmp_cuckoo_root")
def test_count_matching_tasks_visible_filter(db):
    """Pagination counts must apply the SAME visibility filter as the listing,
    or the page totals leak other tenants' submission volumes (and produce
    empty pages). count_matching_tasks(visible_to=) must agree with list_tasks."""
    from lib.cuckoo.common.tenancy import Viewer

    def mk(owner, tenant, vis):
        t = _mk_task()
        t.user_id, t.tenant_id, t.visibility = owner, tenant, vis
        db.session.add(t)
        db.session.commit()

    mk(1, 10, "public")
    mk(1, 10, "tenant")
    mk(1, 10, "private")
    mk(3, 20, "tenant")

    v = Viewer(user_id=2, tenant_id=10)  # tenant-10 member, not owner of the private one
    # the page count must equal the number of rows actually listed for that viewer
    assert db.count_matching_tasks(visible_to=v) == len(db.list_tasks(visible_to=v, limit=100000))
    # and it must be strictly fewer than the unfiltered total (private + other-tenant hidden)
    assert db.count_matching_tasks(visible_to=v) < db.count_matching_tasks()
    # break-glass counts everything, same as no filter
    allv = Viewer(user_id=9, tenant_id=None, is_local_admin=True)
    assert db.count_matching_tasks(visible_to=allv) == db.count_matching_tasks()
