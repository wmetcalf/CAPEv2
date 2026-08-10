"""DB-backed fail-closed guards for the tenant egress-exit resolver + its import-optional facade.

Covers the two review findings the pure-function tests cannot reach:
  F1  web.tenancy_optional.allowed_exit_slugs returned None (== UNRESTRICTED) on ImportError,
      silently switching the whole ACL off while MT stayed enabled and locked.
  F9  users.tenancy.allowed_exit_slugs gave a tenantless/anonymous viewer the GLOBAL exit set
      instead of deny-all, so an unauthenticated caller on a token-auth-off node could detonate
      with real egress through a shared exit.

Both FAIL on the pre-fix code.
"""
import sys
from types import SimpleNamespace
from unittest import mock

import pytest
from django.test import TestCase

from lib.cuckoo.common.tenancy import MTConfig
from users.models import Exit, Tenant
from users.tenancy import allowed_exit_slugs

pytest_plugins = ("mt_test_fixtures",)


def _viewer(tenant_id=None, is_local_admin=False):
    return SimpleNamespace(tenant_id=tenant_id, is_local_admin=is_local_admin)


def _locked():
    return MTConfig(enabled=True, mode="locked", default_visibility="", local_admins_manage_all_tenants=True)


class TenantlessDeniesTests(TestCase):
    """F9: locked mode + no resolvable tenant => deny-all, never the global set."""

    def setUp(self):
        self.glob = Exit.objects.create(slug="gwGlobal", is_global=True, active=True)
        self.t = Tenant.objects.create(slug="acme", name="Acme")
        self.own = Exit.objects.create(slug="gwAcme", is_global=False, active=True)
        self.t.exits.add(self.own)

    def test_anonymous_viewer_gets_deny_all_not_globals(self):
        with mock.patch("users.tenancy.multitenancy_config", return_value=_locked()):
            assert allowed_exit_slugs(_viewer(tenant_id=None)) == set()

    def test_zero_tenant_sentinel_gets_deny_all(self):
        """tenancy_optional's broken-MT viewer_for returns Viewer(tenant_id=0); a falsy tid must
        deny, not fall through to the global union."""
        with mock.patch("users.tenancy.multitenancy_config", return_value=_locked()):
            assert allowed_exit_slugs(_viewer(tenant_id=0)) == set()

    def test_real_tenant_still_gets_globals_plus_its_own(self):
        with mock.patch("users.tenancy.multitenancy_config", return_value=_locked()):
            assert allowed_exit_slugs(_viewer(tenant_id=self.t.id)) == {"gwGlobal", "gwAcme"}

    def test_local_admin_remains_unrestricted(self):
        with mock.patch("users.tenancy.multitenancy_config", return_value=_locked()):
            assert allowed_exit_slugs(_viewer(tenant_id=None, is_local_admin=True)) is None


class FacadeFailClosedTests(TestCase):
    """F1: the import-optional facade must deny, not go unrestricted, when MT is enabled but its
    import chain is broken -- the contract the module docstring states verbatim."""

    @staticmethod
    def _facade_with_broken_users_tenancy(mt_enabled):
        import web.tenancy_optional as to

        # Simulate `from users.tenancy import allowed_exit_slugs` raising, the exact skewed-deploy
        # shape: new web tier against a users app that predates the symbol.
        with mock.patch.dict(sys.modules, {"users.tenancy": None}):
            with mock.patch.object(to, "_mt_enabled", return_value=mt_enabled):
                return to.allowed_exit_slugs(_viewer(tenant_id=1))

    def test_mt_enabled_but_broken_import_denies(self):
        got = self._facade_with_broken_users_tenancy(mt_enabled=True)
        assert got is not None, "None means UNRESTRICTED -- the ACL would be silently off"
        assert set(got) == set()

    def test_mt_genuinely_absent_stays_unrestricted(self):
        """Single-tenant/upstream: no exit ACL exists, so None (no gating) is correct."""
        assert self._facade_with_broken_users_tenancy(mt_enabled=False) is None


class ExitPrivilegeFieldTests(TestCase):
    """F5: Tenant.exits is an egress-PRIVILEGE field, not tenant metadata."""

    def test_exits_is_readonly_for_a_non_superuser(self):
        """TenantAdmin sets filter_horizontal = ("exits",), so a delegated change_tenant grant put a
        working exit picker in front of a non-superuser: they could assign their own tenant any Exit,
        including another tenant's dedicated one, then submit through it."""
        from users.admin import _TENANT_PRIV_FIELDS

        assert "exits" in _TENANT_PRIV_FIELDS


class MissingMigrationTests(TestCase):
    """F10: migration 0006 unapplied must not 500 every submit -- and must not fail open either."""

    def test_database_error_denies_instead_of_raising(self):
        from django.db import DatabaseError

        with mock.patch("users.tenancy.multitenancy_config", return_value=_locked()):
            with mock.patch("users.models.Exit.objects") as objs:
                objs.filter.side_effect = DatabaseError('relation "users_exit" does not exist')
                got = allowed_exit_slugs(_viewer(tenant_id=1))
        assert got == set(), "an un-migrated ACL table must deny, never return None (unrestricted)"


class PendingRestampTests(TestCase):
    """F7: the ACL is snapshotted at submit, so revocation missed already-queued work."""

    def setUp(self):
        self.t = Tenant.objects.create(slug="acme", name="Acme")
        self.e1 = Exit.objects.create(slug="gw1", active=True)
        self.e2 = Exit.objects.create(slug="gw2", active=True)

    def _restamps(self, fn):
        """Run fn() with the task store stubbed; return the (tenant_id, csv) re-stamp calls."""
        calls = []

        class _DB:
            session = mock.MagicMock()

            def restamp_pending_allowed_exits(self, tenant_id, csv):
                calls.append((tenant_id, csv))
                return 1

        with mock.patch("users.tenancy.multitenancy_config", return_value=_locked()):
            with mock.patch("lib.cuckoo.common.tenancy.multitenancy_config", return_value=_locked()):
                with mock.patch("lib.cuckoo.core.database.Database", _DB):
                    fn()
        return calls

    def test_revoking_an_exit_restamps_queued_tasks(self):
        self.t.exits.add(self.e1, self.e2)
        calls = self._restamps(lambda: self.t.exits.remove(self.e2))
        assert calls, "removing an exit must push the narrowed ACL onto pending tasks"
        assert calls[-1] == (self.t.id, "gw1")

    def test_granting_an_exit_also_restamps(self):
        """An ADD has to propagate too, or a freshly-granted exit stays unusable by queued tasks."""
        self.t.exits.add(self.e1)
        calls = self._restamps(lambda: self.t.exits.add(self.e2))
        assert calls[-1] == (self.t.id, "gw1,gw2")

    def test_clearing_all_exits_restamps_deny_all(self):
        self.t.exits.add(self.e1)
        calls = self._restamps(self.t.exits.clear)
        assert calls[-1] == (self.t.id, ""), "a tenant with no exits must be stamped deny-all, not NULL"

    def test_reverse_side_edit_restamps_too(self):
        """Editing the M2M from ExitAdmin (Exit.tenants) must behave the same as from TenantAdmin."""
        calls = self._restamps(lambda: self.e1.tenants.add(self.t))
        assert calls[-1] == (self.t.id, "gw1")

    def test_a_task_store_failure_does_not_break_the_admin_save(self):
        class _BoomDB:
            session = mock.MagicMock()

            def restamp_pending_allowed_exits(self, tenant_id, csv):
                raise RuntimeError("task store down")

        with mock.patch("users.tenancy.multitenancy_config", return_value=_locked()):
            with mock.patch("lib.cuckoo.common.tenancy.multitenancy_config", return_value=_locked()):
                with mock.patch("lib.cuckoo.core.database.Database", _BoomDB):
                    self.t.exits.add(self.e1)  # must not raise
        assert list(self.t.exits.values_list("slug", flat=True)) == ["gw1"]

    def test_mt_disabled_does_not_touch_the_task_store(self):
        from lib.cuckoo.common.tenancy import MTConfig

        off = MTConfig(enabled=False, mode="shared", default_visibility="", local_admins_manage_all_tenants=True)

        class _DB:
            session = mock.MagicMock()

            def restamp_pending_allowed_exits(self, tenant_id, csv):
                raise AssertionError("must not re-stamp when MT is disabled")

        with mock.patch("lib.cuckoo.common.tenancy.multitenancy_config", return_value=off):
            with mock.patch("lib.cuckoo.core.database.Database", _DB):
                self.t.exits.add(self.e1)
