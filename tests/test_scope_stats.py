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
