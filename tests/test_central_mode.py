"""Pure-logic unit tests for central mode — no Django / pymongo / CAPE web imports
(the web layer is iterated live on a CAPE box). Run: pytest tests/test_central_mode.py"""
import os

import pytest

from lib.cuckoo.common.central_mode import _parse, _as_bool, upload_target_realpath
from lib.cuckoo.common.hunt_query import build_hunt_facets
from lib.cuckoo.common.storage_backend import (
    ArtifactNotFound,
    LocalFSStore,
    S3Store,
    get_artifact_store,
)


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


def test_central_mode_backend_fields_parse():
    # The new backend-selection fields (the only thing that made the store AWS-locked)
    # parse from [central_mode], with sane defaults when absent.
    d = _parse({})
    assert d.storage_backend == "s3"
    assert d.s3_endpoint_url == "" and d.s3_access_key == "" and d.s3_secret_key == ""
    assert d.central_local_root == ""
    c = _parse({
        "enabled": "yes",
        "storage_backend": "LOCAL",  # normalized to lower
        "s3_endpoint_url": "https://minio.local:9000",
        "s3_access_key": "ak",
        "s3_secret_key": "sk",
        "central_local_root": "/srv/cape-central",
    })
    assert c.storage_backend == "local"
    assert c.s3_endpoint_url == "https://minio.local:9000"
    assert c.s3_access_key == "ak" and c.s3_secret_key == "sk"
    assert c.central_local_root == "/srv/cape-central"


def test_get_artifact_store_single_node_is_local_fs():
    store, is_central = get_artifact_store(_parse({}))
    assert is_central is False
    assert isinstance(store, LocalFSStore)
    # single-node always reads the local storage/analyses tree
    assert store.base_dir.endswith(os.path.join("storage", "analyses"))


def test_get_artifact_store_central_s3():
    store, is_central = get_artifact_store(
        _parse({"enabled": "yes", "s3_bucket": "bkt", "s3_region": "eu-west-1"})
    )
    assert is_central is True
    assert isinstance(store, S3Store)
    assert store.bucket == "bkt" and store.region == "eu-west-1"
    # endpoint/creds default to None so boto3 uses AWS's endpoint + default cred chain
    assert store.endpoint_url is None and store.access_key is None and store.secret_key is None


def test_get_artifact_store_central_minio_endpoint():
    store, _ = get_artifact_store(
        _parse({
            "enabled": "yes", "s3_bucket": "bkt",
            "s3_endpoint_url": "https://minio.local:9000",
            "s3_access_key": "ak", "s3_secret_key": "sk",
        })
    )
    assert isinstance(store, S3Store)
    assert store.endpoint_url == "https://minio.local:9000"
    assert store.access_key == "ak" and store.secret_key == "sk"


def test_get_artifact_store_central_local_mount(tmp_path):
    root = str(tmp_path / "central")
    store, is_central = get_artifact_store(
        _parse({"enabled": "yes", "storage_backend": "local", "central_local_root": root})
    )
    assert is_central is True
    assert isinstance(store, LocalFSStore)
    assert store.base_dir == root


def test_get_artifact_store_local_backend_without_root_falls_back_to_s3():
    # storage_backend=local but no central_local_root is a misconfig; it must NOT
    # silently read the single-node tree — it falls through to the S3 store (whose
    # missing-bucket prereq centralstore validates up front).
    store, is_central = get_artifact_store(
        _parse({"enabled": "yes", "storage_backend": "local", "s3_bucket": "bkt"})
    )
    assert is_central is True
    assert isinstance(store, S3Store)


def test_localfsstore_roundtrip(tmp_path):
    # LocalFSStore backs BOTH single-node and central-on-a-shared-mount, so its
    # round-trip is the contract the read+write seams depend on.
    base = str(tmp_path / "base")
    store = LocalFSStore(base)
    container = "results/job-1"

    src = tmp_path / "src.txt"
    src.write_text("hello world")
    store.put_file(str(src), container, "reports/report.json")

    assert store.exists(container, "reports/report.json") is True
    assert store.exists(container, "missing") is False

    body_iter, length = store.stream(container, "reports/report.json")
    assert b"".join(body_iter) == b"hello world"
    assert length == len("hello world")

    assert store.read_text(container, "reports/report.json", 1000) == "hello world"
    assert store.read_text(container, "missing", 1000) == ""

    path, is_temp = store.materialize(container, "reports/report.json")
    assert is_temp is False and os.path.exists(path)
    assert store.materialize(container, "missing") == (None, False)

    assert list(store.iter_relpaths(container)) == ["reports/report.json"]

    dest = tmp_path / "out" / "copy.json"
    store.download(container, "reports/report.json", str(dest))
    assert dest.read_text() == "hello world"


def test_localfsstore_stream_missing_raises(tmp_path):
    store = LocalFSStore(str(tmp_path))
    with pytest.raises(ArtifactNotFound):
        store.stream("results/job-x", "nope")


def test_s3store_is_lazy_no_client_on_construct():
    # Constructing the store must NOT build a boto3 client (so import/config never
    # touches the network); the client is built on first op.
    store = S3Store("bkt", "us-east-1")
    assert store._cli is None


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
