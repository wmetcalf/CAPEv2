"""reschedule() must carry task.allowed_exits onto the recreated task. If it drops it (as it did
before the fix), scheduler-restart recovery (startup.py init_tasks) and the manual reschedule API
silently reset allowed_exits -> NULL == UNRESTRICTED, voiding the tenant egress ACL. Regression
guard for the fork tenant-egress-exits feature."""


def test_reschedule_preserves_allowed_exits(db):
    tid = db.add_url(url="http://example.com", route="gw1", allowed_exits="gw1,gwG")
    assert tid
    assert db.view_task(tid).allowed_exits == "gw1,gwG"
    new_id = db.reschedule(tid)
    assert new_id and new_id != tid
    assert db.view_task(new_id).allowed_exits == "gw1,gwG"  # carried across, not dropped to NULL


def test_reschedule_preserves_null_allowed_exits(db):
    # unrestricted task (allowed_exits NULL) stays NULL across reschedule
    tid = db.add_url(url="http://example.com", route="none")
    new_id = db.reschedule(tid)
    assert db.view_task(new_id).allowed_exits is None
