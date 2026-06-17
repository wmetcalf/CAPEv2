"""Pure-logic unit tests for central mode — no Django / pymongo / CAPE web imports
(the web layer is iterated live on a CAPE box). Run: pytest tests/test_central_mode.py"""
from lib.cuckoo.common.central_mode import _parse, _as_bool
from lib.cuckoo.common.hunt_query import build_hunt_facets


def test_as_bool():
    assert _as_bool("yes") is True
    assert _as_bool("on") is True
    assert _as_bool("no") is False
    assert _as_bool(None, False) is False
    assert _as_bool(True) is True


def test_central_mode_defaults_off():
    c = _parse({})
    assert c.enabled is False
    assert c.s3_bucket == ""
    assert c.s3_prefix == "results"


def test_central_mode_on():
    c = _parse({"enabled": "yes", "s3_bucket": "b", "s3_region": "us-east-1", "s3_prefix": "results"})
    assert c.enabled is True
    assert c.s3_bucket == "b"


def test_hunt_facets_per_category_no_facet():
    sent = []

    def fake_agg(coll, pipeline):
        sent.append(pipeline)
        return [{"_id": "evil.com", "count": 5, "task_ids": [1, 2]}]

    facets = build_hunt_facets(
        fake_agg,
        match={"$and": [{}, {"info.visibility": "public"}]},
        hunt_map={"domains": {"db_unwind": "$network.domains", "db_group": "$network.domains.domain", "db_match": {"count": {"$gte": 1}}}},
        categories={"domains": True},
        min_count=1,
    )
    assert not any(any("$facet" in stage for stage in p) for p in sent)
    assert sent[0][0] == {"$match": {"$and": [{}, {"info.visibility": "public"}]}}
    assert facets["domains"][0]["_id"] == "evil.com"
