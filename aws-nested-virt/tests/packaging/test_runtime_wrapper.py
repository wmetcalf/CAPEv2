"""Exercise the packaged legacy command shim without installing a deb or Poetry."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class RuntimeWrapperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = Path(__file__).resolve().parents[2] / "debian/cape-core.postinst"
        script = source.read_text().split("<<'POETRY_WRAP'\n", 1)[1].split("\nPOETRY_WRAP", 1)[0]
        self.wrapper = self.root / "poetry"
        self.wrapper.write_text(script)
        self.wrapper.chmod(0o755)
        (self.root / "bin").mkdir()
        probe = self.root / "bin/probe"
        probe.write_text('#!/bin/sh\nprintf "%s\\n" "$VIRTUAL_ENV" "$PWD" "$@"\n')
        probe.chmod(0o755)
        self.env = dict(os.environ, VIRTUAL_ENV=str(self.root))

    def run_wrapper(self, *args):
        return subprocess.run([str(self.wrapper), *args], env=self.env, capture_output=True, text=True)

    def test_run_uses_shipped_venv_and_preserves_arguments(self):
        result = self.run_wrapper("run", "probe", "one argument", "--flag")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], str(self.root))
        self.assertEqual(result.stdout.splitlines()[-2:], ["one argument", "--flag"])

    def test_directory_option_is_respected(self):
        result = self.run_wrapper("--directory", str(self.root), "run", "probe")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[1], str(self.root))

    def test_legacy_service_cache_config_is_harmless(self):
        result = self.run_wrapper("config", "cache-dir", "/nonexistent/unused")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dependency_mutation_is_rejected(self):
        result = self.run_wrapper("install")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("deb-managed", result.stderr)


if __name__ == "__main__":
    unittest.main()
