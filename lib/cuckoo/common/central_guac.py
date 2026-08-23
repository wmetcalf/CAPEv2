"""Central-mode interactive-Guacamole worker routing.

Single-node: a task's live analysis VM is in local libvirt and guacd is on
localhost. In the broker/autoscaling topology the VM runs on an ephemeral ASG
worker, so the central guac consumer must target THAT worker's guacd + VM.

worker_ip_for_task() resolves task_id (info.id) -> info.job_id -> the broker job
record's sandbox_worker_ip (recorded by the dispatcher at dispatch time) -> the
worker's private IP. The job record is fetched via a pluggable JobDirectory
(job_directory.py): DynamoDB by default, or the broker's HTTP status API — so the
fork carries no hard DynamoDB/boto3 dependency. Returns None for box-local /
single-node tasks (no broker record), so the consumer/view keep their localhost
path unchanged when central mode is off or the task ran locally.
"""
import logging

log = logging.getLogger(__name__)


def _job_id_for_task(task_id):
    """Resolve the broker job_id for a task. Prefer the RDS task.custom stamp
    ('job_id=ui-<id>', set by the submit-bridge at enqueue) — it exists DURING the
    live run, which is exactly when interactive guac is needed. The DocumentDB
    analysis doc is only written at reporting (after the VM is gone), so it can't be
    relied on here; fall back to it only for non-bridged/seeded tasks."""
    try:
        from lib.cuckoo.core.database import Database

        t = Database().view_task(int(task_id))
        custom = getattr(t, "custom", None) if t else None
        if custom:
            # comma-separated k=v pairs — take ONLY the job_id= value (not the rest of the
            # string), matching centralstore.resolve_job_id; a trailing ',foo=bar' would
            # otherwise corrupt the DynamoDB key / S3 prefix lookup.
            for part in str(custom).split(","):
                part = part.strip()
                if part.startswith("job_id="):
                    v = part.split("=", 1)[1].strip()
                    if v:
                        return v
    except Exception:
        pass
    try:
        from dev_utils.mongodb import mongo_find_one

        doc = mongo_find_one("analysis", {"info.id": int(task_id)}, {"info.job_id": 1})
        return (doc or {}).get("info", {}).get("job_id")
    except Exception:
        return None


def worker_ip_for_task(task_id):
    """Private IP of the worker hosting this task's live VM, or None (local)."""
    from lib.cuckoo.common.central_mode import central_mode_config
    from lib.cuckoo.common.job_directory import get_job_directory

    cfg = central_mode_config()
    directory = get_job_directory(cfg)
    if directory is None:
        return None
    try:
        job_id = _job_id_for_task(task_id)
        if not job_id:
            return None
        loc = directory.lookup(job_id)
        return loc.worker_ip if loc else None
    except Exception as e:
        log.warning("central guac: worker resolution failed for task %s: %s", task_id, e)
        return None


def worker_vm_for_task(task_id):
    """For a live broker-dispatched interactive task, return (vm_label, guest_ip) of the
    VM on the worker — needed to build the guac session_data on the central node, where
    the local machines table is empty (the VM lives on the worker). Resolves the broker
    record (job_id -> worker IP + the worker-local cape_task_id) then asks that worker's
    apiv2 for the task's machine. Returns (None, None) for non-bridged/local tasks."""
    from lib.cuckoo.common.central_mode import central_mode_config
    from lib.cuckoo.common.job_directory import get_job_directory

    cfg = central_mode_config()
    directory = get_job_directory(cfg)
    if directory is None:
        return (None, None)
    try:
        job_id = _job_id_for_task(task_id)
        if not job_id:
            return (None, None)
        loc = directory.lookup(job_id)
        if not loc:
            return (None, None)
        worker_ip = loc.worker_ip
        cape_task_id = loc.cape_task_id
        if not worker_ip or cape_task_id is None:
            return (None, None)

        import requests

        token = ""
        try:
            token = open("/etc/cape/api-token").read().strip()
        except Exception:
            pass
        headers = {"Authorization": f"Token {token}"} if token else {}
        r = requests.get(f"http://{worker_ip}:8000/apiv2/tasks/view/{int(cape_task_id)}/",
                         headers=headers, timeout=10)
        data = (r.json() or {}).get("data", {})
        return (data.get("machine"), None)  # central guac uses the worker's localhost for VNC
    except Exception as e:
        log.warning("central guac: worker VM lookup failed for task %s: %s", task_id, e)
        return (None, None)


def libvirt_dsn_for_task(task_id, local_dsn):
    """libvirt DSN to query the VM's VNC port: the worker's libvirt over SSH for a
    worker-hosted task, else the local DSN. (Requires the central node's cape user
    to hold an SSH key authorized on workers — deploy-time plumbing.)"""
    ip = worker_ip_for_task(task_id)
    if not ip:
        return (local_dsn, None)
    # cape's baked SSH key authorizes the central node onto workers; no_verify skips
    # host-key prompts for ephemeral in-VPC workers.
    dsn = f"qemu+ssh://cape@{ip}/system?keyfile=/home/cape/.ssh/id_ed25519&no_verify=1"
    return (dsn, ip)
