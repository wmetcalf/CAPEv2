"""Pure, dependency-free job-visibility predicate — the single source of truth
for who may read/manage a task. Imported by the Django web layer, the apiv2
views, the SQLAlchemy task store, and (separately) validated by the broker.
No Django, no SQLAlchemy imports here — only plain dataclasses so it stays a
pure function set testable against tests/tenancy_vectors.py.
"""
from dataclasses import dataclass
from typing import Optional

PUBLIC, TENANT, PRIVATE = "public", "tenant", "private"
VISIBILITIES = (PUBLIC, TENANT, PRIVATE)


@dataclass(frozen=True)
class Viewer:
    user_id: Optional[int]
    tenant_id: Optional[int]
    is_superuser: bool = False
    is_tenant_admin: bool = False
    is_local_admin: bool = False  # superuser AND cuckoo.conf break-glass flag on


@dataclass(frozen=True)
class Job:
    owner_id: Optional[int]
    tenant_id: Optional[int]
    visibility: str


def _is_owner(v: Viewer, j: Job) -> bool:
    return v.user_id is not None and v.user_id == j.owner_id


def _same_tenant(v: Viewer, j: Job) -> bool:
    return j.tenant_id is not None and v.tenant_id == j.tenant_id


def can_read(v: Viewer, j: Job) -> bool:
    if j.visibility == PUBLIC:
        return True
    if v.is_local_admin:          # operator break-glass (gated upstream)
        return True
    if _is_owner(v, j):
        return True
    if j.visibility == TENANT and _same_tenant(v, j):
        return True
    return False                  # private => owner/break-glass only


def can_toggle(v: Viewer, j: Job) -> bool:
    if _is_owner(v, j):
        return True
    if v.is_local_admin:
        return True
    # tenant-admin manages public/tenant jobs in their own tenant, never private
    if v.is_tenant_admin and _same_tenant(v, j) and j.visibility in (PUBLIC, TENANT):
        return True
    return False


@dataclass(frozen=True)
class MTConfig:
    enabled: bool
    mode: str
    default_visibility: str
    local_admins_manage_all_tenants: bool


def _as_bool(v, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("yes", "true", "1", "on")
    if v is None:
        return default
    return bool(v)


def multitenancy_config() -> MTConfig:
    """Read the [multitenancy] section of cuckoo.conf (server-side policy)."""
    from lib.cuckoo.common.config import Config

    try:
        sec = Config("cuckoo").get("multitenancy")
    except Exception:
        sec = {}
    get = sec.get if hasattr(sec, "get") else (lambda k, d=None: d)
    return MTConfig(
        enabled=_as_bool(get("enabled", False), False),
        mode=str(get("mode", "shared") or "shared"),
        default_visibility=str(get("default_visibility", "") or ""),
        local_admins_manage_all_tenants=_as_bool(get("local_admins_manage_all_tenants", True), True),
    )


def default_visibility(cfg: MTConfig) -> str:
    """The submit-time default visibility for the configured mode."""
    if cfg.default_visibility in VISIBILITIES:
        return cfg.default_visibility
    return PUBLIC if cfg.mode == "shared" else TENANT
