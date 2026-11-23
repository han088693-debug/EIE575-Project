"""Installer checks use temporary fixtures only; never modify real MCP config."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest

SPEC = importlib.util.spec_from_file_location("eie575_installer", Path(__file__).resolve().parents[1] / "install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eie575-install-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "package"
        self.source.mkdir()
        (self.source / "server.py").write_text("# sample server\n", encoding="utf-8")
        (self.source / "host.py").write_text("# sample host\n", encoding="utf-8")
        (self.source / "start_host.ps1").write_text("# sample launcher\n", encoding="utf-8")
        self.settings = {"source_project": "original/project", "tools_root": "original/tools",
                         "state_root": "old/state", "max_run_wall_s": 1800}
        (self.source / "settings.json").write_text(json.dumps(self.settings), encoding="utf-8")
        self.destination = self.root / "installed"
        self.state = self.root / "state"
        self.config = self.root / "project" / ".codex" / "config.toml"
        self.config.parent.mkdir(parents=True)

    def run_install(self, **overrides):
        arguments = dict(source_dir=self.source, install_dir=self.destination, state_root=self.state,
                         config_file=self.config, python_executable=Path(sys.executable))
        arguments.update(overrides)
        return installer.install_package(**arguments)

    def test_preserves_other_config_and_creates_exact_backup(self):
        original = '# preserve this comment\nmodel = "demo"\n[mcp_servers.other]\ncommand = "keep"\n[mcp_servers.other.env]\nPRIVATE_VALUE = "do not print"\n[projects."a.b"]\ntrust_level = "trusted"\n'
        self.config.write_text(original, encoding="utf-8")
        original_bytes = self.config.read_bytes()
        result = self.run_install()
        actual = tomllib.loads(self.config.read_text(encoding="utf-8"))
        actual["mcp_servers"].pop(installer.SERVER_NAME)
        self.assertEqual(actual, tomllib.loads(original))
        self.assertEqual(Path(result["config_backup"]).read_bytes(), original_bytes)
        self.assertIn("# preserve this comment", self.config.read_text(encoding="utf-8"))
        changed = json.loads((self.destination / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(changed, self.settings | {"state_root": str(self.state.resolve())})
        self.assertTrue((self.destination / "host.py").is_file())
        self.assertTrue((self.destination / "start_host.ps1").is_file())

    def test_idempotent_reinstall_and_update_owned_package(self):
        self.run_install()
        original = self.config.read_bytes()
        (self.source / "server.py").write_text("# updated server\n", encoding="utf-8")
        again = self.run_install()
        self.assertIsNone(again["config_backup"])
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual((self.destination / "server.py").read_text(encoding="utf-8"), "# updated server\n")

    def test_refuses_other_service_before_any_destination_write(self):
        original = '[mcp_servers.eie575-simulation]\ncommand = "other-python"\nargs = ["other-server.py"]\n'
        self.config.write_text(original, encoding="utf-8")
        with self.assertRaises(installer.InstallError):
            self.run_install()
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.state.exists())
        self.assertEqual(self.config.read_text(encoding="utf-8"), original)

    def test_refuses_unowned_nonempty_directory(self):
        self.destination.mkdir()
        original = self.destination / "unrelated.txt"
        original.write_text("precious", encoding="utf-8")
        with self.assertRaises(installer.InstallError):
            self.run_install()
        self.assertEqual(original.read_text(encoding="utf-8"), "precious")
        self.assertFalse(self.config.exists())

    def test_refuses_spoofed_or_misplaced_ownership_marker(self):
        self.destination.mkdir()
        (self.destination / installer.MARKER_NAME).write_text(json.dumps({
            "installer_id": installer.INSTALLER_ID, "install_dir": str(self.root / "elsewhere")}), encoding="utf-8")
        with self.assertRaises(installer.InstallError):
            self.run_install()
        self.assertFalse(self.config.exists())

    def test_filters_runtime_data_but_keeps_docs_and_tests(self):
        for folder in ("docs", "tests", "evidence", "results", "tests/__pycache__"):
            (self.source / folder).mkdir(parents=True, exist_ok=True)
            (self.source / folder / "sample.txt").write_text(folder, encoding="utf-8")
        self.run_install()
        self.assertTrue((self.destination / "docs" / "sample.txt").is_file())
        self.assertTrue((self.destination / "tests" / "sample.txt").is_file())
        self.assertFalse((self.destination / "evidence").exists())
        self.assertFalse((self.destination / "results").exists())
        self.assertFalse((self.destination / "tests" / "__pycache__").exists())

    def test_in_place_package_install_is_allowed(self):
        self.run_install(install_dir=self.source)
        self.assertTrue((self.source / installer.MARKER_NAME).is_file())
        self.run_install(install_dir=self.source)
        self.assertEqual(tomllib.loads(self.config.read_text(encoding="utf-8"))["mcp_servers"][installer.SERVER_NAME]["args"][-1], str(self.source / "server.py"))

    def test_rejects_invalid_config_without_changes(self):
        original = b"this is invalid toml"
        self.config.write_bytes(original)
        with self.assertRaises(installer.InstallError):
            self.run_install()
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(self.destination.exists())

    def test_quoted_service_header_and_subtable_are_updated(self):
        self.run_install()
        original = self.config.read_text(encoding="utf-8").replace(
            "[mcp_servers.eie575-simulation]", '[mcp_servers."eie575-simulation"]')
        original += '\n[mcp_servers."eie575-simulation".env]\nLEGACY = "value"\n[unrelated]\nnumber = 42\n'
        self.config.write_text(original, encoding="utf-8")
        self.run_install()
        actual = tomllib.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(actual["unrelated"], {"number": 42})
        self.assertNotIn("env", actual["mcp_servers"][installer.SERVER_NAME])

    def test_table_like_text_in_multiline_string_is_preserved(self):
        original = 'description = """hello\n[mcp_servers.eie575-simulation]\nnot a table\n"""\n'
        self.config.write_text(original, encoding="utf-8")
        self.run_install()
        self.run_install()
        actual = tomllib.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(actual["description"], tomllib.loads(original)["description"])


if __name__ == "__main__":
    unittest.main()
