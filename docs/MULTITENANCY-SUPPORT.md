# Multitenancy — Reference, Authorization Model & Supported Boundaries

Multitenancy (the `[multitenancy]` section of `cuckoo.conf`) is an **opt-in, default-off**
layer that scopes every read surface so a user of one tenant cannot see or act on another
tenant's tasks, samples, reports, artifacts, statistics, search results, or live-VM
(Guacamole) sessions. It adds two SQL columns (`tasks.tenant_id`, `tasks.visibility`), two
Django models (`Tenant`, `UserProfile.tenant`/`is_tenant_admin`), a tenant stamp on the
mongo report (`info.tenant_id` / `info.user_id` / `info.visibility`), and a small pure
predicate module (`lib/cuckoo/common/tenancy.py`) that every gate in the web/apiv2/guac
layers routes through. With `enabled = no` (the default) the predicate short-circuits to
legacy see-all for **every** principal, including anonymous, so a single-tenant install
behaves exactly as upstream. This document is the reference for the model, the config keys,
the authorization matrix, the deliberate fail-closed behaviours, and which deployment
modes the isolation guarantee actually covers today.

## Contents

- [Concepts](#concepts)
- [Data model](#data-model)
- [Configuration reference](#configuration-reference)
- [How users get a tenant](#how-users-get-a-tenant)
- [Authorization matrix](#authorization-matrix)
- [Where scoping is applied](#where-scoping-is-applied)
- [The endpoint coverage gate](#the-endpoint-coverage-gate)
- [Fail-closed behaviour (and why)](#fail-closed-behaviour-and-why)
- [Enabling on an existing (populated) install](#enabling-on-an-existing-populated-install)
- [Supported (isolation enforced end-to-end)](#supported-isolation-enforced-end-to-end)
- [Not yet supported (fail-closed — safe, but limited)](#not-yet-supported-fail-closed--safe-but-limited)
- [Behavior notes](#behavior-notes)
- [Gotchas / operational notes](#gotchas--operational-notes)
- [Tests](#tests)

## Concepts

| Concept | Meaning | Where it lives |
| --- | --- | --- |
| **Tenant** | The isolation boundary. A customer/org that tasks belong to. | `web/users/models.py::Tenant` |
| **Tenant member** | A user whose `UserProfile.tenant` points at a Tenant. | `web/users/models.py::UserProfile.tenant` |
| **Tenant admin** | Elevated *within one tenant*: manages that tenant's public/tenant jobs, may delete its `tenant`-visibility jobs, may ban its plain members. Never reaches another member's `private` job. | `UserProfile.is_tenant_admin` |
| **Local (box) admin / break-glass** | Operator escape hatch: sees and manages everything across tenants. Resolved from `is_superuser` **plus** the `local_admins_manage_all_tenants` config gate — a superuser alone is not automatically break-glass. | `Viewer.is_local_admin`, resolved in `web/users/tenancy.py::viewer_for` |
| **Owner / submitter** | `tasks.user_id`. Always reads and manages their own task at any visibility. | `lib/cuckoo/common/tenancy.py::_is_owner` |
| **Visibility** | Per-task exposure: `public` \| `tenant` \| `private`. | `tasks.visibility`, `VISIBILITIES` |
| **Scope** | A read *slice* used by aggregate surfaces: `public` \| `tenant` \| `mine` \| `global`. | `SCOPES`, `scope_match()` |

The predicate module is deliberately dependency-free:

```python
# lib/cuckoo/common/tenancy.py
"""Pure, dependency-free job-visibility predicate — the single source of truth
for who may read/manage a task. Imported by the Django web layer, the apiv2
views, the SQLAlchemy task store, and (separately) validated by the broker.
No Django, no SQLAlchemy imports here — only plain dataclasses so it stays a
pure function set testable against tests/tenancy_vectors.py.
"""

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
```

## Data model

**SQL (`tasks`)** — `lib/cuckoo/core/data/task.py`:

```python
user_id:   Mapped[Optional[int]] = mapped_column(nullable=True)
tenant_id: Mapped[Optional[int]] = mapped_column(nullable=True, index=True)
visibility: Mapped[str]          = mapped_column(String(16), nullable=False, server_default="private")
```

Created by Alembic revision `3a1b_tenant_visibility`
(`utils/db_migration/versions/3_add_tenant_visibility.py`, revises `2b3c4d5e6f7g`), which
also creates `ix_tasks_tenant_id`. `SCHEMA_VERSION = "3a1b_tenant_visibility"` in
`lib/cuckoo/core/database.py`. Fresh installs build the schema from the ORM
(`Base.metadata.create_all()`), which is why the model declares `index=True` with the same
default index name the migration creates — the two provisioning paths must not diverge.

**Django (`users` app)** — `web/users/models.py`:

```python
class Tenant(models.Model):
    slug = models.SlugField(max_length=48, unique=True)
    name = models.CharField(max_length=128)
    idp_groups = models.JSONField(default=list, blank=True)        # groups -> membership
    admin_idp_groups = models.JSONField(default=list, blank=True)  # groups -> tenant-admin
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)


class UserProfile(models.Model):
    ...
    tenant = models.ForeignKey("Tenant", null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="members")
    is_tenant_admin = models.BooleanField(default=False)
```

Migration: `web/users/migrations/0004_tenant_userprofile_is_tenant_admin_and_more.py`.

**Mongo (report `analysis` doc)** — the reporter stamps `info.tenant_id`,
`info.user_id`, `info.visibility` (`modules/reporting/mongodb.py::stamp_tenant_info`) and
creates a compound index when MT is enabled:

```python
mongo_create_index("analysis",
    [("info.tenant_id", 1), ("info.visibility", 1), ("info.user_id", 1)],
    background=True, name="tenant_scope_idx")
```

**The `users` app is import-optional.** `web/web/settings.py::_users_app_present()` drops
`users` from `INSTALLED_APPS` when `web/users/` isn't on disk, and the
`tenancy_optional` facades key off the *same* import-availability signal so there is one
source of truth. To run single-tenant with the files present, use
`[multitenancy] enabled = no` — do not delete the app.

## Configuration reference

### `[multitenancy]` in `conf/cuckoo.conf` (defaults from `conf/default/cuckoo.conf.default`)

| Key | Default | Values | Meaning |
| --- | --- | --- | --- |
| `enabled` | `no` | bool (`yes/true/1/on`) | Master switch. Off = legacy single-tenant behaviour for every principal (including anonymous). |
| `mode` | `shared` | `shared` \| `locked` | Only affects the **submit-time default visibility** (`shared` → `public`, `locked` → `tenant`). An unknown/typo value fails closed to `locked`. Read *scoping* is mode-independent. |
| `default_visibility` | *(blank)* | blank \| `public` \| `tenant` \| `private` | Blank = per-mode default. An explicitly-set unrecognized value fails closed to `private`. Value is whitespace/case-normalized. |
| `local_admins_manage_all_tenants` | `yes` | bool | Operator break-glass. `yes` → any local Django superuser is `is_local_admin` (cross-tenant read + management). `no` → only superusers with a linked IdP `SocialAccount` keep cross-tenant reach; a plain `createsuperuser` does not. Server-side only — a tenant/user cannot grant themselves this. |

```ini
[multitenancy]
enabled = no
# shared = tenant-less collaborative pool (submit default public);
# locked = per-tenant isolation (submit default tenant).
mode = shared
# Blank = per-mode default (shared->public, locked->tenant). Override with one
# of: public | tenant | private.
default_visibility =
# Operator break-glass: local Django superusers manage ALL tenants (cross-tenant
# read + management) when yes. Set no to force admin access through the IdP.
# Server-side only — a tenant/user cannot grant themselves this.
local_admins_manage_all_tenants = yes
```

`mode` is consumed in exactly one place — `default_visibility(cfg)`:

```python
def default_visibility(cfg: MTConfig) -> str:
    """The submit-time default visibility for the configured mode."""
    if cfg.default_visibility in VISIBILITIES:
        return cfg.default_visibility
    if cfg.default_visibility:
        log.warning("default_visibility %r unrecognized; failing closed to private", ...)
        return PRIVATE
    return PUBLIC if cfg.mode == "shared" else TENANT
```

`shared` does **not** mean see-all: `viewer_scope_match` / `can_read` / the SQL
`visible_to` filter are all mode-independent and still hide explicitly-private and
other-tenant `tenant` analyses. Only `PUBLIC` is the shared pool.

### Related keys MT interacts with

| Key | File / section | Why MT cares |
| --- | --- | --- |
| `groups_claim` | `web.conf` `[oauth_oidc]` (default `groups`) | The ID-token/userinfo claim `reconcile_tenant` reads to map a user to a `Tenant`. |
| `admin_groups`, `superadmin_groups` | `web.conf` `[oauth_oidc]` | Map IdP groups to `is_staff` / `is_superuser`. `is_superuser` is the input to break-glass resolution. |
| `required_groups` | `web.conf` `[oauth_oidc]` | Only users in one of these groups get a CAPE account provisioned at all. |
| `mongodb.enabled` | `reporting.conf` `[mongodb]` | MT's aggregate/search/statistics scoping reads the mongo tenant stamp; the visibility toggle only syncs mongo. |
| `central_database_url` | `cuckoo.conf` `[central_mode]` | Set on **workers** in a central + MT deployment so a worker can resolve a central task's tenancy. Blank ⇒ central-mode analyses stamp fail-closed. |
| `[centralstore] enabled` | `reporting.conf` | Required under central + MT: it stamps the globally-unique `ui-<central_id>` `job_id` the tenant-scoped filters key on. |
| `control_plane_token` | `api.conf` `[api]` | Machine-to-machine secret for the one deliberately un-tenant-scoped endpoint (`tasks_machine`). Blank disables that path (fail-closed). |

## How users get a tenant

### 1. Via IdP groups (any OIDC-compliant provider)

`web/web/allauth_adapters.py::reconcile_tenant(user, user_groups)` is the single mapping
function. It runs on every SSO login from the `user_logged_in` receiver
(`_reconcile_sso_user_on_login`), not just at first provisioning — so a group change in the
IdP takes effect on the next sign-in.

```python
matches = [t for t in Tenant.objects.filter(active=True) if user_groups & _g(t.idp_groups)]
prof, _ = UserProfile.objects.get_or_create(user=user)
if len(matches) == 1:
    t = matches[0]
    new_tenant, new_admin = t, bool(user_groups & _g(t.admin_idp_groups))
else:
    if len(matches) > 1:
        log.warning("user %s matches multiple tenants %s; leaving tenant unset", ...)
    new_tenant, new_admin = None, False
```

Rules encoded there and in `web/users/test_tenancy.py`:

- **One tenant per user (v1).** Exactly one `idp_groups` match is expected. `>1` match →
  fail closed (tenant unset + warning). `0` matches → no tenant.
- `admin_idp_groups` membership sets `is_tenant_admin`; losing it demotes on next login.
- **Absent-claim guard.** If the configured `groups_claim` is not present in the token at
  all (scope/claim-mapping misconfiguration), tenant reconciliation is *skipped* rather
  than silently unsetting everyone's tenant. A present-but-empty claim is honoured
  (the user really is in no groups → demote).
- Claims are normalized first (`_claims`): allauth's `openid_connect` provider nests
  claims under `extra_data["userinfo"]`, so a raw-wrapper check would treat the groups
  claim as absent and skip reconciliation entirely.
- `Tenant.idp_groups` is a `JSONField`, so it is defensively normalized (`_g`): a bare
  string is **one** group (not iterated character-by-character), lists are filtered to
  hashable strings, anything else fails closed to the empty set.
- Matching is done in Python, not with an `idp_groups__contains` query, because the Django
  auth DB may be sqlite (`supports_json_field_contains=False` → `NotSupportedError`).
- The profile is only written when something actually changed (no `UPDATE` per login).

Example tenant row:

```python
Tenant.objects.create(
    slug="acme", name="Acme",
    idp_groups=["acme-soc"],          # membership
    admin_idp_groups=["acme-admins"], # tenant-admin
)
```

### 2. Purely-local users (no IdP)

Set `UserProfile.tenant` / `UserProfile.is_tenant_admin` in the Django admin (the
`ProfileInline` on the user page). `web/users/admin.py` restricts the tenancy-privilege
fields to superusers via `get_readonly_fields`, so a delegated `change_userprofile` /
`change_tenant` grant cannot be used to escalate:

```python
_TENANT_PRIV_FIELDS  = ("idp_groups", "admin_idp_groups")
_PROFILE_PRIV_FIELDS = ("tenant", "is_tenant_admin")
```

`UserProfile` rows are auto-created by a `post_save` signal on `User`, so a locally-created
user already has a profile to assign a tenant to.

### 3. Deactivating a tenant

Setting `Tenant.active = False` drops the tenant from the viewer **immediately** — it does
not wait for the next SSO login (`viewer_for` re-checks `prof.tenant.active` and clears
both `tenant_id` and `is_tenant_admin`).

## Authorization matrix

Four predicates in `lib/cuckoo/common/tenancy.py`, bridged to Django in
`web/users/tenancy.py` (`can_view_task`, `can_manage_task`, `can_delete_task`,
`can_set_visibility_task`, plus `can_delete_job` for an already-resolved viewer).

| Predicate | Used for | Authorized principals |
| --- | --- | --- |
| `can_read` | any read of a task/report/artifact | anyone if `public`; owner; same-tenant member if `tenant`; break-glass |
| `can_toggle` (= `can_manage_task`) | reversible mutations: reschedule, reprocess, comment, tag, VT upload, live-VM session mint | owner; break-glass; tenant-admin for `public`/`tenant` jobs **in their own tenant** |
| `can_set_visibility` | a visibility *transition* | `can_toggle`, **minus** the tenant-admin-only path making a `PUBLIC` job more restrictive |
| `can_delete` | irreversible task DELETE | owner; break-glass; tenant-admin **only** for `tenant`-visibility jobs in their own tenant |

Read/toggle/delete outcomes, from the canonical vectors in `tests/tenancy_vectors.py`
(`VECTORS` is shared by the CAPE predicate and, by design, reusable by the broker's
reimplementation):

| Viewer ↓ / Job → | `public` (other's) | `tenant`, same tenant | `tenant`, other tenant | `private`, own | `private`, other's |
| --- | --- | --- | --- | --- | --- |
| anonymous | read | — | — | n/a | — |
| plain member | read | read | — | read/toggle/delete | — |
| tenant admin (same tenant) | read, **toggle**, *no delete* | read, toggle, delete | — | read/toggle/delete | — |
| tenant admin (other tenant) | read | — | — | read/toggle/delete | — |
| break-glass (`is_local_admin`) | read, toggle, delete | read, toggle, delete | read, toggle, delete | read, toggle, delete | read, toggle, delete |
| superuser, break-glass flag **off**, no IdP link | read | (only if same tenant) | — | read/toggle/delete | — |

Two boundaries worth internalizing, both with the reasoning in the code:

```python
def can_delete(v: Viewer, j: Job) -> bool:
    """Authorize an irreversible task DELETE. Stricter than can_toggle for PUBLIC jobs: a public
    task is a shared/instance resource, so only its ORIGINAL SUBMITTER or a break-glass box admin
    may delete it -- a tenant-admin may toggle/manage a public job in their tenant but NOT delete
    it. ..."""
```

```python
def can_set_visibility(v: Viewer, j: Job, new_visibility: str) -> bool:
    """... a principal authorized ONLY by the tenant-admin path (not the owner, not a break-glass box
    admin) may not make a PUBLIC job MORE restrictive (public -> tenant/private). Without this, a
    tenant-admin -- who can_delete deliberately bars from deleting a public job -- could flip it to
    'tenant' (can_toggle allows that) and then delete it via can_delete's tenant branch. ..."""
```

### Live-VM (Guacamole) sessions are a *manage* action

Minting a Guacamole session (`web/submission/views.py::remote_session`, `status`) and the
WebSocket tunnel re-check (`web/guac/consumers.py`) both gate on `can_manage_task`, not
`can_read`: keyboard/mouse/framebuffer control of a running VM is a task action, so a
read-only viewer of a public/tenant task cannot tunnel into someone else's VM.

### `can_ban_user`

`web/users/tenancy.py::can_ban_user` carries the whole boundary itself (the `ban_user` /
`ban_all_user_tasks` views gate solely on it):

- **MT disabled** → upstream's `is_staff or is_superuser` check, applied *before* consulting
  the viewer (otherwise the MT-off see-all `is_local_admin` would authorize anyone,
  including anonymous).
- **MT enabled** → break-glass bans anyone; a tenant admin bans only plain members of their
  **own** tenant — not a `is_superuser`/`is_staff` target (privilege inversion), not
  themselves; nobody else. Any resolution error denies.

## Where scoping is applied

### SQL (`lib/cuckoo/core/data/`)

`visible_to=<Viewer>` is the read filter, mirroring `can_read`; break-glass skips it entirely.

```python
if visible_to is not None and not visible_to.is_local_admin:
    from lib.cuckoo.common.tenancy import PUBLIC, TENANT
    conds = [Task.visibility == PUBLIC]
    if visible_to.user_id is not None:
        conds.append(Task.user_id == visible_to.user_id)  # owner
    if visible_to.tenant_id is not None:
        conds.append(and_(Task.visibility == TENANT, Task.tenant_id == visible_to.tenant_id))
    stmt = stmt.where(or_(*conds))
```

| Function | File |
| --- | --- |
| `list_tasks(..., visible_to=)` | `data/tasking.py` |
| `count_matching_tasks(..., visible_to=)` | `data/tasking.py` |
| `get_tasks_status_count(scope=, viewer=, visible_to=)` | `data/tasking.py` |
| `count_tasks(scope=, viewer=)`, `minmax_tasks(scope=, viewer=)` via `_scope_where` | `data/tasking.py` |
| `count_samples(scope=, viewer=)`, `check_file_uniq(..., visible_to=)`, `sample_path_by_hash(..., visible_to=)` | `data/samples.py` |
| `set_task_visibility(task_id, visibility, expected_prior=)` | `data/tasking.py` |

`_scope_where` mirrors `scope_match` and returns `[Task.id == -1]` (match nothing) for a
tenant-less/user-less viewer — never an unscoped result.

### Mongo

| Helper | Returns | Used by |
| --- | --- | --- |
| `scope_match(scope, viewer)` | `$match` for one scope; `{"info.id": -1}` when the viewer can't satisfy it | statistics per-scope panels |
| `viewer_scope_match(viewer)` | `$or` of public/own-tenant/mine, or `None` (MT off / break-glass); `{"info.id": -1}` when no scope resolves | `perform_search`, dedup/search builders in `web_utils` / `cape_utils` |
| `entitled_scopes(user)` / `entitled_scope_filter(user)` | scope panel list / combined `$match` | `web/dashboard/views.py`, apiv2 + analysis `statistics_data` |
| `viewer_scope(user)` | central-mode facade over `entitled_scope_filter` | `web/analysis/central_scope.py`, `hunt()`, central artifact staging/feeds |

### Elasticsearch

`viewer_scope_es_filter(viewer)` is the bool-filter analogue and **is** wired into both of
`perform_search`'s ES branches (the mongo+ES `essearch` multi-match branch and the
`es_as_db` branch) and into `top_detections`' ES aggregation
(`lib/cuckoo/common/web_utils.py`) — but ES coverage stops there; see
[Not yet supported](#not-yet-supported-fail-closed--safe-but-limited).

### View-layer guards

| Layer | Guard | Notes |
| --- | --- | --- |
| `web/analysis/views.py` | `@require_task_visibility`, `@require_task_manage`, `@require_task_delete` | All three are a **true no-op** when MT is disabled (pass straight through, so upstream rendering is byte-identical). Each resolves `task_id`/`analysis_number` and runs `_coerce_task_id`. |
| `web/apiv2/views.py` | `_deny_if_hidden`, `_deny_task`, `_deny_manage`, `_deny_by_hash` | Generic 404 for hidden **and** missing tasks under MT (no id enumeration). With MT off, missing-task handling defers to the caller so upstream responses are preserved. |
| by-hash surfaces | `users.tenancy.can_view_sample` | Single source of truth for apiv2 `_deny_by_hash`, web `file()` sample/static, submission resubmit / download-services. A sample is globally hash-deduplicated, so access follows the union of the viewer's visible tasks. |
| `web/guac/views.py` | `can_manage_task` per task; `viewer_for(...).is_local_admin` for task-less console endpoints | See [Behavior notes](#behavior-notes). |
| `web/guac/consumers.py` | `can_manage_task` re-check on the WebSocket | Defense in depth behind the manage-gated mint. |
| `web/audit/views.py` | `_caller_can_delete_session` | Audit sessions have no owner column; authority is derived from the runs' CAPE tasks via `can_delete_task`, and a session with **no** authorization-bearing run fails closed to break-glass under MT. |
| submit | `submission_scope(request)` | Returns `(tenant_id, visibility)`; raises `ValueError` on an invalid explicit visibility so the view can 400. |
| apiv2 response shaping | `_strip_mt_task_fields`, `_strip_mt_sample_fields` | MT-off drops the new `tenant_id`/`visibility` keys (upstream-identical output); MT-on drops `samples.source_url` (per-submission provenance written by whoever first registered the hash — a cross-tenant leak on a shared sample row). |

### The import-optional facades

Two facades let views import MT symbols unconditionally:

- `lib/cuckoo/common/tenancy_optional.py` — lib-level symbols.
- `web/web/tenancy_optional.py` — web-level symbols; delegates `viewer_for` /
  `multitenancy_config` to `users.tenancy` specifically (so a test fixture patching
  `users.tenancy.multitenancy_config` is honoured and scoping can't silently change).

Both catch **`ImportError` only** and distinguish two very different situations via
`_mt_enabled()` (read from the pure lib config, independent of the web import chain):

```python
def can_view_task(user, task):
    try:
        from users.tenancy import can_view_task as real
    except ImportError:
        return not _mt_enabled()  # MT enabled+broken -> deny; genuinely absent -> allow (single-tenant)
    return real(user, task)
```

The fallback `Viewer` docstring records why:

> FALLBACK viewer, returned by `viewer_for()` ONLY when the MT layer is absent — see-all
> (`is_local_admin=True`). When MT IS deployed, `viewer_for()` returns the REAL
> `lib.cuckoo.common.tenancy.Viewer`, a DIFFERENT class. So do NOT
> `isinstance(viewer_for(u), Viewer)` against this type — it's False in production. Treat
> `viewer_for()`'s result structurally (`.is_local_admin` / `.tenant_id`), never by type.

## The endpoint coverage gate

`web/apiv2/test_visibility.py` is a build-failing security gate, not just a unit test. It
auto-discovers routed views from `apiv2/urls.py`, `analysis/urls.py`, `compare/urls.py`,
`guac/urls.py` **and** the root urlconf `web/web/urls.py` (where analysis views are
referenced under the `analysis_views` alias), then asserts each one's source references a
known guard:

```python
GUARD_MARKERS = (
    "_deny_if_hidden", "_deny_task", "_deny_manage", "_resolve_task_id", "visible_to",
    "require_task_visibility", "require_task_manage", "require_task_delete", "can_view_task",
    "can_manage_task", "can_delete_task", "can_toggle_task",
    "scope_match", "entitled_scope_filter", "viewer_scope",
)
ID_GROUPS = ("task_id", "analysis_number", "left_id", "right_id")
```

It covers four surfaces the routed scan alone cannot see:

- `AGGREGATE_TASK_FEEDS = ("tasks_rollingsuri", "pending", "hunt", "search")` — many-task
  feeds with no `task_id` in the route.
- `BODY_KEYED_MUTATIONS = (("apiv2.views", "tasks_delete_many"), ("analysis.views", "tag_tasks"))`
  — ids come from the request body.
- `GUAC_SESSION_VIEWS = (("submission.views", "status"), ("submission.views", "remote_session"))`
  — anything emitting a live-VM session token.
- the Guacamole WebSocket consumer, which isn't URL-routed at all.

`ALLOWLIST` currently holds exactly one entry, `tasks_machine`, with the justification
inline (returns only a pool VM label; authorizes via the `[api] control_plane_token`
constant-time match **or** `viewer_for().is_local_admin`; an empty token disables the
shared-secret path). Adding a task-scoped endpoint without a guard fails CI; adding an
allowlist entry requires a written justification.

## Fail-closed behaviour (and why)

| Condition | Behaviour | Where |
| --- | --- | --- |
| `[multitenancy]` section absent | MT **off** (legitimate single-tenant default, not an error) | `multitenancy_config()` `CuckooOperationalError` branch |
| `cuckoo.conf` malformed / unreadable (parse or IO error) | `MTConfig(enabled=True, mode="locked", default_visibility="private", local_admins_manage_all_tenants=False)` + `log.exception` | `multitenancy_config()` generic `except` |
| unknown/typo `mode` | normalized to `locked` | `multitenancy_config()` |
| explicitly-set unrecognized `default_visibility` | `private` (blank keeps the per-mode default) | `default_visibility()` |
| MT enabled but the MT layer import chain breaks | every task/sample `can_*` denies (`can_ban_user` instead falls back to upstream's `is_staff or is_superuser` check); `viewer_for` → non-admin tenantless viewer; `submission_scope` → `(None, private)`; `viewer_scope_filter` → `{"_id": {"$in": []}}`; `entitled_scopes` → `()` | both `tenancy_optional` modules, `dashboard.views.entitled_scopes`, `analysis/central_scope.py` |
| `Tenant.active = False` | tenant + tenant-admin dropped from the viewer immediately | `viewer_for` |
| user matches >1 tenant's `idp_groups` | tenant unset + warning | `reconcile_tenant` |
| `groups_claim` absent from the token | reconciliation skipped (no mass demotion) | `_reconcile_sso_user_on_login`, `_apply_idp_roles_and_email` |
| explicit `visibility=tenant` from a tenant-less submitter | `ValueError` → view 400 | `submission_scope` |
| per-mode default resolves to `tenant` for a tenant-less submitter | downgraded to `private` | `submission_scope` |
| task unresolvable at report time (deleted/orphan/DB error) | `info` stamped `private`, `tenant_id=None`, `user_id=None` | `stamp_tenant_info` |
| legacy distributed worker (`options.main_task_id` set) | stamped private/unowned | `_stamp_report_for_task` |
| central rewritten (`ui-*`) id with `central_database_url` unset | stamped private/unowned | `_task_tenant_ctx_central` |
| every mongo report insert under MT | inserted **fail-closed private**, then raised to the authoritative value by `_reconcile_report_visibility` under the advisory lock | `modules/reporting/mongodb.py::run` |
| central + MT with a non-bridged `job_id` | insert **refused** (`CuckooReportError`) rather than persisting an unaddressable doc | `_reject_unbridged_under_mt` / `central_bridge_required` |
| Alembic migration on an existing install | existing rows backfilled to `visibility='private'`, `tenant_id` NULL (not `public`) | `3_add_tenant_visibility.py` |
| mongo backfill hits an orphan / non-numeric `info.id` | stamped `private` / unowned; does not abort the run | `mongo_backfill_tenant.py::backfill_doc` |
| Postgres advisory lock unavailable during a toggle | toggle **aborts** (`CuckooOperationalError`), never proceeds unserialized | `_advisory_lock`, `set_task_visibility` |
| `expected_prior` CAS mismatch (concurrent toggle) | `CuckooVisibilityConflict` → 409, retry | `set_task_visibility` |
| non-numeric / out-of-range (`>2147483647`) task id on a `\w+` route | generic denial before any DB call | `_coerce_task_id` |
| missing task under MT | same generic 404 / "Not found" as a hidden task | `_deny_if_hidden`, `_deny_manage`, `require_task_*` |
| audit session with no live task to authorize against | break-glass box admin only (under MT) | `_caller_can_delete_session` |
| `can_ban_user` target lookup error | deny | `can_ban_user` |

## Enabling on an existing (populated) install

Back up first. Then, in order:

**1. SQL schema.** Existing rows are backfilled fail-closed to `private` / `tenant_id NULL`:

```bash
cd utils/db_migration
alembic upgrade head        # revision 3a1b_tenant_visibility
```

**2. Django schema** (creates `Tenant`, adds `UserProfile.tenant` / `is_tenant_admin`):

```bash
cd web
poetry run python3 manage.py migrate
```

**3. Create tenants and map identities.** In the Django admin: create each `Tenant` with
its `idp_groups` / `admin_idp_groups`, and for local (non-SSO) accounts set
`UserProfile.tenant` / `is_tenant_admin` directly. SSO users are mapped on their next login.

**4. Turn the feature on** in `conf/cuckoo.conf` and restart the web and processing
services (config is read at process start):

```ini
[multitenancy]
enabled = yes
mode = locked
default_visibility =
local_admins_manage_all_tenants = yes
```

**5. Run the mongo backfill — this step is not optional on a populated install.**

Turning `enabled = yes` stamps tenant/visibility onto **new** analyses only. Reports
already in MongoDB have no `info.tenant_id` / `info.user_id` / `info.visibility`
stamp, so the scoped search / statistics / compare surfaces treat them as
**fail-closed / invisible** to every tenant (no leak, but the history disappears
from those views) until they are stamped. Run the one-shot backfill once, after
flipping the flag:

```bash
python utils/db_migration/mongo_backfill_tenant.py
```

It reads each selected `analysis` doc's Postgres task and writes
`info.tenant_id` / `info.user_id` / `info.visibility` (orphans whose task was pruned
fail closed to `private`), and creates the `tenant_scope_idx` index. It touches only
(a) un-stamped docs (missing `info.visibility`, first-enable) and (b) crash-orphans in
the exact reporter fail-closed shape (`visibility=private` + null `tenant_id` AND
`user_id`) — never a stamped permissive doc — so it stays idempotent and safe to re-run.
In a **central** deployment run it on the CENTRAL node **while quiesced**: it only
restamps docs whose id space matches the node (broker `ui-*` ids ⇔ central RDS;
worker-local ids ⇔ single-node), and it is not lock-serialized against a live toggle.
The Alembic migration backfills the **SQL** columns only — the mongo stamp is this
separate step. A fresh install needs no backfill (every report is stamped at
creation).

**6. Central deployments only.** On every **worker**, also set `[multitenancy] enabled = yes`
(the reporter's stamp + reconcile are gated on it) and point it at the control plane:

```ini
[central_mode]
central_database_url = postgresql://<user>:<pass>@<central-writer-endpoint>/<db>
```

and keep `[centralstore] enabled = yes` in `reporting.conf` (it stamps the `ui-<central_id>`
`job_id` that tenant-scoped filters key on).

## Supported (isolation enforced end-to-end)

- **Report store: MongoDB.** MT scoping of the aggregate/search/statistics/
  compare surfaces reads the tenant stamp (`info.tenant_id` / `info.user_id` /
  `info.visibility`) written into the mongo analysis document. **MongoDB is
  required for multitenancy.**
- **Single-node CAPE** (one host running web + processing + analysis).
- **Central control plane + broker workers** (the "central mode" path): the
  central UI serves artifacts staged from workers, keyed by the broker `job_id`.
  Tenant stamping works across this path **only when workers can resolve the
  submitter's tenancy from the central control-plane DB**: a worker's own
  `[database]` is its LOCAL per-worker task DB (a different id space), and
  `centralstore` rewrites `info.id` to the CENTRAL task id, so the worker resolves
  and stamps tenancy against the central RDS via `[central_mode] central_database_url`.
  If that URL is unset, central-mode analyses stamp **fail-closed** (private / unowned
  — invisible to everyone but break-glass), never leaked. Point `central_database_url`
  at the **writer/primary** endpoint (the same Postgres the central node uses as its
  `[database]`), NOT a read replica: the worker's post-write reconcile takes its
  per-task advisory lock there to serialize with the central node's visibility toggle
  (advisory locks are cluster-wide, so same-key locks on the same primary mutually
  exclude). If it points at a standby, `pg_advisory_lock` wouldn't exclude the primary
  and the re-read would be replication-lag stale: the worker detects
  `pg_is_in_recovery()` (a fresh pre-lock probe, re-checked on the pinned lock
  connection), warns, and **skips the central visibility upgrade entirely** — the doc keeps
  the reporter's fail-closed `private` stamp (invisible until the URL is repointed and the
  analysis reprocessed, or the backfill is run), never re-widened from a lagging replica.
- **Guacamole interactive sessions** for task-backed analyses: minting a live-VM
  session (and the WebSocket tunnel re-check) is gated by `can_manage_task`
  (owner / tenant-admin / break-glass), NOT mere read visibility — live keyboard/
  mouse/framebuffer control is a task action, so a read-only viewer of a public/
  tenant task cannot tunnel into another user's or tenant's VM.

## Not yet supported (fail-closed — safe, but limited)

These modes do **not** carry tenant context correctly. Rather than leak, MT
**fails closed** on them (data is stamped private / invisible, or the surface is
admin-only), so enabling MT on these modes is safe but the affected analyses
will simply not be visible. Adding real support is tracked as future work.

- **Elasticsearch report store.** A scope filter exists and is applied in
  `perform_search`'s ES branches and in `top_detections`' ES aggregation
  (`viewer_scope_es_filter`), but coverage stops there: the visibility toggle syncs the
  tenant stamp only to MongoDB (`_mongo_reporting_enabled()` gates the sync), so an
  ES-backed install would not update the ES stamp, and the ES statistics aggregate is not
  gated at all — `statistics()` sets `data = False` on its ES branch and returns the empty
  skeleton (which is also why `top_detections`' ES branch is unreachable from it, its
  filter notwithstanding). **Run MT with MongoDB.**
- **Legacy distributed (`utils/dist.py`).** The main→worker submission does not
  forward tenant/user/visibility, so a distributed worker cannot stamp the shared
  mongo document correctly. When a worker processes a distributed task
  (`options.main_task_id` set), the report is stamped **private/invisible**
  (fail-closed) instead of world-visible. Use the broker/central path for
  distributed multitenant analysis. (The central path keys by `job_id` and never
  sets `main_task_id`, so it is unaffected.)

## Behavior notes

- **Statistics API shape (shared mode).** With MT enabled in `shared` mode, the
  `apiv2` statistics endpoint returns **per-scope** results
  (`data['public']`, `data['tenant']`, `data['mine']`) instead of the legacy flat
  `data['signatures']`. This is the correct scoped behavior; it is a breaking
  change for API clients that assumed the flat shape on an MT-shared install.
  Multitenancy-disabled installs and break-glass local-admins still receive the
  flat dict (`entitled_scopes()` → `["global"]`).
- **Direct VNC / VM operator console.** The task-less direct-console endpoints
  (`task_id=0`: raw host:port VNC, plus VM console/start/shutdown/route/snapshot
  by name) have no tenant scoping and mint sessions the per-task tunnel gate does
  not cover, so **all** of them are restricted to break-glass admins
  (`viewer_for(user).is_local_admin`) in addition to the existing config gate —
  never a tenant user. (On an MT-disabled / no-auth install every principal is a
  local-admin, so the operator console stays usable.)
- **Threat-hunt facets.** `hunt()` scopes its aggregation by the viewer's entitled
  scopes (`viewer_scope` `$match`); its facet `task_ids` rely on that stamp-based
  `$match` with no per-id SQL backstop (a `$facet` count can't be post-filtered
  per task). This is safe because the report tenant stamp is written fail-closed
  on every path, so a doc can't carry a spoofed cross-tenant stamp.
- **Submission form.** The visibility `<select>` is MT-only. It offers
  `[public, tenant, private]` to a tenant member and `[public, private]` to a tenant-less
  user, and its default is forced to `private` when the per-mode default would be
  `tenant` for a tenant-less user — otherwise the browser would select the first option
  (`public`) and submit it explicitly, bypassing `submission_scope`'s fail-closed downgrade.
- **`shared` vs `locked` only changes the submit default.** Both modes produce scoped
  read surfaces. If you want a genuinely collaborative pool, that is `mode = shared` plus
  submitters leaving visibility at `public`.

## Gotchas / operational notes

Each of these is a guard that exists because of a real failure; the code comment is the
authority.

1. **`ImportError` fail-open caused a real cross-tenant leak once.** `viewer_for` lives in
   the web `users` app (needs the Django ORM), not the pure predicate module. An early
   version imported it from `lib.cuckoo.common.tenancy`, which *always* raised
   `ImportError`, so the facade silently degraded to see-all **even when MT was deployed**.
   That is why both facades resolve `viewer_for` from `users.tenancy` and why every
   `except ImportError` arm that could otherwise fall back to see-all consults
   `_mt_enabled()` first. Never widen these `except ImportError` clauses to
   `except Exception`.
2. **Do not `isinstance()` a viewer.** `tenancy_optional.Viewer` is a *different class*
   from the real `lib.cuckoo.common.tenancy.Viewer`; an isinstance check is False in
   production. Read `.is_local_admin` / `.tenant_id` structurally.
3. **`viewer_for` must short-circuit the MT-disabled case BEFORE the
   `is_authenticated` check.** On a disabled install a no-auth deployment
   (`WEB_AUTHENTICATION` off) or apiv2 with token auth off (DRF `AllowAny`) serves every
   request as `AnonymousUser`. An earlier ordering returned `is_local_admin=False` for
   anonymous before reading the config, which made ~45 guards deny the private-default
   tasks upstream served. Pinned by
   `test_disabled_anonymous_is_backcompat_see_all`.
4. **`is_staff` is not the tenancy break-glass.** Only `is_local_admin` is. The tenant
   scope in `perform_search` is applied *regardless* of the legacy `privs` flag.
5. **Config is cached at process start.** Editing `[multitenancy]` requires restarting the
   web and processing services; a live edit has no effect until then.
6. **MT must be enabled on the workers too, in a central deployment.** The reporter's
   `stamp_tenant_info` and `_reconcile_report_visibility` are both gated on
   `multitenancy_config().enabled` *on the node doing the reporting*. A central UI with MT
   on and workers with MT off produces unstamped (invisible) analyses.
7. **Point `central_database_url` at the writer/primary, not a reader endpoint.** A standby
   breaks the cross-node advisory-lock serialization (`pg_advisory_lock` on a standby
   doesn't exclude the primary) and makes the under-lock re-read replication-lag stale. The
   worker detects `pg_is_in_recovery()`, warns, and then skips the central doc's visibility
   upgrade so it stays fail-closed `private` rather than being re-widened from a lagging
   replica.
8. **Central + MT requires the submit bridge.** `_reject_unbridged_under_mt` refuses to
   persist an analysis whose `job_id` isn't `ui-<central_id>`, because the tenant-scoped
   own-doc read/write/delete filters key **only** on that id. `[centralstore] enabled = no`
   (the default) under central + MT therefore surfaces as a `CuckooReportError` on the
   report insert — that is the guard working, not a bug. A direct worker submission
   (`local-<id>`) is single-tenant only and is intentionally not addressable through the
   tenant-scoped paths.
9. **Advisory locks: session-level on a dedicated NullPool engine.** The visibility toggle
   holds a Postgres *session*-level advisory lock on a dedicated connection because it must
   outlive the mid-operation commit, and must not be the pooled ORM session (the commit
   returns that connection to the pool → re-entrant or leaked lock) nor the shared app
   QueuePool (a lock held across a slow mongo round-trip could starve it). Failure to
   acquire **raises**; the caller aborts. `lock_engine is None` on non-Postgres (sqlite is
   single-writer) — which means a non-Postgres MT install runs the reconcile unserialized,
   logged once by `_warn_no_lock_engine_once()`. The fail-closed insert keeps that window
   safe; a narrow re-widen remains.
10. **Write ordering is by direction, not by store.** For a RESTRICTIVE change
    `set_task_visibility` syncs mongo first and commits SQL last; for a PERMISSIVE one it
    commits SQL first and publishes mongo last — so a crash or a concurrent read in the
    window always fails closed. The one invariant that must never break is
    "mongo more permissive than SQL" — that is the leak.
11. **The toggle re-asserts ownership, not just visibility.** A doc orphaned by a crash
    between the fail-closed insert and the reconcile is unowned (`tenant_id`/`user_id`
    null). Syncing visibility alone would produce `{visibility: tenant, tenant_id: null}`,
    which matches *no* viewer scope — invisible even to the owner. Restamping ownership is
    idempotent for a normal toggle and repairs the orphan.
12. **`tenant` visibility with a NULL tenant is a broken state, not "everyone".**
    `can_read`'s tenant branch requires a non-NULL job tenant, so such a task is readable
    only by its owner. Both `submission_scope` and the apiv2 toggle endpoint refuse to
    create it.
13. **`submission_scope` ignores a caller-supplied `visibility` when MT is off,** returning
    `(None, public)`. Persisting `private`/`tenant` on a disabled install would plant a
    backfill landmine: the mongo backfill skips already-stamped docs, so those rows would
    unexpectedly hide analyses if MT were enabled later.
14. **The apiv2 visibility endpoint rejects writes when MT is disabled** (400) for the same
    reason — with MT off `can_toggle` would authorize *any* caller.
15. **Missing and hidden are the same response.** Under MT, a task you can't see and a task
    that doesn't exist both return the generic 404 / "Not found", so task ids can't be
    enumerated across tenants by status code. A distinguishable message appears only once
    the caller demonstrably *can* see the task (e.g. `require_task_delete`'s
    "You are not permitted to delete this task").
16. **Non-numeric and out-of-range ids fail closed before the DB.** Some analysis routes
    capture the id as `\w+`; forwarding `"abc"` (or a 40-digit number) to `db.view_task()`
    raised a driver `DataError` → an uncaught 500 that also leaked a task-vs-no-task signal.
    `_coerce_task_id` bounds it to `1..2147483647`.
17. **`samples.source_url` is stripped when MT is on.** The `samples` row is globally
    hash-deduplicated with no owner column, so `source_url` — written by whoever *first*
    registered the hash — would leak another tenant's internal URL/C2 to any tenant that
    later submits the same file.
18. **Break-glass off means IdP-only admin.** With `local_admins_manage_all_tenants = no`,
    a `createsuperuser` account is **not** cross-tenant; only superusers with a linked
    `SocialAccount` are. Note that this resolution costs a
    `socialaccount_set.exists()` query per `viewer_for` call, which is why list views like
    `pending()` resolve the viewer **once** and use `can_delete_job(viewer, task)` per row
    instead of `can_delete_task(user, task)`.
19. **Alembic migration 3 needs `existing_type` for MySQL/MariaDB.** MySQL rebuilds a column
    via `MODIFY COLUMN`, which Alembic can't render without the type; omitting it aborts
    `alembic upgrade` *after* the `add_column` auto-commits, leaving the DB wedged
    half-applied. Pinned by `tests/test_migration_tenant_visibility.py`.
20. **Do not backfill existing tasks to `public`.** Both the SQL migration and the mongo
    backfill choose `private`: in `locked` mode a `public` backfill would make every
    historical task cross-tenant readable and the two stores would disagree on the same rows.
21. **The mongo backfill's selector is deliberately narrow.** It matches un-stamped docs, or
    the *exact* reporter fail-closed shape (`private` + null `tenant_id` **and** `user_id`).
    A bare `{info.user_id: null}` arm would also match every legitimately-stamped
    anonymous/CLI doc (Mongo `null` matches missing too), and a re-run could silently
    downgrade a public doc whose task was later pruned.
22. **Run the backfill on the node whose id space matches.** `_is_central_id(job_id) != _central`
    docs are skipped and counted, because resolving a foreign id against the wrong DB would
    hit a colliding row and stamp another tenant's scope onto the doc.

## Tests

The MT rules are encoded as executable specifications. `tests/tenancy_vectors.py` is the
canonical vector table (shared with the broker's reimplementation by design).

| Area | Files |
| --- | --- |
| Pure predicate + config normalization | `tests/test_tenancy.py`, `tests/tenancy_vectors.py` |
| Facade fail-closed contract | `tests/test_tenancy_optional.py`, `tests/test_tenancy_optional_failclosed.py`, `web/users/test_tenancy_failclosed.py`, `tests/test_mt_absent_smoke.py` |
| SQL store (columns, `visible_to`, scopes, toggle, advisory locks) | `tests/test_task_visibility.py` |
| Migration renders on MySQL + ORM/Alembic index parity | `tests/test_migration_tenant_visibility.py` |
| Identity → tenant mapping, break-glass, `submission_scope`, `can_ban_user` | `web/users/test_tenancy.py`, `web/users/test_admin.py` |
| Endpoint coverage gate + apiv2 authz | `web/apiv2/test_visibility.py`, `web/apiv2/test_analysis_filter.py`, `web/apiv2/test_hash_isolation.py`, `web/apiv2/test_rollingsuri_scope.py` |
| Analysis / compare / dashboard / submission / guac / audit surfaces | `web/analysis/test_visibility.py`, `web/analysis/test_analysis_scope.py`, `web/analysis/test_central_scope.py`, `web/analysis/test_central_delete.py`, `web/compare/test_visibility.py`, `web/compare/test_compare_scope.py`, `web/dashboard/test_dashboard_scope.py`, `web/submission/test_visibility.py`, `web/guac/test_visibility.py`, `web/guac/test_channels_auth.py`, `web/audit/test_tenancy.py` |
| Settings-level import-optionality | `web/web/test_settings_mt_optional.py` |

Fixtures `mt_enabled` / `mt_disabled` / `cape_db` live in `web/mt_test_fixtures.py` (loaded
via `pytest_plugins = ("mt_test_fixtures",)` rather than a `conftest.py`, which would shadow
`tests/conftest.py` under `pythonpath = [".", "web"]`).

Running them mirrors CI (`.github/workflows/python-package.yml`) — the `web/` Django suites
live outside `testpaths` and need `--import-mode=importlib`:

```bash
poetry run python -m pytest --import-mode=append          # tests/ + agent/
poetry run python -m pytest web/ --import-mode=importlib  # web/ Django suites
```

Every new MT-off invariant test asserts the guard is a **true no-op** when the feature is
disabled — that is the contract that keeps single-tenant CAPE byte-identical to upstream.
