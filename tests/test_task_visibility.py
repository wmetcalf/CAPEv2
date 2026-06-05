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
