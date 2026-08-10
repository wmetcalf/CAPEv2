import logging

from django.contrib.auth.models import User
from django.db import models
from django.db.models.signals import m2m_changed, post_save
from django.dispatch import receiver

log = logging.getLogger(__name__)


class Tenant(models.Model):
    """A customer/tenant — the isolation boundary jobs, rulesets and API keys
    belong to. Membership + tenant-admin status are driven by IdP group claims
    (see web/web/allauth_adapters.py); CAPE owns the row (hybrid model)."""

    slug = models.SlugField(max_length=48, unique=True)
    name = models.CharField(max_length=128)
    idp_groups = models.JSONField(default=list, blank=True)  # groups -> membership
    admin_idp_groups = models.JSONField(default=list, blank=True)  # groups -> tenant-admin
    active = models.BooleanField(default=True)
    # Central-store lifetime (days) for this tenant's analyses: how long S3 artifacts
    # + DocumentDB reports are retained. Separate from the ephemeral worker NVMe
    # cleanup. The central retention timer (UI node) stamps info.expire_at from this.
    retention_days = models.PositiveIntegerField(default=90)
    exits = models.ManyToManyField("Exit", blank=True, related_name="tenants")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.slug


class Exit(models.Model):
    """A named egress exit (a rooter [gwX] gateway). `slug` MUST equal the routing.conf
    [gwX] id deployed on the workers. is_global exits are usable by every tenant; others
    are usable only by the tenants they are assigned to (Tenant.exits)."""

    slug = models.SlugField(max_length=48, unique=True)
    name = models.CharField(max_length=128, blank=True)
    is_global = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.slug


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    subscription = models.CharField(max_length=50, default="5/m")
    reports = models.BooleanField(default=False)
    tenant = models.ForeignKey(
        "Tenant", null=True, blank=True, on_delete=models.SET_NULL, related_name="members"
    )
    is_tenant_admin = models.BooleanField(default=False)

    def __str__(self):
        return self.user.username


@receiver(post_save, sender=User)
def create_or_update_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.create(user=instance)
    if hasattr(instance, "userprofile"):
        instance.userprofile.save()


class _TenantViewer:
    """Minimal viewer for allowed_exit_slugs: a plain tenant member, never a local admin (which
    would resolve to None/unrestricted and wipe the stamp instead of narrowing it)."""

    is_local_admin = False

    def __init__(self, tenant_id):
        self.tenant_id = tenant_id


@receiver(m2m_changed, sender=Tenant.exits.through)
def restamp_pending_tasks_on_exit_change(sender, instance, action, reverse, **kwargs):
    """Push an exit-assignment change onto the tenant's ALREADY-QUEUED tasks.

    The ACL is snapshotted onto Task.allowed_exits at submit time, so without this a revocation only
    affected future submissions: anything already sitting in the queue kept the old allowed set and
    the worker honoured it, meaning a revoked exit stayed usable for as long as the backlog lasted.
    Re-stamp pending rows so revocation takes effect on the queue as well.

    Fires on add/remove/clear (an ADD must propagate too, or a newly-granted exit would be unusable
    by queued tasks for no reason). Runs for both directions of the M2M -- editing Tenant.exits in
    the Tenant admin, or Exit.tenants from the other side.
    """
    if action not in ("post_add", "post_remove", "post_clear"):
        return

    if reverse:
        # instance is an Exit; the affected tenants are the pk_set (or, for a clear, unknowable --
        # Django has already dropped the rows, so fall back to every tenant).
        pks = kwargs.get("pk_set") or None
        tenants = Tenant.objects.filter(pk__in=pks) if pks else Tenant.objects.all()
    else:
        tenants = [instance]

    from lib.cuckoo.common.tenancy import multitenancy_config

    if not multitenancy_config().enabled:
        return

    from lib.cuckoo.core.database import Database
    from users.tenancy import allowed_exit_slugs

    db = Database()
    for tenant in tenants:
        # Resolve through the SAME resolver the submit path uses, so the re-stamp cannot drift from
        # what a fresh submission would compute (globals U assigned, active only).
        slugs = allowed_exit_slugs(_TenantViewer(tenant.id))
        csv = ",".join(sorted(slugs)) if slugs is not None else None
        try:
            db.restamp_pending_allowed_exits(tenant.id, csv)
            db.session.commit()
        except Exception:
            db.session.rollback()
            # Never let a task-store failure break the admin save; the ACL is still enforced at the
            # worker for every FUTURE task, and the stale pending rows are logged here.
            log.exception(
                "failed to re-stamp pending tasks for tenant %s after an exit assignment change; "
                "queued tasks keep their previous allowed_exits until they run", tenant.id,
            )
