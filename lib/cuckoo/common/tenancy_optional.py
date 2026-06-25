"""Import-optional facade for the lib-level MT symbols (lib.cuckoo.common.tenancy).

Delegates to the real MT module when it's importable; when it ISN'T (the MT layer is not
deployed — e.g. an upstream central-only build), returns values IDENTICAL to the MT-disabled
code path (which the real functions already implement: viewer_for -> is_local_admin=True ->
every can_* gate True; scope_match -> None). FAIL-CLOSED: catch ImportError ONLY; a runtime
error from a deployed MT layer propagates rather than silently degrading to see-all.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Viewer:
    """Fallback viewer used only when the MT layer is absent — see-all (is_local_admin)."""
    user_id: int = 0
    tenant_id: int = 0
    is_tenant_admin: bool = False
    is_local_admin: bool = True


@dataclass(frozen=True)
class MTConfig:
    enabled: bool = False
    mode: str = "shared"
    default_visibility: str = ""
    local_admins_manage_all_tenants: bool = True


def multitenancy_config():
    try:
        from lib.cuckoo.common.tenancy import multitenancy_config as real
    except ImportError:
        return MTConfig()
    return real()


def viewer_for(user):
    try:
        from lib.cuckoo.common.tenancy import viewer_for as real
    except ImportError:
        return Viewer()
    return real(user)


def scope_match(scope, viewer):
    try:
        from lib.cuckoo.common.tenancy import scope_match as real
    except ImportError:
        return None
    return real(scope, viewer)
