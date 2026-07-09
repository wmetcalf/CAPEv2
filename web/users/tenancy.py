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


def can_view_sample(user, *, sha256=None, sha1=None, md5=None, sample_id=None) -> bool:
    """True iff `user` may access a content-addressed sample identified by
    hash/id — i.e. has >=1 VISIBLE task referencing it. Samples are shared across
    tenants by sha256, so access follows the union of the viewer's visible tasks
    (the same intended boundary apiv2 _deny_by_hash enforces).

    No-op (returns True) when multitenancy is disabled or for a break-glass admin
    (viewer_for -> is_local_admin). THE single source of truth for every by-hash
    surface (apiv2 _deny_by_hash, web file() sample/static, submission resubmit /
    download-services) so they cannot drift apart.
    """
    viewer = viewer_for(user)
    if viewer.is_local_admin:
        return True
    from lib.cuckoo.core.database import Database

    db = Database()
    if sample_id is not None:
        sample = db.view_sample(sample_id)
    elif sha256 or sha1 or md5:
        sample = db.find_sample(sha256=sha256, sha1=sha1, md5=md5)
    else:
        sample = None
    if sample is None:
        return False
    return bool(db.list_tasks(sample_id=sample.id, visible_to=viewer, limit=1))


def can_ban_user(actor, target_user_id) -> bool:
    """May `actor` ban/disable user `target_user_id` (and their pending tasks)?

    - GLOBAL break-glass: a staff/superuser bans anyone, any tenant (preserves the
      original is_staff/is_superuser gate AND single-node behavior — when MT is off
      this is the ONLY path, since viewer_for would otherwise mark every principal
      is_local_admin).
    - TENANT delegation: a tenant-admin bans ONLY a target user in their OWN tenant.
      Enforced here (the target's UserProfile.tenant_id must equal the actor's), so a
      tenant-admin can't ban across tenants even with a forged user_id.
    - Everyone else: denied.
    """
    if getattr(actor, "is_staff", False) or getattr(actor, "is_superuser", False):
        return True
    v = viewer_for(actor)
    if not (v.is_tenant_admin and v.tenant_id is not None):
        return False
    from django.contrib.auth.models import User

    try:
        prof = User.objects.select_related("userprofile").get(pk=int(target_user_id)).userprofile
    except Exception:
        return False
    return getattr(prof, "tenant_id", None) == v.tenant_id


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


def allowed_exit_slugs(viewer):
    """The set of egress exit slugs a viewer's tenant may use: global exits ∪ the tenant's
    assigned exits (active only). Returns None (== UNRESTRICTED, today's behavior) when MT is
    disabled, in shared mode, or the caller is a local admin — the SAME no-op conditions as
    viewer_scope_match. THE single source of truth for the submit filter/validation and the
    worker guard, so they cannot drift."""
    from users.models import Exit

    cfg = multitenancy_config()
    if not cfg.enabled or cfg.mode != "locked" or getattr(viewer, "is_local_admin", False):
        return None
    active = Exit.objects.filter(active=True)
    slugs = set(active.filter(is_global=True).values_list("slug", flat=True))
    tid = getattr(viewer, "tenant_id", None)
    if tid is not None:
        slugs |= set(active.filter(tenants__id=tid).values_list("slug", flat=True))
    return slugs
