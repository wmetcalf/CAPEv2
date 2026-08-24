"""CAPE Sandbox -> sandbox-forensics-v1 adapter.

Translates a CAPE ``report.json`` (as produced by ``modules/reporting/jsondump.py``)
into the canonical sandbox-forensics-v1 envelope.

This is intentionally permissive about missing keys — CAPE reports vary
in completeness depending on what the analysis actually produced.
Defaults populate to sentinel-empty (``[]``, ``{}``, ``""``, ``0.0``)
rather than raising; downstream consumers are expected to treat those
as "no data" rather than "no field".
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Iterable

from ..schema import (
    DISPOSITION_BENIGN,
    DISPOSITION_MALICIOUS,
    DISPOSITION_SUSPICIOUS,
    DISPOSITION_UNKNOWN,
    SCHEMA_VERSION,
    TARGET_TYPE_FILE,
    TARGET_TYPE_URL,
)


# CAPE timestamps are "YYYY-MM-DD HH:MM:SS" (no timezone, assumed UTC).
# v1 schema requires RFC 3339 date-time, so we coerce.
_CAPE_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _iso(ts: Any) -> str:
    """Coerce a CAPE timestamp string to ISO 8601 with UTC offset."""
    if not ts:
        return ""
    if isinstance(ts, (int, float)):
        return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat()
    s = str(ts).strip()
    # CAPE may already emit ISO; pass through if so.
    if "T" in s:
        return s
    try:
        d = _dt.datetime.strptime(s, _CAPE_TS_FMT).replace(tzinfo=_dt.timezone.utc)
        return d.isoformat()
    except ValueError:
        return s  # last-resort: pass through unchanged


def _disposition_from_score(score: float) -> list[str]:
    """Map CAPE's 0-10 malscore to v1 disposition vocabulary.

    CAPE itself uses informal cutoffs at ~5 (malicious) and ~3
    (suspicious). Match those.
    """
    if score >= 5.0:
        return [DISPOSITION_MALICIOUS]
    if score >= 3.0:
        return [DISPOSITION_SUSPICIOUS]
    if score > 0.0:
        return [DISPOSITION_UNKNOWN]
    return [DISPOSITION_BENIGN]


def _families(report: dict) -> list[str]:
    families: list[str] = []
    f = report.get("malfamily") or ""
    if f:
        families.append(f)
    # CAPE config extractors populate "CAPE.configs" with per-family configs;
    # the top-level keys are family names.
    for cfg in (report.get("CAPE") or {}).get("configs") or []:
        if isinstance(cfg, dict):
            for fam in cfg.keys():
                if fam not in families:
                    families.append(fam)
    return families


def _detections(report: dict) -> list[dict]:
    """Flatten CAPE signatures + ClamAV hits + YARA matches into a single
    detection list. Each entry is::

        {"engine": "<engine>", "rule": "<rule>", "severity": "<sev>",
         "categories": [...], "description": "<desc>"}
    """
    out: list[dict] = []

    # CAPE behavioral signatures
    for sig in report.get("signatures") or []:
        out.append(
            {
                "engine": "cape_signature",
                "rule": sig.get("name", ""),
                "severity": _cape_severity(sig.get("severity")),
                "categories": list(sig.get("categories") or []),
                "description": sig.get("description", ""),
            }
        )

    # ClamAV hits live under static.clamav (newer CAPE) or
    # info.clamav (older). It's a list of strings (sig names).
    for clam in _iter_clamav(report):
        out.append(
            {
                "engine": "clamav",
                "rule": clam,
                "severity": "medium",
                "categories": ["antivirus"],
                "description": "",
            }
        )

    # YARA matches under static.yara / target.file.yara / behavior.yara
    for y in _iter_yara(report):
        if not isinstance(y, dict):
            continue
        out.append(
            {
                "engine": "yara",
                "rule": y.get("name", ""),
                "severity": _yara_severity(y),
                "categories": list((y.get("meta") or {}).get("categories", []) or []),
                "description": (y.get("meta") or {}).get("description", ""),
            }
        )

    # Suricata alerts on the network capture
    suricata = ((report.get("network") or {}).get("suricata") or {}).get("alerts") or []
    for alert in suricata:
        if not isinstance(alert, dict):
            continue
        out.append(
            {
                "engine": "suricata",
                "rule": alert.get("signature", ""),
                "severity": _suricata_severity(alert.get("severity")),
                "categories": [alert.get("category", "")] if alert.get("category") else [],
                "description": alert.get("signature", ""),
            }
        )

    return out


def _iter_clamav(report: dict) -> Iterable[str]:
    seen: set[str] = set()
    for path in (
        ("static", "clamav"),
        ("target", "file", "clamav"),
        ("info", "clamav"),
    ):
        node: Any = report
        for k in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(k)
        if isinstance(node, list):
            for s in node:
                if isinstance(s, str) and s not in seen:
                    seen.add(s)
                    yield s


def _iter_yara(report: dict) -> Iterable[dict]:
    for path in (
        ("static", "yara"),
        ("target", "file", "yara"),
        ("behavior", "yara"),
        ("CAPE", "yara"),
    ):
        node: Any = report
        for k in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(k)
        if isinstance(node, list):
            yield from (n for n in node if isinstance(n, dict))


def _cape_severity(s: Any) -> str:
    # CAPE signature severity is integer 1-3 (informational, suspicious, malicious)
    try:
        n = int(s)
    except (TypeError, ValueError):
        return "info"
    return {1: "info", 2: "medium", 3: "high"}.get(n, "info")


def _yara_severity(rule: dict) -> str:
    meta = rule.get("meta") or {}
    if isinstance(meta.get("severity"), str):
        return meta["severity"].lower()
    return "medium"


def _suricata_severity(s: Any) -> str:
    # Suricata severity 1-4 (lower number = higher severity)
    try:
        n = int(s)
    except (TypeError, ValueError):
        return "medium"
    return {1: "high", 2: "medium", 3: "low", 4: "info"}.get(n, "medium")


def adapt(report: dict, job_id: str) -> dict:
    """Convert a CAPE ``report.json`` dict into sandbox-forensics-v1.

    :param report: parsed CAPE report.json (the dict returned by jsondump)
    :param job_id: our broker-assigned UUID for this job
    """
    info = report.get("info") or {}
    target = report.get("target") or {}
    file_info = target.get("file") or {}
    machine = info.get("machine") or {}

    score = float(report.get("malscore") or 0.0)

    # Common file vs. url handling
    target_block: dict[str, Any]
    if (target.get("category") or "").lower() == "url":
        target_block = {
            "type": TARGET_TYPE_URL,
            "url": target.get("url") or "",
        }
    else:
        target_block = {
            "type": TARGET_TYPE_FILE,
            "sha256": file_info.get("sha256", ""),
            "sha1": file_info.get("sha1", ""),
            "md5": file_info.get("md5", ""),
            "size": int(file_info.get("size") or 0),
            "mime_type": file_info.get("type", ""),
            "filename": file_info.get("name", ""),
        }
        if file_info.get("ssdeep"):
            target_block["ssdeep"] = file_info["ssdeep"]

    out = {
        "schema_version": SCHEMA_VERSION,
        "analysis": {
            "id": job_id,
            "source": "cape",
            "source_version": str(info.get("version") or ""),
            "source_task_id": str(info.get("id") or ""),
            "started_at": _iso(info.get("started")),
            "ended_at": _iso(info.get("ended")),
            "duration_seconds": float(info.get("duration") or 0),
            "environment": {
                "os": str(machine.get("platform") or ""),
                "hostname": str(machine.get("name") or ""),
            },
        },
        "target": target_block,
        "score": score,
        "disposition": _disposition_from_score(score),
        "families": _families(report),
        "detections": _detections(report),
    }
    return out
