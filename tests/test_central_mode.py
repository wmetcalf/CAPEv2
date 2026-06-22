"""Pure-logic unit tests for central mode — no Django / pymongo / CAPE web imports
(the web layer is iterated live on a CAPE box). Run: pytest tests/test_central_mode.py"""
import os

from lib.cuckoo.common.central_mode import _parse, _as_bool, upload_target_realpath
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


def test_upload_target_realpath(tmp_path):
    # Layout: storage/{analyses/<id>, binaries, guacrecordings}; a sibling secret outside storage.
    storage = tmp_path / "storage"
    analysis = storage / "analyses" / "42"
    binaries = storage / "binaries"
    guac = storage / "guacrecordings"
    for d in (analysis, binaries, guac):
        d.mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("AWS_SECRET")
    blob = binaries / "deadbeef"
    blob.write_text("sample bytes")
    rec = guac / "42_sess"
    rec.write_text("guac dump")

    base_real = os.path.realpath(str(analysis))
    trusted = [os.path.realpath(str(binaries)), os.path.realpath(str(guac))]

    # a regular file inside the analysis tree -> uploaded (its own realpath)
    plain = analysis / "report.json"
    plain.write_text("{}")
    assert upload_target_realpath(str(plain), base_real, trusted) == os.path.realpath(str(plain))

    # the `binary` symlink -> resolves into storage/binaries (trusted) -> uploaded as the blob
    binlink = analysis / "binary"
    binlink.symlink_to(blob)
    assert upload_target_realpath(str(binlink), base_real, trusted) == os.path.realpath(str(blob))

    # a recording referenced via symlink into storage/guacrecordings (trusted) -> uploaded
    reclink = analysis / "guac.rec"
    reclink.symlink_to(rec)
    assert upload_target_realpath(str(reclink), base_real, trusted) == os.path.realpath(str(rec))

    # a sample-planted symlink to a host secret OUTSIDE storage roots -> skipped (None)
    evil = analysis / "evil"
    evil.symlink_to(secret)
    assert upload_target_realpath(str(evil), base_real, trusted) is None

    # prefix-collision guard: a sibling dir sharing a name prefix must NOT count as inside
    sibling = storage / "binaries-evil"
    sibling.mkdir()
    sneaky = sibling / "x"
    sneaky.write_text("nope")
    link2 = analysis / "sneaky"
    link2.symlink_to(sneaky)
    assert upload_target_realpath(str(link2), base_real, trusted) is None


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
