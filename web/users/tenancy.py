"""Bridge Django request.user -> the pure core predicate (lib.cuckoo.common.tenancy).

The web/apiv2 layers call can_view_task / can_toggle_task; the actual policy lives
in the framework-neutral predicate so the broker can reuse it unchanged.
"""
from lib.cuckoo.common.tenancy import Viewer, Job, can_read, can_toggle, multitenancy_config


def viewer_for(user) -> Viewer:
    """Build a predicate Viewer from a Django user, resolving the operator
    break-glass: when local_admins_manage_all_tenants is on, ANY superuser
    crosses tenants; when off, only IdP-provisioned superusers (those with a
    linked allauth SocialAccount) do — a local createsuperuser does not.
    """
    cfg = multitenancy_config()
    is_super = bool(getattr(user, "is_superuser", False))
    if not cfg.enabled:
        # Multitenancy off => legacy single-tenant behavior for EVERY principal,
        # INCLUDING anonymous (a no-auth public install, or apiv2 with token-auth
        # disabled => DRF AllowAny): see and manage everything, exactly like
        # upstream. is_local_admin short-circuits the predicate and the list
        # filter; existing/legacy tasks are NOT hidden. This MUST run BEFORE the
        # is_authenticated check, or anonymous requests on a disabled install get
        # is_local_admin=False and every can_read/visible_to guard denies the
        # private-default tasks upstream served (back-compat regression).
        return Viewer(user_id=getattr(user, "id", None), tenant_id=None, is_superuser=is_super,
                      is_tenant_admin=False, is_local_admin=True)

    if not getattr(user, "is_authenticated", False):
        # MT enabled: an anonymous request stays public-only (no break-glass).
        return Viewer(user_id=None, tenant_id=None)

    prof = getattr(user, "userprofile", None)
    if not is_super:
        is_local = False
    elif cfg.local_admins_manage_all_tenants:
        is_local = True
    else:
        # flag off -> force admin access through the IdP: only superusers with a
        # SocialAccount (IdP-provisioned) keep cross-tenant reach.
        try:
            is_local = user.socialaccount_set.exists()
        except Exception:
            is_local = False
    return Viewer(
        user_id=user.id,
        tenant_id=getattr(prof, "tenant_id", None),
        is_superuser=is_super,
        is_tenant_admin=bool(getattr(prof, "is_tenant_admin", False)),
        is_local_admin=is_local,
    )


def _job_for(task) -> Job:
    return Job(
        owner_id=getattr(task, "user_id", None),
        tenant_id=getattr(task, "tenant_id", None),
        visibility=getattr(task, "visibility", "private"),
    )


def can_view_task(user, task) -> bool:
    return can_read(viewer_for(user), _job_for(task))


def can_toggle_task(user, task) -> bool:
    return can_toggle(viewer_for(user), _job_for(task))


def can_manage_task(user, task) -> bool:
    """Authorize a mutation (delete/reschedule/reprocess/comment/remove) on a
    task. Same policy as toggling visibility: owner, tenant-admin for the
    tenant's public/tenant jobs, or break-glass superuser — never another
    member's private job."""
    return can_toggle(viewer_for(user), _job_for(task))


def submission_scope(request):
    """Resolve (tenant_id, visibility) for a new submission from the request.

    Tenant comes from the submitting user; visibility is the explicit
    ``visibility`` param when valid, else the per-mode default. Raises
    ValueError on an invalid explicit visibility so the view can 400.
    """
    from lib.cuckoo.common.tenancy import multitenancy_config, default_visibility, VISIBILITIES

    v = viewer_for(request.user)
    data = getattr(request, "data", None)
    if data is None:
        data = getattr(request, "POST", None) or {}
    requested = data.get("visibility") if hasattr(data, "get") else None
    if requested:
        if requested not in VISIBILITIES:
            raise ValueError("invalid visibility")
        visibility = requested
    else:
        visibility = default_visibility(multitenancy_config())
    return v.tenant_id, visibility
