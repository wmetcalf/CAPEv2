"""Central-mode interactive-Guacamole worker routing.

Single-node: a task's live analysis VM is in local libvirt and guacd is on
localhost. In the broker/autoscaling topology the VM runs on an ephemeral ASG
worker, so the central guac consumer must target THAT worker's guacd + VM.

worker_ip_for_task() resolves task_id (info.id) -> info.job_id (DocumentDB) ->
the broker DynamoDB job record's sandbox_worker_ip (recorded by the dispatcher
at dispatch time) -> the worker's private IP. Returns None for box-local /
single-node tasks (no broker record), so the consumer/view keep their localhost
path unchanged when central mode is off or the task ran locally.
"""
import logging

log = logging.getLogger(__name__)


def worker_ip_for_task(task_id):
    """Private IP of the worker hosting this task's live VM, or None (local)."""
    from lib.cuckoo.common.central_mode import central_mode_config

    cfg = central_mode_config()
    if not cfg.enabled or not cfg.broker_table:
        return None
    try:
        from dev_utils.mongodb import mongo_find_one

        doc = mongo_find_one("analysis", {"info.id": int(task_id)}, {"info.job_id": 1})
        job_id = (doc or {}).get("info", {}).get("job_id")
        if not job_id:
            return None
        import boto3

        item = (
            boto3.resource("dynamodb", region_name=cfg.s3_region)
            .Table(cfg.broker_table)
            .get_item(Key={"job_id": job_id})
            .get("Item", {})
        )
        return item.get("sandbox_worker_ip") or None
    except Exception as e:
        log.warning("central guac: worker resolution failed for task %s: %s", task_id, e)
        return None


def libvirt_dsn_for_task(task_id, local_dsn):
    """libvirt DSN to query the VM's VNC port: the worker's libvirt over SSH for a
    worker-hosted task, else the local DSN. (Requires the central node's cape user
    to hold an SSH key authorized on workers — deploy-time plumbing.)"""
    ip = worker_ip_for_task(task_id)
    return (f"qemu+ssh://cape@{ip}/system", ip) if ip else (local_dsn, None)
