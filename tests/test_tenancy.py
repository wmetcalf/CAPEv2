import pytest

from tests.tenancy_vectors import VECTORS
from lib.cuckoo.common.tenancy import can_read, can_toggle, Viewer, Job


@pytest.mark.parametrize("label,viewer,job,want_read,want_toggle", VECTORS, ids=[v[0] for v in VECTORS])
def test_predicate_matches_vectors(label, viewer, job, want_read, want_toggle):
    v = Viewer(**viewer)
    j = Job(**job)
    assert can_read(v, j) is want_read, f"{label}: read"
    assert can_toggle(v, j) is want_toggle, f"{label}: toggle"
