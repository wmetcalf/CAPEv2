import time


def test_statistics_helpers_merge_scope_match(monkeypatch):
    import lib.cuckoo.common.web_utils as wu
    captured = {}

    def fake_agg(coll, cmd):
        captured["cmd"] = cmd
        return []

    monkeypatch.setattr(wu, "mongo_aggregate", fake_agg, raising=False)
    # Bypass config guards so mongo_aggregate is reached in the test environment.
    monkeypatch.setattr(wu.repconf.mongodb, "enabled", True)
    monkeypatch.setattr(wu.web_cfg.general, "top_detections", True)
    # Clear any cached result so the aggregation actually runs.
    if hasattr(wu.top_detections, "cache"):
        del wu.top_detections.cache
    wu.top_detections(date_since=False, scope_match={"info.tenant_id": 10, "info.visibility": "tenant"})
    first_match = captured["cmd"][0]["$match"]
    assert first_match.get("info.tenant_id") == 10 and first_match.get("info.visibility") == "tenant"


def test_top_detections_scoped_bypasses_cache(monkeypatch):
    """Scoped calls must never read from or write to the shared time-keyed cache."""
    import lib.cuckoo.common.web_utils as wu

    call_count = {"n": 0}

    def counting_agg(coll, cmd):
        call_count["n"] += 1
        return []

    monkeypatch.setattr(wu, "mongo_aggregate", counting_agg, raising=False)
    monkeypatch.setattr(wu.repconf.mongodb, "enabled", True)
    monkeypatch.setattr(wu.web_cfg.general, "top_detections", True)

    # Plant a fresh-looking stale sentinel in the cache so an unguarded read would return it.
    stale_data = [{"sentinel": 1}]
    wu.top_detections.cache = (time.time(), stale_data)

    scope = {"info.tenant_id": 10, "info.visibility": "tenant"}

    # First scoped call: must NOT return the sentinel and must have called the aggregation.
    result1 = wu.top_detections(date_since=False, scope_match=scope)
    assert result1 != stale_data, "scoped call returned stale cached data from the shared cache"
    assert call_count["n"] == 1, "expected exactly one aggregation call after first scoped call"

    # Second scoped call: scoped path must not have populated the cache, so aggregation runs again.
    result2 = wu.top_detections(date_since=False, scope_match=scope)
    assert call_count["n"] == 2, "expected aggregation to run again (scoped calls must not populate cache)"

    # The shared cache must still hold the original sentinel (scoped calls never overwrite it).
    assert hasattr(wu.top_detections, "cache"), "cache attr should still exist"
    _, cached_val = wu.top_detections.cache
    assert cached_val == stale_data, "scoped call must not have overwritten the shared cache"
