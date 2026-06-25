"""MT-absent integration smoke — the inverse of the MT-present faithful suite.

With the multi-tenant layer (users.tenancy + lib.cuckoo.common.tenancy) made unimportable,
the import-optional facades must fall back to see-all AND the base view modules must still
import. This proves central mode runs WITHOUT our multi-tenant fork (the Phase 4 un-weave
goal). Box-run: needs the Django/CAPE environment (pytest-django sets up Django). The test
restores the real facade bindings in a finally so it doesn't pollute later tests.
"""
import builtins
import importlib


def test_mt_absent_facades_see_all_and_base_views_import():
    import lib.cuckoo.common.tenancy_optional as L
    import web.tenancy_optional as W

    real_import = builtins.__import__

    def hide(name, *args, **kwargs):
        if name in ("users.tenancy", "lib.cuckoo.common.tenancy") or \
           name.startswith("users.tenancy.") or name.startswith("lib.cuckoo.common.tenancy."):
            raise ImportError("simulated: MT layer absent")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = hide
    try:
        # re-run the facades' module-level (constant) imports under the hidden layer
        importlib.reload(L)
        importlib.reload(W)

        # lib facade -> MT-disabled-equivalent fallbacks
        assert L.multitenancy_config().enabled is False
        assert L.viewer_for(object()).is_local_admin is True
        assert L.PUBLIC == "public" and L.VISIBILITIES == ("public", "tenant", "private")
        assert L.scope_match("public", object()) is None
        assert L.viewer_scope_match(object()) is None

        # web facade -> see-all fallbacks (can_* True, scopes None)
        assert W.can_view_task(object(), object()) is True
        assert W.can_view_sample(object(), sha256="x") is True
        assert W.viewer_for(object()).is_local_admin is True
        assert W.viewer_scope_filter(object()) is None
        assert W.submission_scope(object()) is None

        # the base view modules import with the MT layer absent (the core un-weave claim)
        for mod in ("analysis.views", "apiv2.views", "submission.views",
                    "dashboard.views", "guac.views", "compare.views"):
            importlib.import_module(mod)
    finally:
        builtins.__import__ = real_import
        importlib.reload(L)  # restore real bindings for any later tests in the session
        importlib.reload(W)
