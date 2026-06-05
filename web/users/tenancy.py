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
    if not getattr(user, "is_authenticated", False):
        return Viewer(user_id=None, tenant_id=None)

    prof = getattr(user, "userprofile", None)
    cfg = multitenancy_config()
    is_super = bool(getattr(user, "is_superuser", False))
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
