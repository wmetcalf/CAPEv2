import uuid
from base64 import urlsafe_b64decode

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from lib.cuckoo.common.config import Config
from lib.cuckoo.core.database import Database
from users.tenancy import can_view_task


class conditional_login_required:
    def __init__(self, decorator, condition):
        self.decorator = decorator
        self.condition = condition

    def __call__(self, func):
        if not self.condition:
            return func
        return self.decorator(func)

try:
    import libvirt
    LIBVIRT_AVAILABLE = True
except ImportError:
    LIBVIRT_AVAILABLE = False

machinery = Config().cuckoo.machinery
machinery_available = ["kvm", "qemu"]
machinery_dsn = getattr(Config(machinery), machinery).get("dsn", "qemu:///system")
db = Database()


def _error(request, task_id, msg):
    return render(request, "guac/error.html", {
        "error_msg": msg, "error": "remote session", "task_id": task_id,
    })


@conditional_login_required(login_required, settings.WEB_AUTHENTICATION)
def index(request, task_id, session_data):
    # tenant isolation: only mint a live-VM session token for a task the caller
    # may view (hidden == "not found" — no cross-tenant enumeration).
    _task = db.view_task(int(task_id))
    if _task is None or not can_view_task(request.user, _task):
        return _error(request, task_id, "No analysis found with specified ID")

    if not LIBVIRT_AVAILABLE:
        return _error(request, task_id, "Libvirt not available")

    if machinery not in machinery_available:
        return _error(request, task_id, f"Machinery type '{machinery}' is not supported")

    # Central mode: a broker-dispatched job's VM is on a worker — check that
    # worker's libvirt. None => local (single-node), DSN unchanged.
    from lib.cuckoo.common.central_guac import libvirt_dsn_for_task

    dsn, _worker_ip = libvirt_dsn_for_task(int(task_id), machinery_dsn)

    conn = None
    try:
        conn = libvirt.open(dsn)
        if not conn:
            return _error(request, task_id, "Could not connect to hypervisor")

        try:
            session_id, _claimed_label, guest_ip = (
                urlsafe_b64decode(session_data).decode("utf8").split("|")
            )
        except Exception as e:
            return _error(request, task_id, str(e))

        # SECURITY: the VM label MUST come from the authorized task, never from the
        # attacker-controlled session_data path segment — otherwise a tenant who can
        # view ONE of their own running tasks could pass another tenant's VM label and
        # tunnel into that tenant's live analysis VM (cross-tenant takeover). Derive the
        # authoritative label from the gated task: _task.machine (single-node) or the
        # worker's VM (central mode). Ignore _claimed_label entirely.
        label = _task.machine
        if not label:
            from lib.cuckoo.common.central_guac import worker_vm_for_task

            label, _gip = worker_vm_for_task(int(task_id))
        if not label:
            return _error(request, task_id, "No VM is associated with this task")

        try:
            dom = conn.lookupByName(label)
        except Exception as e:
            return _error(request, task_id, str(e))

        if not dom:
            return _error(request, task_id, f"VM {label} not found")

        state = dom.state(flags=0)
        if not state or state[0] != 1:
            return render(request, "guac/wait.html", {"task_id": task_id})

        # VM is running — get or create a session token
        recording_name = f"{task_id}_{session_id}"
        token = uuid.uuid4()

        guac_session = db.create_guac_session(
            token=token,
            task_id=int(task_id),
            vm_label=label,
            guest_ip=guest_ip,
        )

        response = render(request, "guac/index.html", {
            "session_id": session_id,
            "task_id": task_id,
            "recording_name": recording_name,
        })

        response.set_cookie(
            "guac_session",
            str(guac_session.token),
            httponly=True,
            secure=request.is_secure(),
            samesite="Lax",
            path="/guac/",
        )

        return response

    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
