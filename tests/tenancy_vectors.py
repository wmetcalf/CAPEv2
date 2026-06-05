"""Canonical visibility test-vectors — the single source of truth shared by the
CAPE predicate (lib/cuckoo/common/tenancy.py) and, later, the broker's DynamoDB
reimplementation. Each case: a viewer + a job + expected read/toggle outcome.

Viewer/job tenants are small ints; None means "no tenant".
"""

# Visibility levels
PUBLIC, TENANT, PRIVATE = "public", "tenant", "private"

# Each vector: (label, viewer, job, can_read, can_toggle)
#   viewer = dict(user_id, tenant_id, is_superuser, is_tenant_admin, is_local_admin)
#   job    = dict(owner_id, tenant_id, visibility)
#   is_local_admin = the cuckoo.conf local_admins_manage_all_tenants gate already
#                    resolved for this viewer (True only when flag on AND superuser).
VECTORS = [
    # --- public: everyone reads ---
    ("public/anon",      dict(user_id=None, tenant_id=None, is_superuser=False, is_tenant_admin=False, is_local_admin=False),
                         dict(owner_id=1, tenant_id=10, visibility=PUBLIC), True,  False),
    ("public/other",     dict(user_id=2, tenant_id=20, is_superuser=False, is_tenant_admin=False, is_local_admin=False),
                         dict(owner_id=1, tenant_id=10, visibility=PUBLIC), True,  False),
    # --- tenant: only same-tenant members read ---
    ("tenant/same",      dict(user_id=2, tenant_id=10, is_superuser=False, is_tenant_admin=False, is_local_admin=False),
                         dict(owner_id=1, tenant_id=10, visibility=TENANT), True,  False),
    ("tenant/other",     dict(user_id=2, tenant_id=20, is_superuser=False, is_tenant_admin=False, is_local_admin=False),
                         dict(owner_id=1, tenant_id=10, visibility=TENANT), False, False),
    ("tenant/null-job",  dict(user_id=2, tenant_id=None, is_superuser=False, is_tenant_admin=False, is_local_admin=False),
                         dict(owner_id=1, tenant_id=None, visibility=TENANT), False, False),  # null tenant != "everyone"
    # --- private: only owner ---
    ("private/owner",    dict(user_id=1, tenant_id=10, is_superuser=False, is_tenant_admin=False, is_local_admin=False),
                         dict(owner_id=1, tenant_id=10, visibility=PRIVATE), True,  True),
    ("private/teammate", dict(user_id=2, tenant_id=10, is_superuser=False, is_tenant_admin=False, is_local_admin=False),
                         dict(owner_id=1, tenant_id=10, visibility=PRIVATE), False, False),
    ("private/tadmin",   dict(user_id=2, tenant_id=10, is_superuser=False, is_tenant_admin=True,  is_local_admin=False),
                         dict(owner_id=1, tenant_id=10, visibility=PRIVATE), False, False),  # tenant-admin can't reach private
    # --- tenant-admin: manages (toggles) public/tenant jobs in own tenant, no extra read ---
    ("tadmin/toggle-tenant", dict(user_id=2, tenant_id=10, is_superuser=False, is_tenant_admin=True, is_local_admin=False),
                         dict(owner_id=1, tenant_id=10, visibility=TENANT), True,  True),
    ("tadmin/other-tenant",  dict(user_id=2, tenant_id=20, is_superuser=False, is_tenant_admin=True, is_local_admin=False),
                         dict(owner_id=1, tenant_id=10, visibility=TENANT), False, False),
    # --- owner always reads + toggles own ---
    ("owner/tenant-job", dict(user_id=1, tenant_id=10, is_superuser=False, is_tenant_admin=False, is_local_admin=False),
                         dict(owner_id=1, tenant_id=10, visibility=TENANT), True,  True),
    # --- superuser break-glass (local_admins_manage_all_tenants resolved into is_local_admin) ---
    ("breakglass/read",  dict(user_id=9, tenant_id=None, is_superuser=True, is_tenant_admin=False, is_local_admin=True),
                         dict(owner_id=1, tenant_id=10, visibility=PRIVATE), True,  True),
    ("breakglass/off",   dict(user_id=9, tenant_id=None, is_superuser=True, is_tenant_admin=False, is_local_admin=False),
                         dict(owner_id=1, tenant_id=10, visibility=PRIVATE), False, False),  # flag off => no cross-owner reach
]
