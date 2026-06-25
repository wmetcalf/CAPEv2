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
