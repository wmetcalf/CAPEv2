"""Adapter registry + ``normalize`` dispatcher.

Each adapter is a single function::

    def adapt(native: dict, job_id: str) -> dict

that takes a native sandbox report and returns a sandbox-forensics-v1
document (a plain dict that conforms to the v1 JSON Schema).

Register adapters in :data:`ADAPTERS` keyed by source name. The
:func:`normalize` dispatcher looks up the source and delegates.
"""

from __future__ import annotations

from typing import Callable, Dict

from . import cape

# Registry: source name -> adapt(native, job_id) -> dict
ADAPTERS: Dict[str, Callable[[dict, str], dict]] = {
    "cape": cape.adapt,
}


class UnknownSourceError(ValueError):
    """Raised when ``normalize`` is called with a source we don't have
    an adapter for."""


def normalize(native: dict, source: str, job_id: str) -> dict:
    """Convert a native sandbox report into sandbox-forensics-v1.

    :param native:  parsed native sandbox JSON (e.g. CAPE report.json)
    :param source:  vendor identifier (``cape``, ``crowdstrike``, ...)
    :param job_id:  our broker-assigned job UUID
    :returns:       sandbox-forensics-v1 dict
    :raises UnknownSourceError: if no adapter is registered for ``source``
    """
    adapt = ADAPTERS.get(source)
    if adapt is None:
        raise UnknownSourceError(
            f"no adapter registered for source={source!r}; "
            f"known: {sorted(ADAPTERS)}"
        )
    return adapt(native, job_id)


__all__ = ["ADAPTERS", "UnknownSourceError", "normalize"]
