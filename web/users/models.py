import logging

from django.contrib.auth.models import User
from django.db import models, transaction
from django.db.models.signals import m2m_changed, post_save
from django.dispatch import receiver

log = logging.getLogger(__name__)

# How many recompute/stamp passes _restamp_one_tenant will make before giving up on convergence.
# >1 exists purely to settle races between concurrent admin saves; see the docstring there.
_RESTAMP_MAX_PASSES = 3

# "no ACL stamped yet", distinct from a stamped None (== unrestricted) and from a stamped "" (==
# deny-all). Both of those are legitimate values that must not be mistaken for "already converged".
_NOT_STAMPED = object()


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


def _acl_csv(tenant_id):
    """The tenant's current exit ACL as the Task.allowed_exits wire format (CSV, or None ==
    unrestricted). Resolved through the SAME resolver the submit path uses, so the re-stamp cannot
    drift from what a fresh submission would compute (globals U assigned, active only)."""
    from users.tenancy import allowed_exit_slugs

    slugs = allowed_exit_slugs(_TenantViewer(tenant_id))
    return ",".join(sorted(slugs)) if slugs is not None else None


def _rollback_quietly(db):
    """Return the task-store session to a clean, reusable state after a failed re-stamp.

    rollback() (not remove()) is deliberate: db.session is a process-wide scoped_session shared with
    every other caller on this thread, so removing it would close a Session that an outer frame may
    still be holding. A rollback discards our failed UPDATE, ends the implicit transaction and
    releases the pooled connection, which is all this function owes the next caller.

    Reaching db.session can itself raise -- Database() is a proxy whose __getattr__ raises
    CuckooDatabaseInitializationError until init_database() has run -- which is exactly how the
    previous version of this code leaked an exception out of its own error handler.
    """
    try:
        db.session.rollback()
    except Exception:
        log.debug("task-store session rollback failed after a failed exit re-stamp", exc_info=True)


def _restamp_one_tenant(db, tenant_id):
    """Stamp one tenant's current ACL onto its PENDING tasks, converging under concurrent writers.

    Idempotent by construction: restamp_pending_allowed_exits is an unconditional
    ``UPDATE ... WHERE tenant_id=? AND status=PENDING SET allowed_exits=?``, so replaying it with the
    same ACL is a no-op and the duplicate callbacks that ``exits.set()`` produces (post_remove +
    post_add) cost one redundant UPDATE, nothing more.

    The extra passes exist for concurrency. Two admins saving different exit changes for the same
    tenant each queue their own callback; whichever lands LAST decides what the queue sees, and a
    callback that computed its CSV before the other transaction committed would stamp a stale set --
    in the revoke direction that silently re-grants a revoked exit to queued tasks, the very bug this
    receiver exists to prevent. So after stamping we recompute: if the committed ACL still matches
    what we wrote, we are done; if another writer moved it, we stamp again. Both racers therefore
    settle on the same final ACL regardless of callback order.
    """
    written = _NOT_STAMPED
    for _ in range(_RESTAMP_MAX_PASSES):
        csv = _acl_csv(tenant_id)
        if csv == written:
            return  # committed ACL matches what we stamped -- converged
        try:
            db.restamp_pending_allowed_exits(tenant_id, csv)
            db.session.commit()
        except Exception:
            _rollback_quietly(db)
            # Never let a task-store failure break the admin save; the ACL is still enforced at the
            # worker for every FUTURE task, and the stale pending rows are logged here.
            log.exception(
                "failed to re-stamp pending tasks for tenant %s after an exit assignment change; "
                "queued tasks keep their previous allowed_exits until they run", tenant_id,
            )
            return
        written = csv
    log.warning(
        "exit ACL for tenant %s changed under every re-stamp pass; queued tasks carry %r, which may "
        "already be stale. A concurrent admin save will re-stamp them.", tenant_id, written,
    )


def _restamp_pending_tasks(tenant_ids):
    """Cross-store write: push the exit ACL from the Django user store onto queued rows in the
    SQLAlchemy analysis task store. Runs post-commit, and MUST NOT raise.

    DELIBERATE cross-store write. The ACL is snapshotted onto Task.allowed_exits at submit time
    because the worker guard has to be able to fail closed on the task row alone, without a live
    round trip to the UI tier. The alternative -- have the worker re-derive the ACL at route time --
    was rejected: workers are vanilla/central-mode nodes with no access to the Django user store, and
    making egress authorization depend on a synchronous call to the control plane turns a UI outage
    into either an egress outage or (worse) a fail-open. Snapshotting keeps the guard local, and this
    function is the price: the snapshot needs invalidating when the ACL changes.

    If this is ever upstreamed, extract it behind a small interface (e.g. an ``ExitAclSink`` with a
    single ``restamp(tenant_id, allowed_exits)`` method, resolved from config) so the Django app
    depends on a port rather than reaching directly into lib.cuckoo.core.database. The behaviour
    below is the contract that interface would have to keep; the import location is not.

    ``tenant_ids`` is a concrete tuple of tenant pks, or None meaning "every tenant" (the reverse
    post_clear case, where Django has already dropped the rows and the affected set is unknowable).
    """
    try:
        from lib.cuckoo.core.database import Database

        # Database() is a proxy; it does not connect here, but any attribute access raises
        # CuckooDatabaseInitializationError if init_database() never ran (a web tier booted without
        # the task store). Construct and probe it inside the guard.
        db = Database()

        ids = tuple(Tenant.objects.values_list("id", flat=True)) if tenant_ids is None else tenant_ids
        for tenant_id in ids:
            _restamp_one_tenant(db, tenant_id)
    except Exception:
        # Backstop. This runs from a transaction.on_commit callback, where an escaping exception
        # surfaces as a 500 on an admin save whose changes are ALREADY committed -- the operator
        # would see a failure page for a change that actually took effect. Log and swallow instead.
        log.exception(
            "failed to re-stamp pending tasks after an exit assignment change (tenants=%r); queued "
            "tasks keep their previous allowed_exits until they run", tenant_ids,
        )


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

    The write itself is deferred to transaction.on_commit, for two reasons. (1) Correctness: the
    admin wraps the change form in an atomic block, so resolving and stamping inline would publish an
    ACL to the task store that a subsequent rollback erases from Django -- a phantom ACL no admin ever
    saved. Post-commit, we only ever propagate changes that really landed, and we read the COMMITTED
    ACL rather than our own uncommitted view. (2) Blast radius: a slow or wedged task store no longer
    holds the admin's row locks open, and a task-store failure cannot roll the admin change back,
    because by then there is nothing left to roll back. Outside an atomic block (management command,
    API) on_commit runs the callback immediately, so non-admin callers keep synchronous behaviour.
    """
    if action not in ("post_add", "post_remove", "post_clear"):
        return

    from lib.cuckoo.common.tenancy import multitenancy_config

    if not multitenancy_config().enabled:
        return

    if reverse:
        # instance is an Exit; the affected tenants are the pk_set (or, for a clear, unknowable).
        # Capture pks now -- pk_set only exists in this kwargs -- and resolve "all tenants" later,
        # post-commit, so the fallback sees the committed tenant list.
        pks = kwargs.get("pk_set") or None
        tenant_ids = tuple(sorted(pks)) if pks else None
    else:
        tenant_ids = (instance.pk,)

    transaction.on_commit(lambda: _restamp_pending_tasks(tenant_ids))
