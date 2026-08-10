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
