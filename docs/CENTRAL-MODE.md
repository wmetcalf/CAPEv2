# Central mode

"Central mode" splits one CAPE install into **one central web/API node** (the UI, the relational
task DB, the Mongo-compatible report store) and **many stateless analysis workers** that own the
hypervisor, run the scheduler/processing, and publish their results to a shared object store
instead of keeping them on local disk. It is a single config gate — `[central_mode] enabled` in
`conf/cuckoo.conf` — and it is **off by default**: with the gate off every code path in this
document short-circuits and CAPE behaves exactly as the single-node install it always was
(see [Gate OFF: single-node behaviour](#gate-off-single-node-behaviour)). Central mode is
independent of `[multitenancy]`; the two toggles compose but neither requires the other.

- [Why](#why)
- [Node roles](#node-roles)
- [Enabling it](#enabling-it)
- [`[central_mode]` config reference](#central_mode-config-reference)
- [`job_id`: the universal key](#job_id-the-universal-key)
- [Write path: worker → central store](#write-path-worker--central-store)
- [Read path: central UI → artifacts](#read-path-central-ui--artifacts)
- [Storage backends](#storage-backends)
- [Status, failures and reconciliation](#status-failures-and-reconciliation)
- [Interactive live-VM attach across nodes](#interactive-live-vm-attach-across-nodes)
- [Reprocessing a task in central mode](#reprocessing-a-task-in-central-mode)
- [Gate OFF: single-node behaviour](#gate-off-single-node-behaviour)
- [Gotchas / operational notes](#gotchas--operational-notes)
- [Tests](#tests)

---

## Why

A stock CAPE node is all-in-one: the web UI reads `storage/analyses/<task_id>/` on its own
filesystem, and the analysis VMs live in that same host's libvirt. That couples UI availability,
retention and disk sizing to the machine that detonates samples, and it makes horizontal scaling
of detonation capacity impossible without shipping users to N separate UIs.

Central mode breaks that coupling at three seams, and only those three:

1. **Artifacts** — the FS→object-store seam (`lib/cuckoo/common/artifact_storage.py` read side,
   `modules/reporting/centralstore.py` write side). Workers push the analysis tree to a shared
   store; the UI streams from it (or lazily stages it locally).
2. **Identity** — a global `job_id` replaces the per-node `info.id` as the key of an analysis,
   because a worker's task-id sequence is local and collides across workers.
3. **Live VMs** — interactive Guacamole attach resolves *which worker* holds a running task's VM
   (`lib/cuckoo/common/job_directory.py`, `lib/cuckoo/common/central_guac.py`).

Everything else — processing modules, signatures, the report document, the report UI — is
unchanged upstream code.

## Node roles

| | Central node | Worker |
|---|---|---|
| Runs | Django web + apiv2 (+ Guacamole consumer) | scheduler (`cuckoo.py`), `utils/process.py`, libvirt/VMs, rooter |
| `[database]` | the **central** task DB (authoritative task rows, tenancy, visibility) | its own **local** task DB (a different id space) |
| `[mongodb]` | the shared report store | the same shared report store (it writes into it) |
| `[central_mode] enabled` | `yes` | `yes` |
| `[centralstore] enabled` (`reporting.conf`) | not used (the UI never reports) | **`yes`** — required |
| `[central_mode] tolerate_missing_rooter` | `yes` (UI advertises routes, routes nothing) | **`no`** (a worker with a dead rooter must fail fast) |
| `[central_mode] central_database_url` | not used | set (needed only with `[multitenancy]`) |
| Local `storage/analyses/` | a *cache* — lazily staged from the central store | the authoritative copy until the upload is confirmed |

There is no central-mode dispatcher in this repository. Getting a submission from the central node
onto a worker is done by an **external broker** (and, when tenancy matters, a *submit-bridge*); the
only in-tree contract is described in [`job_id`](#job_id-the-universal-key) and
[the job directory](#interactive-live-vm-attach-across-nodes). Central mode also works without a
broker (direct submission on a worker) — see the `local-<id>` form below.

## Enabling it

Worker, `conf/cuckoo.conf`:

```ini
[central_mode]
enabled = yes
storage_backend = s3
s3_bucket = my-cape-results
s3_region = us-east-1
s3_prefix = results
# central + multitenancy only:
central_database_url = postgresql://cape_ro:...@central-db:5432/cape
```

Worker, `conf/reporting.conf` — **`centralstore` is a prerequisite**, not an optimisation:

```ini
[centralstore]
enabled = yes
```

> From `conf/default/reporting.conf.default`: *"This module is a PREREQUISITE for central mode --
> with it disabled, `[central_mode]` is non-functional (no artifacts uploaded, `info.job_id` never
> stamped, `info.id` never rewritten to the central task id, and the visibility reconcile has no
> job_id to key on)."*

Central node, `conf/cuckoo.conf` — same `[central_mode]` storage block (so the read seam resolves
the same container), plus:

```ini
[central_mode]
enabled = yes
tolerate_missing_rooter = yes
job_directory = broker_http
broker_url = https://broker.internal
broker_api_token = <token>
```

## `[central_mode]` config reference

Parsed by `lib/cuckoo/common/central_mode.py` (`_parse()` → `CentralModeConfig`); documented in
`conf/default/cuckoo.conf.default`. Every value is coerced defensively — a bad boolean, int or port
degrades to the default rather than crashing startup (`_as_bool` / `_as_int` / `_as_port`; a port
outside `1..65535` is rejected).

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `no` | The gate. Off = single-node behaviour everywhere. |
| `storage_backend` | `s3` | `s3` = any S3-compatible object store via boto3 (lazy import); `local` = a shared local/NFS mount. Lower-cased on parse. |
| `s3_bucket` | *(empty)* | Bucket for `storage_backend = s3`. Empty + central enabled ⇒ `centralstore` raises `CuckooReportError` up front. |
| `s3_region` | `us-east-1` | Region passed to the client. Also used as the region for `job_directory = dynamodb`. |
| `s3_prefix` | `results` | Key prefix; the per-analysis container is `<s3_prefix>/<job_id>`. |
| `s3_endpoint_url` | *(empty)* | Empty ⇒ the SDK's default endpoint. Set it to point at MinIO/Ceph/etc. |
| `s3_access_key` | *(empty)* | Empty ⇒ boto3's default credential chain (e.g. an instance role). |
| `s3_secret_key` | *(empty)* | Paired with `s3_access_key`; both must be non-empty to be used. |
| `central_local_root` | *(empty)* | Root for `storage_backend = local`; per-job trees live at `<central_local_root>/<s3_prefix>/<job_id>/`. Empty ⇒ `get_artifact_store()` falls through to `S3Store` (see [Storage backends](#storage-backends)). |
| `job_directory` | `broker_http` | Job→worker resolver for interactive attach: `broker_http` (vendor-neutral HTTP) or `dynamodb` (opt-in). Lower-cased on parse. |
| `broker_url` | *(empty)* | `broker_http` base URL. Empty ⇒ no directory (attach keeps the local path). |
| `broker_api_token` | *(empty)* | Bearer token for `broker_http`. Empty ⇒ no `Authorization` header. |
| `broker_table` | *(empty)* | Table name for `job_directory = dynamodb`. Empty ⇒ no directory. |
| `worker_api_token_file` | `/etc/cape/api-token` | File on the central node holding the secret presented to a worker's `apiv2 tasks/machine`. Must equal that worker's `[api] control_plane_token`. Missing/unreadable ⇒ unauthenticated request. |
| `worker_api_port` | `8000` | The worker's apiv2/web port. |
| `worker_ssh_user` | `cape` | The central node's libvirt-over-SSH identity onto workers. |
| `worker_ssh_keyfile` | `/home/cape/.ssh/id_ed25519` | Key for the above; URL-quoted into the libvirt DSN. |
| `central_database_url` | *(empty)* | SQLAlchemy URL of the **central** task DB, used by **workers only** to resolve a central task's tenancy when stamping the shared report doc. Empty ⇒ central analyses stamp **fail-closed** (private/unowned). Must be the **writer/primary**. |
| `tolerate_missing_rooter` | `no` | Set `yes` **only on the central UI node**: `init_rooter`/`init_routing` then warn-and-continue on an unreachable rooter while still populating the submission form's route list. |

Related keys outside `[central_mode]`:

| Key | File | Why it matters |
|---|---|---|
| `[centralstore] enabled` | `reporting.conf` | Prerequisite (see above). Default `no`. |
| `[api] control_plane_token` | `api.conf` | Worker-side shared secret that authorizes the central node's `tasks/machine` lookup. Default empty = that auth path disabled. |

## `job_id`: the universal key

A worker mints its own `info.id`, so two workers happily produce task 42. Central mode therefore
keys everything — the object-store container, the report doc, the delete/visibility writes — on
`info.job_id`.

The broker delivers it in the task's `custom` field. `job_id_from_custom()`
(`lib/cuckoo/common/artifact_storage.py`) is the **single parser** shared by the write consumer
(`centralstore.resolve_job_id`) and the read/delete consumers (`central_views.central_job_id_for_task`),
so the keys can never drift. Its rules:

* `job_id=<v>` is honoured **only as the raw first comma-field** — the string is deliberately *not*
  stripped before the prefix test, so the parser is anchored exactly like the bridge's
  `custom NOT LIKE 'job_id=%'` enqueue filter. `foo=bar,job_id=ui-9` and ` job_id=ui-9` are **not**
  honoured.
* A bare token (no `=`, no `,`) is the direct-submission fallback…
* …except a bare `ui-<N>`, which is **never** honoured: that is the bridge's reserved central-id form.
* The result must satisfy `_SAFE_JOB_ID_RE = ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$` and contain no `..`,
  because it becomes a container prefix. Rejected probe-shaped values are logged at WARNING.

Forms you will see:

| Form | Produced by | Consequence |
|---|---|---|
| `ui-<central_task_id>` | the submit-bridge | `centralstore` **rewrites `info.id` to `<central_task_id>`**, so the central UI resolves the doc by its own task id natively (no rewrite of ~25 upstream `info.id` call sites). |
| `local-<worker_task_id>` | `resolve_job_id()` fallback (direct submission, no broker) | Works, but is **not globally unique** and carries no tenancy. |

`lib/cuckoo/core/data/tasking.py::add()` deliberately does **not** scrub a client-supplied
`job_id=` from `custom` (it is the shared ingest for user submissions, the broker, and internal
resubmitters). Containment lives in the consumers' anchoring described above; every central *write*
that targets a task's own doc derives `ui-<task_id>` from the already-authorized task id rather than
reading `custom` (`central_own_analysis_filter`, `central_guac._job_id_for_task`).

## Write path: worker → central store

`modules/reporting/centralstore.py`, `order = 9998` — deliberately **before** the native
`modules/reporting/mongodb.py` (`order = 9999`) so the report document already carries
`info.job_id` (and the rewritten `info.id`) when it is inserted.

```
RunReporting on the worker
 ├─ centralstore (9998)
 │   ├─ gate:  if not central_mode_config().enabled: return          # single-node no-op
 │   ├─ prereq: s3_bucket set + boto3 importable (unless storage_backend=local AND central_local_root set)
 │   ├─ job_id = info.job_id or resolve_job_id(info.custom, info.id)
 │   ├─ reject unsafe job_id  -> CuckooReportError
 │   ├─ info["job_id"] = job_id ; if job_id == "ui-<N>": info["id"] = N
 │   ├─ upload storage/analyses/<id>/**  ->  <s3_prefix>/<job_id>/<relpath>
 │   ├─ upload storage/guacrecordings/<id>[_<session>]  ->  <container>/guacrecordings/<name>
 │   └─ if failed == 0: write .centralstore.done  (locally AND as the LAST object)
 └─ mongodb (9999) -> the shared report store, keyed by info.job_id / rewritten info.id
```

Details worth knowing:

* **Nothing is excluded** from the upload (`_EXCLUDE_DIRS = set()`), `reports/` included — the
  report doc in Mongo is additive, it does not replace `reports/report.json` etc.
* **Symlink guard (security).** The analysis tree contains symlinks *out* of itself — `binary` →
  `storage/binaries/<sha256>`, guac recordings → `storage/guacrecordings/`. Those must ship, so
  `central_mode.upload_target_realpath()` resolves them, but only accepts a realpath inside the
  analysis tree or one of the trusted roots (`storage/binaries`, `storage/guacrecordings`). A
  sample-planted symlink (`binary` → `~/.aws/credentials`) resolves elsewhere → returns `None` →
  skipped with a WARNING. The prefix test is separator-terminated so `storage/binaries-evil` does
  not count as inside `storage/binaries`.
* **Symlinked directories are never descended** (`dirs[:]` filter in `_upload_tree`).
* **Guac recordings** live outside the analysis tree and need their own pass; the name match
  requires an exact `<task_id>` or a `<task_id>_` boundary so task 1 never picks up task 15's file.
* **`.centralstore.done` is a two-place completion marker** (`_emit_done_marker`):
  * locally, in `storage/analyses/<id>/` — an external cleanup job can use it as its purge gate
    (present ⇒ artifacts are durable centrally ⇒ the local copy is safe to delete);
  * in the store, uploaded **last**, as `<container>/.centralstore.done` — the read side's
    completion signal. Its upload is retried 3 times (0.25 s, 0.5 s backoff) because reporting
    runs once and will not re-emit it; total failure only disables that job's staging *cache*
    (per-file reads still work).
* **Any upload failure ⇒ no marker at all**, and a WARNING naming the `job_id`. That is the
  fail-safe direction: an unconfirmed analysis is retained locally rather than purged.

## Read path: central UI → artifacts

`web/analysis/views.py` (which is heavily synced from upstream) carries only a thin dispatch at the
top of each affected view:

```python
from lib.cuckoo.common.central_mode import central_mode_config

if central_mode_config().enabled:
    from analysis.central_views import central_file
    return central_file(request, category, task_id, dlfile)
```

All central logic lives in `web/analysis/central_views.py` — a file upstream does not have, so it
never takes part in a merge conflict. Those functions run **only after** the upstream view's
decorator stack (`require_task_visibility`, auth, ratelimit) has authorized the relational task.

### Resolving the container

`artifact_storage._store_and_container(task_id, scope)`:

1. `get_artifact_store(cfg)` → `(store, is_central)`. Not central ⇒ `(LocalFSStore(storage/analyses), str(task_id))` and **no lookup at all**.
2. Central ⇒ `_job_id_for_task(task_id, scope)`:
   * prefer the **relational** `job_id` (`_rds_job_id` → the task row's `custom`), because it is
     collision-free and independent of the tenancy reconcile;
   * **authorize per call**: `job_id` came from user-supplied `custom`, so it is *not* an
     unforgeable token. The doc it names must match the viewer's `scope` **or** be this task's own
     not-yet-reconciled doc (`info.tenant_id: null` **and** `info.id == task_id`). Otherwise `Http404`.
   * only the (scope-independent) `task_id → job_id` mapping is cached — a bounded LRU of 1024
     entries. Scoped lookups are never cached.
   * non-bridged fallback: `{"info.id": task_id}` ANDed with the viewer scope, never cached.
3. `_is_safe_job_id(jid)` again (the Mongo-fallback branch returns a value straight from a document),
   then `container = f"{cfg.s3_prefix}/{jid}"`.

### Two ways artifacts reach the user

**Per-file streaming** — `artifact_response(task_id, relpath, content_type, filename, scope=)`
returns a `StreamingHttpResponse` straight off the store. Used by the download views. `relpath` is
validated by `_safe_relpath` (no absolute paths, no `\`, no `..` segment) before it becomes an
object key.

`central_views.central_file()` maps a UI category to an analysis-relative key:

| category | key under `<s3_prefix>/<job_id>/` |
|---|---|
| `sample` / `static` | `binary` (only if the requested sha256 **is** this task's sample) |
| `dropped` | `files/<name>` |
| `CAPE*` (non-zip) | `CAPE/<name>` |
| `pcap` / `decrypted_pcap` / `mixed_pcap` | `dump.pcap` / `dump_decrypted.pcap` / `dump_mixed.pcap` |
| `procdump*` (non-zip) | `procdump/<name>` |
| `memdump` / `memdumpstrings` | `memory/<name>.dmp` / `memory/<name>.dmp.strings` |
| `debugger_log` | `debugger/<name>.log` |
| `suricata` / `zip` | `logs/files/<name>` / `logs/files.zip` |
| `tlskeys` / `mitmdump` / `sysmon` / `evtx` | `tlsdump/tlsdump.log` / `mitmdump/dump.har` / `sysmon/sysmon.data` / `evtx/evtx.zip` |
| `rtf` / `usage` | `rtf_objects/<name>` / `aux/usage.svg` |
| screenshots / bingraph / vba2graph (`central_file_nl`) | `shots/<n>.jpg|.png` / `bingraph/<n>-ent.svg` / `vba2graph/svg/<n>.svg` |
| report download (`central_filereport`) | `reports/<fname>` |

The by-hash download is deliberately narrowed: the central store holds **only this analysis's**
`binary`, not a content-addressed by-hash store, so `central_file` serves it only when
`_task_sample_sha256(request, task_id) == dlfile.lower()` — otherwise "File not found", never the
wrong file's bytes under the requested hash's name.

**Lazy local staging** — many report features read `storage/analyses/<task_id>/` directly.
Rather than port each reader, `ensure_local_analysis(task_id, scope=)` copies the tree down once:

* excludes the big on-demand dumps: `memory/` and root `memory.dmp*` (`exclude_prefixes`);
* caches with a `.central_staged` marker, written **only** if the store listing contained
  `.centralstore.done` — so a listing taken mid-upload is never cached as complete;
* `_stage_tree` has no per-file `try`, so the first download error aborts the rest of that pass;
  `ensure_local_analysis`'s outer `except` swallows it (the partial tree stays, the per-file seam
  still serves) but a clean `Http404` propagates so the view 404s instead of rendering a broken
  page; other errors log a WARNING (S3 creds/permissions/network must not vanish silently);
* destination paths are re-validated: a key that would escape the analysis dir is skipped.

`ensure_local_memory(task_id, scope=, include_full_ram=)` stages the memory dumps on explicit
demand. `include_full_ram=False` (the procmemory endpoints) stages only the per-process `memory/`
subtree, so a per-process dump request never drags the multi-GB full-RAM image onto the web node.
Callers: `report()`, `load_files()`, the EVTX tab loaders, `web/apiv2/views.py::_central_stage()`
(which must run **after** per-task authorization).

### Report-document reads

`central_views.scoped_analysis_query(request, task_id, extra=None)` is the shared filter for every
per-task read of the shared `analysis` collection (`report()`, the apiv2 report family, tab
loaders, compare seeds):

```python
if central_mode_config().enabled:
    q = central_analysis_query(task_id, scope=viewer_scope(request.user))
else:
    q = {"info.id": int(task_id)}
```

`central_analysis_query` keys on the globally-unique `info.job_id` for a bridged task and falls back
to `info.id` ANDed with the viewer scope for a non-bridged/seeded doc. `viewer_scope()`
(`web/analysis/central_scope.py`) is the single choke point for tenancy and is **fail-closed**: it
catches `ImportError` only — meaning "the multitenancy layer is genuinely absent, see-all is
correct" — and lets any *runtime* error propagate rather than degrade to an unscoped read. If the MT
layer is detectably enabled but its import fails, it returns a deny-all `{"_id": {"$in": []}}`.

### Not available centrally

These render an explicit error instead of a silent 404, because they generate/bundle artifacts
server-side from the local tree:

| Surface | Message source |
|---|---|
| per-connection pcap regeneration | `central_pcapstream` |
| on-demand detail generation | `central_on_demand` ("re-processing is handled by workers") |
| `pcapng` (and any other category `central_file` does not map) | `central_file`'s final `else` |

(The zip-on-the-fly *download bundles* do work, and never reach `central_file`: `file()` intercepts
every `zip_categories` member — including the search-driven `*zipall` ones — calls
`central_stage_local()` to materialise the tree, and the upstream pyzipper path then runs unchanged.)

## Storage backends

`lib/cuckoo/common/storage_backend.py` is Django-free and vendor-neutral. A `container` is the
per-analysis root (`<task_id>` single-node, `<s3_prefix>/<job_id>` central); `relpath` is the
artifact path within it (already validated by the caller).

```python
class ArtifactStore:
    def exists(self, container, relpath) -> bool: ...
    def stream(self, container, relpath, chunk=8192): ...   # -> (byte_iter, length|None); ArtifactNotFound
    def read_text(self, container, relpath, max_bytes): ...  # "" if absent
    def materialize(self, container, relpath): ...           # -> (local_path, is_temp) | (None, False)
    def iter_relpaths(self, container): ...                  # yields relpaths
    def download(self, container, relpath, dest_abspath): ...
    def put_file(self, local_path, container, relpath): ...
```

Two implementations ship:

* **`S3Store`** — any S3-compatible store through boto3. `endpoint_url`/creds are optional
  (`""` → SDK defaults), the client is built lazily on first use, and `read_text` uses a ranged
  `GET` (`Range: bytes=0-<max_bytes>`).
* **`LocalFSStore`** — used for single-node **and** for a central deployment on a shared mount.
  `put_file` writes to a `.part-*` temp file in the *same* directory and then `os.replace()`s it, so
  a concurrent reader staging from the mount can never observe a half-written object at the final key.

Selection (`get_artifact_store(cfg)`):

```python
if not cfg.enabled:                                           # single-node, always
    return LocalFSStore(<CUCKOO_ROOT>/storage/analyses), False
if cfg.storage_backend == "local" and cfg.central_local_root:  # shared mount
    return LocalFSStore(cfg.central_local_root), True
return S3Store(cfg.s3_bucket, cfg.s3_region, cfg.s3_endpoint_url,
               cfg.s3_access_key, cfg.s3_secret_key), True
```

To add a backend: subclass `ArtifactStore`, honour the contracts in the docstrings
(`stream` raises `ArtifactNotFound`; `materialize` returns `(None, False)` and must never raise —
`S3Store` wraps even the `mkstemp` in its `try` for exactly that reason; `read_text` returns `""`),
and wire it into `get_artifact_store()`. Note that `storage_backend = "local"` **without**
`central_local_root` silently falls through to `S3Store` — that is intentional, so a half-configured
mount does not read the local single-node tree and look like it worked.

## Status, failures and reconciliation

**Task status is per-node.** `utils/process.py` sets the *worker-local* task status from the
reporting outcome:

```python
error_count = RunReporting(task=task.to_dict(), results=results, reprocess=reprocess).run()
status = TASK_REPORTED if error_count == 0 else TASK_FAILED_REPORTING
db.set_status(task_id, status)
```

A `centralstore` failure is a reporting-module error, so it counts toward `error_count` → the
worker's task lands in `failed_reporting`, and no `.centralstore.done` marker exists. Propagating
status onto the *central* task row is the broker's job; there is no in-tree code for it.

**Artifact-completion reconciliation** is the marker protocol described above: local marker = purge
gate, store marker = read-side staging-cache gate. If uploads partly failed, neither the purge nor
the cache is enabled, and the UI keeps serving whatever *did* land through the per-file seam.

**Tenancy/visibility reconciliation** (only when `[multitenancy]` is enabled) happens in
`modules/reporting/mongodb.py`:

1. The doc is inserted **fail-closed**: `stamp_tenant_info(report["info"], None)` ⇒
   `visibility=private`, no tenant/user. It is never permissive before it is correct.
2. In `run()`'s `finally`, `_reconcile_report_visibility()` raises it to the authoritative SQL value
   under the same per-task advisory lock the UI's `set_task_visibility` takes.
3. Which database holds that lock depends on the id space: for a rewritten `ui-<N>` id the
   authoritative toggle runs on the **central** node, so the lock and the tenancy re-read must both
   go to `central_database_url`. `_central_lock_engine()` probes `pg_is_in_recovery()` **fresh every
   call** (no caching, so an in-place failover is noticed) and `_connection_in_recovery()` re-checks
   on the very connection that holds the lock. If it cannot get a validated writer-primary lock it
   **skips the upgrade** and logs `visibility reconcile SKIPPED for central task <id>` — the doc
   stays fail-closed private, which is the safe direction and is enumerable from logs.
4. `_reconcile_report_visibility()` never raises: it runs in a `finally`, where an escape would flip
   a fully-stored report to `failed_reporting` or mask the storage block's own exception.

**Pre-insert delete.** Re-reporting the same analysis must delete the previous document. Single-node
uses the upstream `mongo_delete_data([ids])`. Central + a `ui-<N>` job_id instead deletes **only**
`{"info.job_id": <job_id>}` and that document's own call chunks by ObjectId — a bare `info.id` delete
in a shared collection would destroy another worker's colliding document.

**Bridge-required backstop.** When central mode **and** multitenancy are both on
(`central_bridge_required()`), only a bridged `ui-<central_id>` document has a tenant-resolvable
identity. Both write choke points refuse anything else: `centralstore.run()` raises, and
`mongodb.py::_reject_unbridged_under_mt()` raises at the actual DB write (so the invariant holds even
if `centralstore` is disabled or errored). `central_bridge_required()` is itself fail-closed: if the
central-mode or MT config cannot be read, it assumes bridge-required, i.e. the most restrictive
own-doc filter.

## Interactive live-VM attach across nodes

Single-node: the VM is in local libvirt and guacd is on localhost. Central mode: the VM is on an
ephemeral worker, and the central node's `machines` table is empty.

```
UI mints session_data (submission/views.py remote_session / status)
   task.machine empty  ->  central_guac.worker_vm_for_task(task_id)
                              job_id = "ui-<task_id>"        # DERIVED, never read from custom
                              JobDirectory.lookup(job_id)    # -> sandbox_worker_ip, cape_task_id
                              GET http://<worker_ip>:<worker_api_port>/apiv2/tasks/machine/<cape_task_id>/
                                  Authorization: Token <contents of worker_api_token_file>
                              -> {"machine": "<vm label>", "vnc_port": <int|null>}
WebSocket (web/guac/consumers.py)
   libvirt_dsn_for_task()  -> (qemu+ssh://<user>@<worker_ip>/system?keyfile=...&no_verify=1, worker_ip)
   worker_ip truthy  ->  vnc_port = worker_vnc_port_for_task()   # the WORKER's own libvirt, via apiv2
   guacd_hostname = worker_ip ;  guest_host = "localhost"        # worker's guacd reaches its own VM
```

Key points:

* **The `job_id` is derived** (`f"ui-{int(task_id)}"`), never read from `task.custom`. Reading
  `custom` here let a user who submitted `custom=job_id=ui-<victim>` tunnel into another tenant's
  live VM. `can_manage_task` has already authorized the caller's own task id, so deriving binds the
  tunnel to it.
* **The VNC port is resolved by the worker**, not by the UI over SSH. The UI-side libvirt-over-SSH
  lookup intermittently hung or returned `-1` during VM boot and was the cause of Guacamole 519
  `UPSTREAM_NOT_FOUND`. `local_vnc_port()` runs *on the worker* (via `apiv2 tasks/machine`), rejects
  `<= 0`, and retries 3 times at 0.5 s to ride out libvirt's autoport window.
* **`apiv2 tasks/machine/<id>/`** (`web/apiv2/views.py::tasks_machine`) is intentionally not
  tenant-scoped, and the exemption is bounded: it returns only the pool VM label and its VNC port,
  never analysis content or tenant metadata, and it authorizes on either (a) a constant-time match
  of `Authorization: Token <…>` against `[api] control_plane_token` — a machine-to-machine path that
  works even on a `token_auth_enabled = no` worker, where DRF leaves the request anonymous — or
  (b) an `is_local_admin` principal. An **empty** `control_plane_token` disables path (a) entirely
  (it never compares `"" == ""`). Any other caller gets the same generic 404 as a missing task.
  A 401/403/404 from this call is logged with a "verify worker_api_token matches the worker's
  `[api] control_plane_token`" hint, because silently returning `None` is indistinguishable from
  "this task is local".
* **The worker IP is validated once**, in `job_directory.loc_from_item()` → `_valid_worker_ip()`: it
  must parse as an `ipaddress.ip_address`. It becomes the netloc of both a libvirt DSN and a
  token-bearing apiv2 URL, so a poisoned broker record like
  `1.2.3.4/system?keyfile=/attacker/key&no_verify=1&x=` cannot inject DSN parameters or redirect the
  secret. IPv6 literals are bracketed by `_bracket()`; the keyfile path is `quote(…, safe="/")`d.
* **The guac session label and guest IP are always derived from the authorized task**, never from the
  base64 `session_data` path segment (`web/guac/views.py`) — trusting the claimed label let a caller
  tunnel into another VM by name, and trusting the claimed IP let them point guacd at an arbitrary
  `host:3389` (SSRF).

### Job directory backends

`lib/cuckoo/common/job_directory.py`. Both backends return the same shape and read the same field
names, so the broker's storage choice is a detail:

```python
class JobLocation:  # worker_ip (validated, or None), cape_task_id (may be 0)
class JobDirectory:
    def lookup(self, job_id): ...   # -> JobLocation | None
```

* `broker_http` (default, vendor-neutral): `GET {broker_url}/api/status/{job_id}` with
  `Authorization: Bearer {broker_api_token}` when a token is set, 10 s timeout. Response JSON must
  carry `sandbox_worker_ip` and `cape_task_id`. A non-200 is logged with the misconfig hint.
* `dynamodb` (opt-in): `get_item(Key={"job_id": job_id})` on `broker_table` in `s3_region`.

`get_job_directory(cfg)` returns `None` — meaning "keep the local/single-node path" — when central
mode is off, or when the selected backend's required key (`broker_url` / `broker_table`) is unset.
All lookup failures degrade to `None` with a WARNING; they never raise into a view.

## Reprocessing a task in central mode

Reprocessing is a **worker** operation. The central UI's on-demand endpoint returns
`"On-demand detail generation is not available in central mode (re-processing is handled by workers)"`
(`central_views.central_on_demand`).

On the worker that ran the task, using its **worker-local** task id:

```bash
# regenerate processing + signatures + reports for local task 42
python3 utils/process.py -r 42
# a range / list also works: 1-10  or  1,3,5
```

What happens: `RunProcessing` → `RunSignatures` → `RunReporting(..., reprocess=True)`, then
`db.set_status()` on the local DB. `centralstore` runs again and re-uploads the tree under the same
`job_id`, `mongodb.py` performs the `job_id`-scoped pre-insert delete and re-inserts (fail-closed,
then reconciled).

Ordering requirements and traps:

* **The local analysis tree must still exist.** `utils/process.py` skips with
  `Analysis folder doesn't exist anymore for Task #<n>` if `storage/analyses/<id>` is gone. If a
  cleanup job already purged it on the strength of `.centralstore.done`, that worker cannot
  reprocess the task.
* **Use the worker-local id, not the central task id.** `db.view_task(num)` hits the worker's own
  DB and the tree is keyed by the local id — even though the report document's `info.id` was
  rewritten to the central id.
* **`-sig` (signature-only re-run) is not central-aware.** `_load_report()` queries
  `{"info.id": task_id}` with the *local* id, while a bridged document is keyed by the central id,
  and there is no guard for a missing document before `analysis.get("behavior", …)`. Use the full
  `-r` path instead: with `[mongodb] enabled` (which central mode requires) the missing-document
  `AttributeError` fires inside `_load_report()`, before the `report.json` / `--json-report`
  fallbacks can be reached.
* Reprocessing races a visibility toggle only in the narrow window the reconcile serialises; on a
  standby/unset `central_database_url` the reconcile skips and the doc stays private.

## Gate OFF: single-node behaviour

With `[central_mode] enabled = no` (the default) there is **no behavioural change** to a single-node
install: every seam short-circuits at the gate and the upstream code path runs. Verified in this
branch, seam by seam:

| Seam | OFF behaviour |
|---|---|
| `modules/reporting/centralstore.py::run()` | `if not cfg.enabled: return` — and `[centralstore] enabled = no` is the shipped default, so `RunReporting.process()` returns before `run()` is even called. |
| `storage_backend.get_artifact_store()` | `LocalFSStore(<CUCKOO_ROOT>/storage/analyses)`, `is_central=False`. |
| `artifact_storage._store_and_container()` | returns `(store, str(task_id))` immediately — no `job_id` resolution, no Mongo/SQL lookup. |
| `ensure_local_analysis()` / `ensure_local_memory()` | `if not cfg.enabled: return`. |
| `web/analysis/views.py`, `web/apiv2/views.py`, `web/submission/views.py` | each central branch is inside `if central_mode_config().enabled:`; otherwise the upstream body runs. |
| `central_views.scoped_analysis_query()` | returns the bare `{"info.id": int(task_id)}` filter. |
| `central_views.central_delete_analysis()` | delegates to upstream `mongo_delete_data(task_id)`. |
| `job_directory.get_job_directory()` | `None` ⇒ `central_guac.libvirt_dsn_for_task()` returns `(local_dsn, None)` and `worker_vm_for_task()` returns `(None, None)` ⇒ guac uses local libvirt, the configured `guacd_host` and `vnc_host`, and reads the VNC port from local libvirt exactly as before. |
| `modules/reporting/mongodb.py` | `_central_pre` is `False` ⇒ upstream `mongo_delete_data(list(ids_to_delete))`; `_reject_unbridged_under_mt()` is `False`; the tenant stamp/reconcile is additionally gated on `[multitenancy] enabled`, so an MT-off install writes exactly the upstream report shape. |
| `lib/cuckoo/core/startup.py::init_rooter()` | gated on the **separate** `tolerate_missing_rooter` flag (default `no`), so single-node *and* workers still fail fast on a missing rooter. |

Two additions exist regardless of the gate, and neither changes an existing behaviour:

* the `apiv2 tasks/machine/<task_id>/` route, whose authorization is fail-closed (empty
  `control_plane_token` ⇒ `is_local_admin` only; note that with multitenancy **off**, `viewer_for`
  treats every principal as local-admin, which is what keeps single-tenant/AllowAny installs working);
* the `[central_mode]` / `[centralstore]` config sections themselves, both shipped disabled.

## Gotchas / operational notes

* **`[centralstore] enabled = yes` is mandatory on workers.** Without it, `[central_mode]` is inert:
  no uploads, no `info.job_id`, no `info.id` rewrite, and nothing for the reconcile to key on.
* **`centralstore` must stay at `order = 9998`** — before `mongodb` (9999). Reversing them writes a
  report document with no `job_id`, which the read seam cannot resolve.
* **Point `central_database_url` at the writer/primary.** A reader endpoint breaks the reconcile's
  cross-node advisory-lock serialization (`pg_advisory_lock` on a standby excludes nothing on the
  primary) and the under-lock re-read would be replication-lag stale. The worker detects
  `pg_is_in_recovery()`, warns once, and leaves central analyses fail-closed private.
* **Empty `central_database_url` under multitenancy is not an error — it is a warn-once fail-closed.**
  `_warn_central_lock_once("central_database_url is unset")` fires once per process and each affected
  task logs `visibility reconcile SKIPPED`; analyses stamp `private`/unowned and are invisible to
  everyone but break-glass.
* **`tolerate_missing_rooter = yes` belongs on the UI node only.** It is gated on the flag alone,
  *not* on `central_mode.enabled`, precisely so a worker whose rooter died still refuses to start.
* **A missing `.centralstore.done` in the store means every report view re-runs the staging pass**
  (the `.central_staged` cache is never written), so every request re-lists the whole container and
  re-stats every key — already-downloaded files are skipped, but the listing cost repeats forever.
  Check for `centralstore: gave up uploading …/.centralstore.done` in the worker log.
* **Never let a cleanup job purge an analysis without the local `.centralstore.done`.** That marker
  is the only in-tree signal that the artifacts are durable centrally.
* **`job_id` is not an authorization token.** It comes from user-supplied `custom`. Every read
  re-authorizes the resolved document against the viewer scope; only the `task_id → job_id` mapping
  is cached, never a scoped resolution.
* **A first-position forged `custom=job_id=ui-<victim>` is honoured verbatim by `resolve_job_id`.**
  On the broker path the bridge overwrites `custom` and the dispatcher builds it from the queue
  message, so a forgery never reaches a worker; in a bridge-less deployment this is contained by
  topology only. The documented durable fix is a signed/out-of-band `job_id` authenticated against
  the delivering broker.
* **`worker_api_token_file` must equal the worker's `[api] control_plane_token`.** A mismatch shows
  up as a dead interactive attach; the WARNING names the check.
* **A worker's `[database]` is a different id space from the central one.** Anything that resolves a
  central task must go through `central_database_url` (tenancy) or the `job_id` (documents/artifacts).
  Reprocessing and log-reading use worker-local ids.
* **Mongo equality on `null` also matches an *absent* field.** Every document is inserted unstamped,
  so a bare `{info.id: <tid>}` + "tenant is null or ours" filter would re-admit a foreign colliding
  document. That is why `central_own_analysis_filter()` (shared by the visibility toggle, the central
  delete and the comment write) qualifies its arms by `job_id`, and why the bridge-required form drops
  the `info.id` arms entirely.

## Tests

`tests/test_central_mode.py` and `tests/test_central_guac_jobid.py` are pure-logic — no Django,
pymongo or boto3 needed. The rest need the normal CAPE test environment: `test_artifact_jobid.py`
imports `django.http`, the `web/analysis/` tests import the Django `analysis` app, and
`test_task_visibility.py` uses the `db` fixture.

```bash
pytest tests/test_central_mode.py         # config parsing, storage backends, job directory, done marker,
                                          # symlink-exfil guard, bridge-required refusals
pytest tests/test_artifact_jobid.py       # job_id parser + the shared read/write safety guard
pytest tests/test_central_guac_jobid.py   # derived job_id / worker resolution
pytest web/analysis/test_central_scope.py web/analysis/test_central_delete.py
pytest tests/test_mongo_stamp.py tests/test_task_visibility.py
```
