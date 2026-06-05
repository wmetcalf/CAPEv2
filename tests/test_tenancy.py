import pytest

from tests.tenancy_vectors import VECTORS
from lib.cuckoo.common.tenancy import can_read, can_toggle, Viewer, Job


@pytest.mark.parametrize("label,viewer,job,want_read,want_toggle", VECTORS, ids=[v[0] for v in VECTORS])
def test_predicate_matches_vectors(label, viewer, job, want_read, want_toggle):
    v = Viewer(**viewer)
    j = Job(**job)
    assert can_read(v, j) is want_read, f"{label}: read"
    assert can_toggle(v, j) is want_toggle, f"{label}: toggle"


def test_config_defaults():
    from lib.cuckoo.common import tenancy
    cfg = tenancy.multitenancy_config()
    assert cfg.enabled in (True, False)
    assert cfg.mode in ("shared", "locked")
    assert isinstance(cfg.local_admins_manage_all_tenants, bool)
    # default_visibility resolves per mode when blank
    assert tenancy.default_visibility(cfg) in ("public", "tenant", "private")


def test_default_visibility_per_mode():
    from lib.cuckoo.common import tenancy
    shared = tenancy.MTConfig(enabled=True, mode="shared", default_visibility="",
                              local_admins_manage_all_tenants=True)
    locked = tenancy.MTConfig(enabled=True, mode="locked", default_visibility="",
                              local_admins_manage_all_tenants=True)
    assert tenancy.default_visibility(shared) == "public"
    assert tenancy.default_visibility(locked) == "tenant"
