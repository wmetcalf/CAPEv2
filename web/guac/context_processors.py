import logging

from lib.cuckoo.common.config import Config
from lib.cuckoo.core.database import Database

web_cfg = Config("web")
log = logging.getLogger("guac-session")


def guac_vnc_console(request):
    """Context processor that exposes VNC Console settings and guests to templates."""
    enabled = web_cfg.guacamole.get("vnc_console_enabled", False)
    if isinstance(enabled, str):
        enabled = enabled.lower() in ("yes", "true", "on", "1")
    if not enabled:
        return {"vnc_console_enabled": False}

    # Runs on EVERY page render (context processor) — never let a DB hiccup 500 the
    # whole site; fail to an empty machine list instead (gemini review, PR #12).
    try:
        db = Database()
        machines = [machine.label for machine in db.list_machines(include_reserved=True)]
    except Exception as e:
        log.error("VNC console context processor: failed to list machines: %s", e)
        machines = []
    new_tab = web_cfg.guacamole.get("vnc_console_new_tab", True)
    if isinstance(new_tab, str):
        new_tab = new_tab.lower() in ("yes", "true", "on", "1")

    return {
        "vnc_console_enabled": True,
        "vnc_console_machines": machines,
        "vnc_console_new_tab": new_tab,
    }
