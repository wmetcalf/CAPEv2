"""Dependency-light regression tests for direct console isolation.

Load the production functions without importing the web service or libvirt.
Only the database boundary and response renderer are substituted.
"""
import ast
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

SOURCE = Path(__file__).resolve().parents[2] / "web/guac/views.py"


def load_function(name, namespace):
    tree = ast.parse(SOURCE.read_text())
    matches = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert matches, f"Missing production guard: {name}"
    node = matches[0]
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace[name]


class DirectVNCAnalysisGuardTests(unittest.TestCase):
    def guard(self, machine, activity):
        ns = {"_view_vm_machine": Mock(return_value=machine),
              "_active_task_and_guest": activity, "logger": logging.getLogger(__name__)}
        return load_function("_vm_has_active_analysis", ns)

    def test_unlocked_machine_running_task_is_busy(self):
        guard = self.guard(SimpleNamespace(id=7, locked=False), Mock(return_value=(12, False)))
        self.assertTrue(guard("vm"))

    def test_guest_is_busy_even_without_machine_record(self):
        guard = self.guard(None, Mock(return_value=(None, True)))
        self.assertTrue(guard("vm"))

    def test_database_failure_is_busy(self):
        guard = self.guard(None, Mock(side_effect=RuntimeError("DB offline")))
        with self.assertLogs(level="ERROR"):
            self.assertTrue(guard("vm"))

    def test_idle_machine_is_allowed(self):
        guard = self.guard(SimpleNamespace(id=7, locked=False), Mock(return_value=(None, False)))
        self.assertFalse(guard("vm"))

    def test_console_and_start_refuse_busy_vm_before_libvirt(self):
        for name in ("direct_vnc_vm", "direct_vnc_vm_start"):
            with self.subTest(name=name):
                ns = {"is_vnc_console_enabled": lambda: True,
                      "_is_direct_vnc_authorized": lambda request: True,
                      "_vm_has_active_analysis": lambda vm: True,
                      "LIBVIRT_AVAILABLE": False,
                      "_error": lambda request, task_id, message, **kwargs: message}
                view = load_function(name, ns)
                self.assertIn("active analysis", view(SimpleNamespace(), "vm"))
