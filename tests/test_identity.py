"""Snapshot provenance tests; source fixtures are never executed."""

import importlib.util
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "experiment/src"
sys.path.insert(0, str(SRC))
from common import AuditError, json_bytes, read_json


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "snapshot"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src/route.cpp").write_text('// route\nauto p = key.find("Vrf");\n')
        self.identity = self.base / "source-identity.json"
        self.config = read_json(ROOT / "experiment/config.json")
        self.config["source_roots"] = ["src"]
        self.config_path = self.base / "config.json"
        self.config_path.write_bytes(json_bytes(self.config))

    def test_binding_is_immutable_and_covers_entire_snapshot_without_git(self):
        from source_identity import bind, verify
        pin = self.config["intended_source_commit"]
        bind(self.repo, pin, self.identity)
        original = self.identity.read_bytes()
        self.assertEqual(verify(self.repo, self.identity, pin)["source_commit"], pin)
        self.assertEqual(self.identity.stat().st_mode & 0o222, 0)
        with self.assertRaises(AuditError):
            bind(self.repo, pin, self.identity)
        self.assertEqual(self.identity.read_bytes(), original)
        with self.assertRaises(AuditError):
            verify(self.repo, self.identity, "a" * 40)
        (self.repo / "README.md").write_text("new source context")
        with self.assertRaises(AuditError):
            verify(self.repo, self.identity, pin)

    def test_identity_rejects_unsafe_inputs_and_writable_attestations(self):
        from source_identity import bind, verify
        pin = self.config["intended_source_commit"]
        for value in ("UNPINNED", "abc", "g" * 40):
            with self.subTest(value=value), self.assertRaises(AuditError):
                bind(self.repo, value, self.identity)
        with self.assertRaises(AuditError):
            bind(self.repo, pin, self.repo / "identity.json")
        bind(self.repo, pin, self.identity)
        self.identity.chmod(0o644)
        with self.assertRaises(AuditError):
            verify(self.repo, self.identity, pin)

    def test_preparation_and_validation_bind_identity_bytes(self):
        from source_identity import bind
        from extract import extract
        from validate import validate
        spec = importlib.util.spec_from_file_location("audit_selection_identity", SRC / "select.py")
        selection = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(selection)
        bind(self.repo, self.config["intended_source_commit"], self.identity)
        for mode in ("exhaustive", "targeted"):
            out = self.base / mode
            with mock.patch.object(selection, "find_ast_grep", return_value=(None, None)):
                selection.select(self.repo, self.config_path, out, mode, identity=self.identity)
            if mode == "targeted":
                extract(self.repo, out / "selected-groups.json")
            self.assertEqual(validate(out)[0], 1)
            manifest = read_json(out / "manifest.json")
            self.assertEqual(manifest["source_commit"], self.config["intended_source_commit"])
            self.assertIn("sha256", manifest["source_identity"])
        self.identity.chmod(0o644)
        self.identity.write_bytes(self.identity.read_bytes() + b"\n")
        self.identity.chmod(0o444)
        with self.assertRaises(AuditError):
            validate(out)


if __name__ == "__main__":
    unittest.main()
