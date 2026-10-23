"""Only wrapper tests: disposable text fixtures, never compiled or executed."""

from contextlib import redirect_stderr, redirect_stdout
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "experiment" / "src"
sys.path.insert(0, str(SRC))
from common import ReviewError, json_bytes, read_json, source_commit, validate_config
from extract import extract
from validate import validate

# Do not import this as "select": that name belongs to Python's I/O module.
spec = importlib.util.spec_from_file_location("selection_baseline", SRC / "select.py")
selection = importlib.util.module_from_spec(spec)
spec.loader.exec_module(selection)


class BaselineTests(unittest.TestCase):
    def setUp(self):
        # Disposable outputs use the OS temporary directory (or caller TMPDIR).
        self.temp = tempfile.TemporaryDirectory(prefix="baseline-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "source input"
        self.repo.mkdir()
        self.config = json.loads((ROOT / "experiment/config.json").read_text())
        self.config["intended_source_commit"] = "UNPINNED"
        self.config["source_roots"] = ["src"]
        self.config["domain_anchors"] = ["interface", "route"]
        self.config["grouping"]["context_lines"] = 2
        self.config_path = self.base / "config.json"
        self.config_path.write_bytes(json_bytes(self.config))
        lines = [f"// context {i}" for i in range(1, 35)]
        lines[0] = "// route café"
        lines[2] = 'if (key.find("Ethernet") == 0) {}'
        lines[4] = "// ``` fence inside source"
        lines[5] = 'if (key.compare(0, 3, "Vrf") == 0) {}'
        lines[19] = 'if (key.rfind("Vlan", 0) == 0) {}'
        lines[27:30] = ["auto prefix = key.substr(", "    0,", "    delimiter);"]
        self.put("src/route.cpp", ("\r\n".join(lines) + "\r\n").encode())
        self.put("src/interface.h", b"// interface declarations\n")
        self.put("src/helper.c", b"// route helper\nint count;\n")
        self.put("src/unrelated.cpp", b'auto x = key.find("unused");\n')
        for path in ("src/tests/route.cpp", "src/build/route.cpp", "src/.hidden/route.cpp",
                     "src/docker/route.cpp", "src/cmake-build-debug/route.cpp",
                     "src/test_route.cpp", "src/route_test.cpp", "src/tests.h",
                     "src/route.py", "elsewhere/route.cpp", ".git/route.cpp"):
            self.put(path, b'// route\nauto x = key.find("excluded");\n')
        self.put(".git/HEAD", b"ref: refs/heads/main\n")
        self.put(".git/refs/heads/main", ("a" * 40 + "\n").encode())
        self.put("outside/route.cpp", b"\xff")
        (self.repo / "src/linked.cpp").symlink_to(self.repo / "outside/route.cpp")
        (self.repo / "src/linked-directory").symlink_to(self.repo / "outside", target_is_directory=True)
        self.out = self.base / "output"

    def put(self, path, data):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def prepare(self, out=None, mode="targeted"):
        out = out or self.out
        with mock.patch.object(selection.shutil, "which", return_value=None), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            selection.select(self.repo, self.config_path, out, mode)
            if mode == "targeted":
                extract(self.repo, out / "selected-groups.json")
        return out

    def tree_bytes(self, root):
        return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*")
                if p.is_file() and not p.is_symlink()}

    def test_filter_determinism_windows_and_read_only_source(self):
        before = self.tree_bytes(self.repo)
        self.prepare()
        second = self.prepare(self.base / "second output")
        self.assertEqual(self.tree_bytes(self.out), self.tree_bytes(second))
        self.prepare()  # Identical reruns are idempotent, including extraction.
        self.assertEqual(before, self.tree_bytes(self.repo))
        manifest = read_json(self.out / "manifest.json")
        self.assertEqual([f["path"] for f in manifest["files"]],
                         ["src/helper.c", "src/interface.h", "src/route.cpp"])
        self.assertEqual(manifest["source_commit"], "a" * 40)
        self.assertIn("path:route", manifest["files"][2]["inclusion_reasons"])
        self.assertIn("content:route", manifest["files"][0]["inclusion_reasons"])
        index = read_json(self.out / "selected-groups.json")
        self.assertEqual(index["scanner"], {"name": "python-regex", "version": "1"})
        group = index["groups"][0]
        self.assertEqual(len(group["matches"]), 4)
        scope = read_json(self.out / "groups" / group["id"] / "scope.json")
        self.assertEqual(scope["source"]["ranges"], [
            {"start_line": 1, "end_line": 8}, {"start_line": 18, "end_line": 22},
            {"start_line": 26, "end_line": 32}])
        card = (self.out / "groups" / group["id"] / "source-card.md").read_text()
        self.assertIn("````text", card)
        self.assertIn("No enclosing function is inferred.", card)
        self.assertIn('     3 | if (key.find("Ethernet") == 0)', card)
        self.assertEqual(validate(self.out), (3, 1))

    def test_exhaustive_does_not_need_matching_or_cards(self):
        with mock.patch.object(selection, "find_ast_grep", side_effect=AssertionError("must not run")):
            self.prepare(mode="exhaustive")
        self.assertEqual(validate(self.out, mode="exhaustive"), (3, 0))
        self.assertFalse((self.out / "groups").exists())
        with self.assertRaises(ReviewError):
            validate(self.out, mode="targeted")

    def test_targeted_without_candidates_still_requires_extraction(self):
        self.put("src/route.cpp", b"// route without a candidate\n")
        with mock.patch.object(selection.shutil, "which", return_value=None), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            selection.select(self.repo, self.config_path, self.out)
            with self.assertRaises(ReviewError):
                validate(self.out)
            extract(self.repo, self.out / "selected-groups.json")
        self.assertEqual(validate(self.out), (3, 0))

    def test_source_drift_is_rejected(self):
        self.prepare()
        for action in ("change", "delete", "add", "commit"):
            with self.subTest(action=action):
                path = self.repo / (".git/refs/heads/main" if action == "commit" else "src/route.cpp")
                original = path.read_bytes()
                if action == "change":
                    path.write_bytes(original + b"// changed\n")
                elif action == "delete":
                    path.unlink()
                elif action == "add":
                    self.put("src/new_route.cpp", b"// route\n")
                else:
                    path.write_text("b" * 40 + "\n")
                with self.assertRaises(ReviewError):
                    validate(self.out)
                path.write_bytes(original)
                if action == "add":
                    (self.repo / "src/new_route.cpp").unlink()

    def test_extraction_refuses_changed_source_before_writing_cards(self):
        with mock.patch.object(selection.shutil, "which", return_value=None), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            selection.select(self.repo, self.config_path, self.out)
        self.put("src/route.cpp", b"// route changed\n")
        with self.assertRaises(ReviewError):
            extract(self.repo, self.out / "selected-groups.json")
        self.assertFalse((self.out / "groups").exists())

    def test_missing_corrupt_extra_and_symlink_artifacts_are_rejected(self):
        self.prepare()
        group_id = read_json(self.out / "selected-groups.json")["groups"][0]["id"]
        cases = ["missing-manifest", "invalid-json", "duplicate-json-key", "missing-index",
                 "root-partial", "extra-group", "group-partial", "missing-card", "changed-card",
                 "changed-scope", "symlink-card", "symlink-scope", "symlink-group"]
        for case in cases:
            with self.subTest(case=case):
                out = self.base / case
                shutil.copytree(self.out, out)
                group_dir = out / "groups" / group_id
                if case == "missing-manifest":
                    (out / "manifest.json").unlink()
                elif case == "invalid-json":
                    (out / "selected-groups.json").write_text("{")
                elif case == "duplicate-json-key":
                    (out / "selected-groups.json").write_text('{"groups": [], "groups": []}')
                elif case == "missing-index":
                    (out / "selected-groups.json").unlink()
                elif case == "root-partial":
                    (out / "manifest.json.tmp").write_text("partial")
                elif case == "extra-group":
                    (out / "groups" / "partial").mkdir()
                elif case == "group-partial":
                    (group_dir / "scope.json.tmp").write_text("partial")
                elif case == "missing-card":
                    (group_dir / "source-card.md").unlink()
                elif case == "changed-card":
                    (group_dir / "source-card.md").write_text("wrong source")
                elif case == "changed-scope":
                    scope = read_json(group_dir / "scope.json")
                    scope["source"]["ranges"][0]["end_line"] -= 1
                    (group_dir / "scope.json").write_bytes(json_bytes(scope))
                elif case.startswith("symlink-"):
                    if case == "symlink-group":
                        shutil.rmtree(group_dir)
                        group_dir.symlink_to(self.out / "groups" / group_id, target_is_directory=True)
                    else:
                        name = "scope.json" if case == "symlink-scope" else "source-card.md"
                        (group_dir / name).unlink()
                        (group_dir / name).symlink_to(self.out / "groups" / group_id / name)
                with self.assertRaises(ReviewError):
                    validate(out)

    def test_modified_index_is_rejected(self):
        self.prepare()
        original = read_json(self.out / "selected-groups.json")
        for case in ("traversal", "wrong-id", "range", "unknown-pattern", "omission", "hash", "type"):
            with self.subTest(case=case):
                index = copy.deepcopy(original)
                group = index["groups"][0]
                if case == "traversal":
                    group["path"] = "../outside/route.cpp"
                elif case == "wrong-id":
                    group["id"] = "g-" + "0" * 20
                elif case == "range":
                    group["matches"][0]["end_byte"] = 100000
                elif case == "unknown-pattern":
                    group["matches"][0]["pattern_id"] = "unknown"
                elif case == "omission":
                    index["groups"] = []
                elif case == "hash":
                    index["manifest_sha256"] = "0" * 64
                else:
                    group["matches"][0]["start_line"] = True
                (self.out / "selected-groups.json").write_bytes(json_bytes(index))
                with self.assertRaises(ReviewError):
                    validate(self.out)

    def test_source_and_output_boundaries_and_config(self):
        for bad_out in (self.repo, self.repo / "results", self.base):
            with self.subTest(out=bad_out), self.assertRaises(ReviewError):
                selection.select(self.repo, self.config_path, bad_out)
        for root in ("..", "../outside", "/absolute", ".git", "src/../outside", "src/linked-directory"):
            config = copy.deepcopy(self.config)
            config["source_roots"] = [root]
            self.config_path.write_bytes(json_bytes(config))
            with self.subTest(root=root), self.assertRaises(ReviewError):
                selection.select(self.repo, self.config_path, self.out)
        config = copy.deepcopy(self.config)
        config["restrictions"]["provider_calls"] = True
        with self.assertRaises(ReviewError):
            validate_config(config)

    def test_pin_and_metadata_handling(self):
        self.config["intended_source_commit"] = "b" * 40
        self.config_path.write_bytes(json_bytes(self.config))
        with self.assertRaises(ReviewError):
            selection.select(self.repo, self.config_path, self.out)
        self.assertFalse(self.out.exists())
        (self.repo / ".git/refs/heads/main").unlink()
        self.put(".git/packed-refs", ("# packed refs\n" + "c" * 40 + " refs/heads/main\n").encode())
        self.assertEqual(source_commit(self.repo), "c" * 40)
        self.put(".git/HEAD", ("d" * 40 + "\n").encode())
        self.assertEqual(source_commit(self.repo), "d" * 40)
        shutil.rmtree(self.repo / ".git")
        self.assertIsNone(source_commit(self.repo))
        self.put(".git", b"gitdir: /not-allowed/metadata\n")
        with self.assertRaises(ReviewError):
            source_commit(self.repo)

    def test_ast_adapter_uses_stdin_and_isolated_config(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            self.assertEqual(kwargs["env"], {"PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C"})
            self.assertIn("--stdin", argv)
            config_path = Path(argv[argv.index("--config") + 1])
            self.assertTrue(config_path.is_relative_to(self.out.resolve()))
            self.assertEqual(read_json(config_path), {"ruleDirs": []})
            data = kwargs["input"]
            token = b'key.find("Ethernet")'
            if argv[argv.index("--pattern") + 1] == "$S.find($$$ARGS)" and token in data:
                start = data.index(token)
                hit = {"text": token.decode(), "range": {"byteOffset": {"start": start, "end": start + len(token)}}}
                return SimpleNamespace(returncode=0, stdout=json_bytes([hit, hit]))
            return SimpleNamespace(returncode=1, stdout=b"[]")

        with mock.patch.object(selection, "find_ast_grep", return_value=("/fake/ast-grep", "0.test")), \
                mock.patch.object(selection.subprocess, "run", side_effect=fake_run), \
                redirect_stdout(io.StringIO()):
            selection.select(self.repo, self.config_path, self.out)
            extract(self.repo, self.out / "selected-groups.json")
        self.assertTrue(calls)
        self.assertEqual(validate(self.out), (3, 1))
        index = read_json(self.out / "selected-groups.json")
        self.assertEqual(index["scanner"], {"name": "ast-grep", "version": "0.test"})
        self.assertEqual(len(index["groups"][0]["matches"]), 1)

    def test_installed_ast_failure_never_silently_falls_back(self):
        for code, data in ((2, b""), (0, b"garbage"), (1, b"garbage")):
            with self.subTest(code=code, data=data), \
                    mock.patch.object(selection, "find_ast_grep", return_value=("/fake/ast-grep", "0.test")), \
                    mock.patch.object(selection.subprocess, "run", return_value=SimpleNamespace(returncode=code, stdout=data)), \
                    self.assertRaises(ReviewError):
                selection.select(self.repo, self.config_path, self.out)
            self.assertFalse((self.out / "manifest.json").exists())

    def test_tool_discovery_resolves_relative_paths_and_rejects_source_tools(self):
        binary = self.base / "bin" / "ast-grep"
        result = SimpleNamespace(returncode=0, stdout="ast-grep 0.test\n")
        with mock.patch.object(selection.shutil, "which", return_value=os.path.relpath(binary, Path.cwd())), \
                mock.patch.object(selection.subprocess, "run", return_value=result) as run:
            self.assertEqual(selection.find_ast_grep(self.repo), (str(binary.resolve()), "0.test"))
            self.assertEqual(run.call_args.args[0][0], str(binary.resolve()))
        with mock.patch.object(selection.shutil, "which", return_value=str(self.repo / "ast-grep")), \
                mock.patch.object(selection.subprocess, "run") as run, self.assertRaises(ReviewError):
            selection.find_ast_grep(self.repo)
        run.assert_not_called()
        with mock.patch.object(selection.shutil, "which", side_effect=[None, "/bin/sg"]), \
                mock.patch.object(selection.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="")):
            self.assertEqual(selection.find_ast_grep(self.repo), (None, None))

    def test_runner_and_validator_cli(self):
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        (bin_dir / "python3").symlink_to(sys.executable)
        env = {"PATH": str(bin_dir) + os.pathsep + os.defpath, "PYTHONDONTWRITEBYTECODE": "1"}
        for mode in ("exhaustive", "targeted"):
            out = self.base / (mode + " cli output")
            result = subprocess.run([
                "/bin/bash", str(ROOT / "experiment/run-experiment.sh"), "prepare", mode,
                "--repo", str(self.repo), "--out", str(out), "--config", str(self.config_path)],
                cwd=self.base, env=env, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("PASS validation", result.stdout)
        (out / "partial.tmp").write_text("partial")
        result = subprocess.run([sys.executable, "-B", str(SRC / "validate.py"), "--out", str(out)],
                                env=env, text=True, capture_output=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAIL validation", result.stderr)
        for args in ([], ["discover", "targeted"], ["prepare", "targeted", "--out"],
                     ["prepare", "targeted", "--unknown"]):
            result = subprocess.run(["/bin/bash", str(ROOT / "experiment/run-experiment.sh"), *args],
                                    env=env, capture_output=True, check=False)
            self.assertNotEqual(result.returncode, 0)



if __name__ == "__main__":
    unittest.main()
