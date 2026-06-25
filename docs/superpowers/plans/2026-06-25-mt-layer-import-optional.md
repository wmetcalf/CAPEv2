# MT Layer Import-Optional (Phase 4 core) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the multi-tenant (MT) layer **import-optional** so central mode (and the base CAPE web/api app) runs with the MT layer *physically absent*, defaulting to single-tenant see-all — without changing behavior one bit when the MT layer **is** present (our deployments). This lets central mode be proposed upstream as a vendor-neutral "distributed/shared-storage mode" while our MT layer stays a private add-on on top.

**Architecture:** Two import-safe **tenancy facades** become the single choke point through which all non-MT code reaches MT functionality: `lib/cuckoo/common/tenancy_optional.py` (the lib-level symbols) and `web/web/tenancy_optional.py` (the web-level symbols + `entitled_scope_filter`). Each symbol **delegates** to the real MT module when importable, and falls back to a value **identical to the MT-*disabled* code path** when the MT module raises `ImportError`. Because the real functions already implement see-all when `multitenancy_config().enabled` is False (`viewer_for` → `is_local_admin=True` → every `can_*` gate returns `True`; `scope_match`/`submission_scope`/`entitled_scope_filter` → `None`), the absent-layer fallback is provably the same behavior as a present-but-disabled layer. We then **redirect the 6 module-level imports** (+ a handful of lazy ones) from `users.tenancy` / `lib.cuckoo.common.tenancy` / `dashboard.views` to the facades; the ~300 call sites are unchanged. `INSTALLED_APPS` makes `users` conditional, and the one ORM coupling (`allauth_adapters`) is guarded.

**Tech Stack:** Python 3.12, Django, CAPEv2 web app. Tests: pytest + the faithful harness (rsync checkout to the live box, `PYTHONPATH=... DJANGO_SETTINGS_MODULE=web.settings /opt/CAPEv2/bin/python -m pytest`). Pure-logic facade tests run locally via `pytest --noconftest`.

**FAIL-CLOSED INVARIANT (non-negotiable, per the tenant-isolation audit):** the facades catch **`ImportError` only** (= MT layer not deployed). When the MT layer **is** deployed, its functions are authoritative and any *runtime* error propagates — never silently degrades to see-all (which would bypass tenant isolation). This is the same contract already proven in `web/analysis/central_scope.py`.

---

## Background: the exact coupling (from the 2026-06-25 audit)

**6 module-level imports to redirect:**
- `web/analysis/views.py:195` — `from users.tenancy import can_view_task, can_toggle_task, can_manage_task, can_view_sample, viewer_for, can_ban_user`
- `web/apiv2/views.py:33` — `from users.tenancy import submission_scope, can_view_task, can_toggle_task, can_manage_task, can_view_sample, viewer_for`
- `web/submission/views.py:18-19` — both `users.tenancy` (submission_scope, can_view_task, can_view_sample, viewer_for) and a `lib.cuckoo.common.tenancy` import
- `web/dashboard/views.py:18` — `import users.tenancy as _ut` (used by `entitled_scopes`/`entitled_scope_filter`, defined here)
- `web/guac/views.py:14` — `from users.tenancy import can_view_task`
- `web/compare/views.py:17` — `from users.tenancy import can_view_task`
- `lib/cuckoo/common/cape_utils.py` — module-level `viewer_scope_*` / tenancy imports (Tier 3)

**Symbols + their MT-disabled (= absent-fallback) value:**

| Symbol | Source module | MT-disabled / absent fallback | Why |
|---|---|---|---|
| `viewer_for(user)` | `lib.cuckoo.common.tenancy` | `Viewer(is_local_admin=True)` | MT-off sets `is_local_admin=True` (tenancy.py:27) |
| `Viewer` (dataclass) | `lib.cuckoo.common.tenancy` | local minimal dataclass w/ `is_local_admin=True` default | needed to construct the fallback viewer |
| `MTConfig` (dataclass) | `lib.cuckoo.common.tenancy` | local minimal dataclass, `enabled=False` | config shape |
| `multitenancy_config()` | `lib.cuckoo.common.tenancy` | `MTConfig(enabled=False)` | see-all |
| `scope_match(scope, viewer)` | `lib.cuckoo.common.tenancy` | `None` | returns None for local-admin |
| `can_view_task(user, task)` | `users.tenancy` | `True` | `if viewer.is_local_admin: return True` |
| `can_toggle_task(user, task)` | `users.tenancy` | `True` | same |
| `can_manage_task(user, task)` | `users.tenancy` | `True` | same |
| `can_view_sample(user, **h)` | `users.tenancy` | `True` | tenancy.py:90-91 |
| `can_ban_user(actor, target)` | `users.tenancy` | `True` | tenancy.py:119 (matches MT-off; the ban_user *view* still applies its own Django staff gate) |
| `submission_scope(request)` | `users.tenancy` | `None` | see-all |
| `entitled_scope_filter(user)` | `dashboard.views` | `None` | returns None when MT off (dashboard/views.py:67) |
| `entitled_scopes(user)` | `dashboard.views` | `("global",)` | local-admin sees global |

**Other couplings:**
- `web/web/settings.py:260` — `INSTALLED_APPS` unconditionally lists `"users"`.
- `web/web/allauth_adapters.py:301` — `from users.models import Tenant` (login-time IdP reconciliation). Lazy + guarded.
- Already-lazy/safe (no change needed): `lib/cuckoo/core/data/tasking.py`, most of `lib/cuckoo/common/web_utils.py`, `web/guac/consumers.py`, `web/analysis/central_scope.py`.

**Non-goal:** removing the MT layer or changing MT behavior. With the layer present (every real deployment), the facades are transparent pass-throughs; the faithful suite must stay 100% green to prove that.

---

## File Structure

- **Create `lib/cuckoo/common/tenancy_optional.py`** — lib-level facade: `viewer_for`, `Viewer`, `MTConfig`, `multitenancy_config`, `scope_match`. Pure stdlib + lazy import of `lib.cuckoo.common.tenancy`. Django-free.
- **Create `web/web/tenancy_optional.py`** — web-level facade: `can_view_task`, `can_toggle_task`, `can_manage_task`, `can_view_sample`, `can_ban_user`, `submission_scope` (from `users.tenancy`); `entitled_scope_filter`, `entitled_scopes` (from `dashboard.views`). Re-exports the lib facade's symbols too so a view has one import.
- **Modify** the 6 base view modules' import lines (+ `cape_utils.py`) to import from the facades.
- **Modify `web/web/settings.py`** — conditional `users` in `INSTALLED_APPS`.
- **Modify `web/web/allauth_adapters.py`** — guard the `Tenant` import.
- **Create `tests/test_tenancy_optional.py`** — pure-logic facade tests (delegate-when-present, MT-disabled-equiv-when-absent, fail-closed-on-runtime-error).
- **Create `web/web/test_settings_mt_optional.py`** — settings conditionalization test.

`central_scope.py` (Phase 3) stays; optionally later it can re-export from `web/web/tenancy_optional.py` to dedupe, but that's out of scope here.

---

## Task 1: lib-level tenancy facade

**Files:**
- Create: `lib/cuckoo/common/tenancy_optional.py`
- Test: `tests/test_tenancy_optional.py`

- [ ] **Step 1: Write the failing test (lib facade)**

```python
# tests/test_tenancy_optional.py
import builtins
import pytest
from lib.cuckoo.common import tenancy_optional as topt


def _hide(monkeypatch, modname):
    real_import = builtins.__import__
    def fake(name, *a, **k):
        if name == modname or name.startswith(modname + "."):
            raise ImportError(f"simulated-absent: {modname}")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)


def test_lib_facade_present_delegates(monkeypatch):
    # MT present + enabled: multitenancy_config().enabled is authoritative
    cfg = topt.multitenancy_config()
    assert hasattr(cfg, "enabled")


def test_lib_facade_absent_is_see_all(monkeypatch):
    _hide(monkeypatch, "lib.cuckoo.common.tenancy")
    assert topt.multitenancy_config().enabled is False
    assert topt.viewer_for(object()).is_local_admin is True
    assert topt.scope_match("acme", topt.viewer_for(object())) is None
```

- [ ] **Step 2: Run it, expect failure** — `pytest tests/test_tenancy_optional.py -q --noconftest` → ImportError (module not yet created).

- [ ] **Step 3: Implement `lib/cuckoo/common/tenancy_optional.py`**

```python
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
```

- [ ] **Step 4: Run the tests, expect pass** — `pytest tests/test_tenancy_optional.py -q --noconftest` → PASS.

- [ ] **Step 5: Commit** — `git add lib/cuckoo/common/tenancy_optional.py tests/test_tenancy_optional.py && git commit -m "central-mode: lib-level import-optional tenancy facade"`

---

## Task 2: web-level tenancy facade

**Files:**
- Create: `web/web/tenancy_optional.py`
- Test: add to `tests/test_tenancy_optional.py` (web symbols; run on the box where Django is available)

- [ ] **Step 1: Write the failing test** — extend the test file:

```python
def test_web_facade_absent_is_see_all(monkeypatch):
    _hide(monkeypatch, "users.tenancy")
    from web.web import tenancy_optional as wopt
    assert wopt.can_view_task(object(), object()) is True
    assert wopt.can_view_sample(object(), sha256="x") is True
    assert wopt.submission_scope(object()) is None
    assert wopt.can_ban_user(object(), 1) is True


def test_web_facade_entitled_scope_filter_absent(monkeypatch):
    _hide(monkeypatch, "dashboard.views")
    from web.web import tenancy_optional as wopt
    assert wopt.viewer_scope_filter(object()) is None  # entitled_scope_filter facade


def test_web_facade_fail_closed_on_runtime_error(monkeypatch):
    import users.tenancy as ut
    def boom(*a, **k):
        raise RuntimeError("authz backend down")
    monkeypatch.setattr(ut, "can_view_task", boom)
    from web.web import tenancy_optional as wopt
    with pytest.raises(RuntimeError):
        wopt.can_view_task(object(), object())
```

- [ ] **Step 2: Run on the box, expect failure** (module missing).

- [ ] **Step 3: Implement `web/web/tenancy_optional.py`**

```python
"""Import-optional facade for the web-level MT symbols (users.tenancy + the
entitled_scope_filter/entitled_scopes defined in dashboard.views).

Same contract as the lib facade: delegate when the MT layer is importable, fall back to the
MT-disabled-equivalent value when it raises ImportError, FAIL-CLOSED on runtime errors. Re-
exports the lib-level facade symbols so a view needs a single import.
"""
from lib.cuckoo.common.tenancy_optional import MTConfig, Viewer, multitenancy_config, scope_match, viewer_for  # noqa: F401


def can_view_task(user, task):
    try:
        from users.tenancy import can_view_task as real
    except ImportError:
        return True
    return real(user, task)


def can_toggle_task(user, task):
    try:
        from users.tenancy import can_toggle_task as real
    except ImportError:
        return True
    return real(user, task)


def can_manage_task(user, task):
    try:
        from users.tenancy import can_manage_task as real
    except ImportError:
        return True
    return real(user, task)


def can_view_sample(user, *, sha256=None, sha1=None, md5=None, sample_id=None):
    try:
        from users.tenancy import can_view_sample as real
    except ImportError:
        return True
    return real(user, sha256=sha256, sha1=sha1, md5=md5, sample_id=sample_id)


def can_ban_user(actor, target_user_id):
    # MT-disabled returns True (is_local_admin); the ban_user VIEW still applies its own
    # Django staff/permission gate, so this is not the sole authz boundary.
    try:
        from users.tenancy import can_ban_user as real
    except ImportError:
        return True
    return real(actor, target_user_id)


def submission_scope(request):
    try:
        from users.tenancy import submission_scope as real
    except ImportError:
        return None
    return real(request)


def viewer_scope_filter(user):
    """Facade for dashboard.views.entitled_scope_filter (None = see-all)."""
    try:
        from dashboard.views import entitled_scope_filter as real
    except ImportError:
        return None
    return real(user)


def entitled_scopes(user):
    try:
        from dashboard.views import entitled_scopes as real
    except ImportError:
        return ("global",)
    return real(user)
```

- [ ] **Step 4: Run on the box, expect pass.**

- [ ] **Step 5: Commit** — `git commit -am "central-mode: web-level import-optional tenancy facade"`

---

## Task 3: Redirect the base-view module-level imports

**Files (modify the import line ONLY — call sites unchanged):**
- `web/analysis/views.py:195`
- `web/apiv2/views.py:33`
- `web/submission/views.py:18-19`
- `web/dashboard/views.py:18`
- `web/guac/views.py:14`
- `web/compare/views.py:17`
- `lib/cuckoo/common/cape_utils.py` (Tier 3)

- [ ] **Step 1: Redirect each import to the facade.** Examples:

```python
# web/analysis/views.py:195  (was: from users.tenancy import can_view_task, can_toggle_task, can_manage_task, can_view_sample, viewer_for, can_ban_user)
from web.tenancy_optional import can_view_task, can_toggle_task, can_manage_task, can_view_sample, viewer_for, can_ban_user
```
```python
# web/dashboard/views.py: entitled_scope_filter/entitled_scopes are DEFINED here and depend on
# users.tenancy via `_ut`. Redirect `_ut` to the lib+web facades so their internals degrade:
#   import web.tenancy_optional as _ut    # provides viewer_for; multitenancy_config; scope_match
# (entitled_scope_filter already returns None when multitenancy_config().enabled is False, so
#  with the facade reporting enabled=False under an absent layer it naturally yields see-all.)
```
> Note the import path: from a Django app module, `from web.tenancy_optional import ...` (the `web` project package). Verify the exact dotted path against how `web/web/` is on `sys.path` in this project (it may be `from web.tenancy_optional` or just `tenancy_optional` depending on settings). Match the style already used for `web.settings`.

- [ ] **Step 2: Grep to confirm no remaining direct MT imports in the redirected files.**

```bash
grep -nE "from users.tenancy import|import users.tenancy|from lib.cuckoo.common.tenancy import|from dashboard.views import entitled_scope" web/analysis/views.py web/apiv2/views.py web/submission/views.py web/dashboard/views.py web/guac/views.py web/compare/views.py
```
Expected: empty (all now go through the facade). `central_scope.py` may keep its own (it's already guarded).

- [ ] **Step 3: Run the faithful suite (MT PRESENT) — must stay green.**

```bash
# rsync checkout to the box, then:
PYTHONPATH=/home/cape/cape-dev:/home/cape/pytest-libs DJANGO_SETTINGS_MODULE=web.settings /opt/CAPEv2/bin/python -m pytest \
  tests/test_central_mode.py tests/test_tenancy.py tests/test_task_visibility.py tests/test_scope_stats.py \
  web/guac/test_direct_vnc.py web/guac/test_visibility.py web/users/test_tenancy.py \
  web/analysis/test_visibility.py web/analysis/test_central_scope.py \
  web/apiv2/test_hash_isolation.py web/apiv2/test_visibility.py tests/test_tenancy_optional.py -q -p no:cacheprovider
```
Expected: all pass (facade is a transparent pass-through when MT present). This is the proof of zero behavior change.

- [ ] **Step 4: Commit** — `git commit -am "central-mode: route base views through the import-optional tenancy facade"`

---

## Task 4: Conditional INSTALLED_APPS + guarded ORM import

> **IMPLEMENTATION NOTE (post-review, 2026-06-25):** the `CAPE_DISABLE_MT_APP` env flag below
> was REPLACED by presence-detection — settings drops `users` from INSTALLED_APPS iff
> `(BASE_DIR / "users").is_dir()` is false, the SAME import-availability signal the facades
> use. The env flag diverged from the facades (flag set + files present → facades still imported
> the real code → non-migrated-table errors). Single signal now: file presence. Runtime
> single-tenant on a files-present deploy uses `[multitenancy] enabled = no` (existing toggle),
> not app removal. allauth guard narrowed to `except ImportError` only (not `(ImportError, Exception)`).

**Files:**
- Modify: `web/web/settings.py` (~line 260)
- Modify: `web/web/allauth_adapters.py` (~line 301)
- Test: `web/web/test_settings_mt_optional.py`

- [ ] **Step 1: Make `users` conditional in settings.**

```python
# web/web/settings.py — replace the static "users" entry with a flag-gated append.
# MT_LAYER_INSTALLED defaults True (our deployments); an upstream/no-MT build sets
# CAPE_DISABLE_MT_APP=1 to drop the app + its migrations.
import os
_MT_APP = os.environ.get("CAPE_DISABLE_MT_APP", "") not in ("1", "true", "yes")
INSTALLED_APPS = [
    # ... existing apps WITHOUT "users" ...
]
if _MT_APP:
    INSTALLED_APPS.append("users")
```
> Keep `users` in its current ordering relative to other apps if ordering matters (e.g. it must precede apps that reference its models). Verify against the current list before moving it.

- [ ] **Step 2: Guard the allauth ORM import.**

```python
# web/web/allauth_adapters.py — reconcile_tenant() is the only place importing the Tenant
# model. Make it a no-op when the model/app is absent.
def reconcile_tenant(user, groups):
    try:
        from users.models import Tenant
    except (ImportError, Exception):  # app not installed / models unavailable
        return
    ...  # existing body
```
> Use the narrowest exception that covers `ImproperlyConfigured`/`AppRegistryNotReady`/`ImportError` for an uninstalled app; do not swallow logic errors inside the existing body — keep the try around the import only.

- [ ] **Step 3: Test (pure settings logic).**

```python
# web/web/test_settings_mt_optional.py
import importlib, os
def test_users_app_droppable(monkeypatch):
    monkeypatch.setenv("CAPE_DISABLE_MT_APP", "1")
    import web.settings as s
    importlib.reload(s)
    assert "users" not in s.INSTALLED_APPS
    monkeypatch.delenv("CAPE_DISABLE_MT_APP")
    importlib.reload(s)
    assert "users" in s.INSTALLED_APPS
```
> Reloading settings is fiddly under Django; if reload is impractical, instead unit-test a small helper `mt_app_enabled(env)` extracted from settings and assert on it. Prefer the helper.

- [ ] **Step 4: Run; expect pass. Commit** — `git commit -am "central-mode: make the users (MT) app + ORM coupling optional"`

---

## Task 5: MT-absent integration smoke (the real proof)

**Files:**
- Create: `tests/test_mt_absent_smoke.py` (box-run)

- [ ] **Step 1: Simulate the layer absent and assert the app still imports + serves see-all.**

```python
# Run with CAPE_DISABLE_MT_APP=1 and the users.tenancy / lib.cuckoo.common.tenancy modules
# made unimportable (rename on a scratch copy, or monkeypatch builtins.__import__ as in
# Task 1). Then: django.setup() succeeds; a Client() GET of /analysis/<id>/ returns 200
# (see-all), and an artifact endpoint returns the bytes for ANY authenticated user (no tenant
# scoping, since there are no tenants). Assert NO ImportError / 500.
```
> This is the end-to-end equivalent of the live read-smoke already done for MT-present. Keep it box-run (needs Django + real-ish config); gate it so it only runs when explicitly invoked.

- [ ] **Step 2: Run on the box; expect the app boots + serves with the MT layer absent.**

- [ ] **Step 3: Commit** — `git commit -am "central-mode: MT-absent integration smoke"`

---

## Self-Review checklist (run before executing)

1. **Spec coverage:** every one of the 6 module-level imports (Task 3) + settings + ORM (Task 4) + both facades (Tasks 1–2) + the MT-absent proof (Task 5) is covered. ✔
2. **No placeholder authz:** every `can_*` fallback value is justified against the MT-disabled code path in the table above; `can_ban_user`/`can_manage_task` defaults noted as relying on the view's own gate. ✔
3. **Fail-closed:** every facade function catches `ImportError` ONLY — verify no `except Exception` crept in (Task 2 Step 3 test `test_web_facade_fail_closed_on_runtime_error` enforces it). ✔
4. **Import path:** confirm `from web.tenancy_optional import ...` vs `from tenancy_optional import ...` against this project's `sys.path` before redirecting (Task 3 note). One wrong path breaks every base view — validate with the faithful suite (Task 3 Step 3) which imports all of them.
5. **Zero behavior change when MT present:** the faithful suite staying 100% green (Task 3 Step 3) is the gate; do not proceed past Task 3 if any test regresses.

## Validation summary

- Facade unit tests (delegate / absent-see-all / fail-closed) green.
- Faithful suite green with MT **present** (transparent pass-through — zero behavior change; this is the safety gate).
- MT-**absent** smoke: app boots + serves see-all with `users.tenancy` / `lib.cuckoo.common.tenancy` unimportable and `CAPE_DISABLE_MT_APP=1`.
- Live: no redeploy needed for our nodes (facade transparent); the MT-absent build is what an upstream/non-MT deployment would use.

## Open items

- **Facade import path** (`web.tenancy_optional` vs `tenancy_optional`) — resolve at Task 3 against the project layout.
- **`cape_utils.py` (Tier 3)** + any `viewer_scope_*` module-level imports there — fold into Task 3 if the same pattern; if it needs lib-only symbols, it imports from the lib facade.
- **`central_scope.py` dedupe** — optionally re-export from `web/web/tenancy_optional.py` later (not required for correctness).
- **Templates** referencing tenant fields degrade naturally (a None/absent attr renders empty) — verify in the Task 5 smoke, fix any that hard-fail.
