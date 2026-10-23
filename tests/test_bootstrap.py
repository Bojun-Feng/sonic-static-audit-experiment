"""Offline bootstrap tests: only a fake limactl, never a VM or provider CLI."""

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiment/src"))
import bootstrap_lima as bootstrap
from common import AuditError, validate_config


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bootstrap-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.paths = tuple(self.base / name for name in ("source input", "references", "results"))
        self.bin = self.base / "bin"
        self.scratch = self.base / "scratch"
        self.lima_state = self.base / "lima-state"
        for path in (*self.paths, self.bin, self.scratch, self.lima_state):
            path.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        # dirname is needed by the Bash entry point even in the missing-Lima test.
        (self.bin / "dirname").symlink_to(shutil.which("dirname"))
        self.config = json.loads((ROOT / "experiment/config.json").read_text())
        self.versions = self.config["guest_cli_versions"]
        self.scenario = self.base / "scenario.json"
        self.scenario.write_text("{}")
        self.log = self.base / "calls.jsonl"
        self.fake = self.bin / "limactl"
        self.fake.write_text(f"#!{sys.executable} -B\n" + "import json, os, pathlib, signal, stat, sys\n"
                            + f"base = pathlib.Path({str(self.base)!r})\n" + r'''
args = sys.argv[1:]
scenario = json.loads((base / "scenario.json").read_text())
with (base / "calls.jsonl").open("a") as log:
    log.write(json.dumps({"args": args, "env_keys": sorted(os.environ)}) + "\n")
command = args[0]
if scenario.get("fail") == command and command not in ("create", "shell"):
    sys.exit(23)
if command == "--version":
    print("limactl version " + scenario.get("version", "2.2.1"))
elif command == "list":
    print(scenario.get("existing", "unrelated-vm"))
elif command == "create":
    config = pathlib.Path(args[-1])
    assert stat.S_IMODE(config.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    (base / "config-path.txt").write_text(str(config))
    (base / "captured.yaml").write_text(config.read_text())
    (base / "lima-state" / "sonic-static-audit").mkdir()
    if scenario.get("fail") == command:
        sys.exit(23)
elif command == "start":
    pass
elif command == "shell":
    (base / "guest.sh").write_text(sys.stdin.read())
    if scenario.get("signal"):
        os.kill(os.getppid(), scenario["signal"])
        sys.exit(23)
    result = base / "results" / "setup-receipt.txt"
    if scenario.get("fail") == command:
        result.write_text("partial receipt")
        sys.exit(23)
    if not scenario.get("missing_receipt"):
        result.write_text((base / "expected-receipt.txt").read_text())
    if scenario.get("bad_receipt"):
        result.write_text("status=wrong\n")
    if scenario.get("extra_result"):
        (result.parent / "unexpected.log").write_text("extra")
elif command == "delete":
    (base / "lima-state" / "sonic-static-audit").rmdir()
else:
    raise AssertionError(args)
''')
        self.fake.chmod(0o755)
        receipt = bootstrap.expected_receipt(self.versions)
        (self.base / "expected-receipt.txt").write_text(
            "".join(f"{key}={value}\n" for key, value in receipt.items()))
        self.env = {"PATH": str(self.bin) + os.pathsep + os.defpath,
                    "TMPDIR": str(self.scratch), "LIMA_HOME": str(self.lima_state),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    # Synthetic canaries, never read actual credentials.
                    "OPENAI_API_KEY": "test-canary", "ANTHROPIC_API_KEY": "test-canary",
                    "SSH_AUTH_SOCK": "/test-canary", "HTTPS_PROXY": "test-canary"}

    def invoke(self, *args):
        return subprocess.run(["/bin/bash", str(ROOT / "experiment/bootstrap-lima.sh"), *args],
                              cwd=self.base, env=self.env, text=True, capture_output=True,
                              check=False, timeout=15)

    def path_args(self, paths=None):
        return [part for label, path in zip(("source", "reference", "results"), paths or self.paths)
                for part in (f"--{label}", str(path))]

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def scenario_is(self, **kwargs):
        self.scenario.write_text(json.dumps(kwargs))

    def assert_clean_scratch(self):
        self.assertEqual(list(self.scratch.iterdir()), [])
        if (self.base / "config-path.txt").exists():
            self.assertFalse(Path((self.base / "config-path.txt").read_text()).parent.exists())

    def test_dry_run_is_deterministic_without_lima_or_writes(self):
        first = self.invoke("--dry-run")
        second = self.invoke("--dry-run")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(self.calls(), [])
        self.assert_clean_scratch()
        self.assertEqual(list(self.paths[2].iterdir()), [])
        self.assertIn("codex login --device-auth", first.stdout)
        self.assertIn("claude auth login", first.stdout)
        self.assertIn("limactl delete --force sonic-static-audit", first.stdout)
        self.assertIn("A retry starts from scratch", first.stdout)
        self.fake.unlink()
        self.assertEqual(self.invoke("--dry-run").stdout, first.stdout)

    def test_create_uses_exact_dry_run_config_and_script(self):
        plan = self.invoke("--dry-run", *self.path_args())
        self.assertEqual(plan.returncode, 0, plan.stderr)
        result = self.invoke("--create", *self.path_args())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call["args"][0] for call in self.calls()],
                         ["--version", "list", "create", "start", "shell"])
        rendered = plan.stdout.split("--- BEGIN LIMA YAML ---\n")[1].split("--- END LIMA YAML ---")[0]
        self.assertEqual(rendered, (self.base / "captured.yaml").read_text())
        guest = plan.stdout.split("--- BEGIN GUEST SCRIPT ---\n")[1].split("--- END GUEST SCRIPT ---")[0]
        self.assertEqual(guest, (self.base / "guest.sh").read_text())
        config_path = (self.base / "config-path.txt").read_text()
        for actual, expected in zip(self.calls()[2:], bootstrap.commands(config_path, self.versions)):
            self.assertEqual(actual["args"], expected[1:])
            self.assertIn(shlex.join(["limactl", *actual["args"]]).replace(config_path, "<scratch>/audit.yaml")
                          if actual["args"][0] != "create" else "limactl create --tty=false", plan.stdout)
        for call in self.calls():
            self.assertTrue({"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "SSH_AUTH_SOCK", "HTTPS_PROXY"}
                            .isdisjoint(call["env_keys"]))
        self.assertEqual(rendered.count("  - location:"), 4)  # Image plus exactly three mounts.
        self.assertEqual(rendered.count("writable: false"), 2)
        self.assertEqual(rendered.count("writable: true"), 1)
        for setting in ("forwardAgent: false", "loadDotSSHPubKeys: false", "propagateProxyEnv: false",
                        "system: false", "user: false", "ignore: true"):
            self.assertIn(setting, rendered)
        bootstrap.verify_receipt(self.paths[2], self.versions)
        self.assertIn("codex login --device-auth", result.stdout)
        self.assert_clean_scratch()

    def test_existing_vm_is_never_started_or_deleted(self):
        self.scenario_is(existing=bootstrap.VM_NAME)
        result = self.invoke("--create", *self.path_args())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        self.assertEqual([c["args"][0] for c in self.calls()], ["--version", "list"])
        self.assert_clean_scratch()

    def test_partial_existing_state_and_global_overrides_are_refused(self):
        vm = self.lima_state / bootstrap.VM_NAME
        vm.mkdir()
        result = self.invoke("--create", *self.path_args())
        self.assertIn("already exists", result.stderr)
        self.assertTrue(vm.is_dir())
        vm.rmdir()
        directory = self.lima_state / "_config"
        directory.mkdir()
        for name in ("default.yaml", "override.yaml"):
            path = directory / name
            path.write_text("must never be loaded")
            result = self.invoke("--create", *self.path_args())
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing inherited Lima configuration", result.stderr)
            path.unlink()
        self.assertFalse(any(c["args"][0] in ("create", "start", "delete") for c in self.calls()))

    def test_bad_version_missing_binary_and_inventory_failure_are_nonmutating(self):
        self.scenario_is(version="0.0.0")
        result = self.invoke("--create", *self.path_args())
        self.assertIn("version mismatch", result.stderr)
        self.assertEqual([c["args"][0] for c in self.calls()], ["--version"])
        self.scenario_is(fail="list")
        result = self.invoke("--create", *self.path_args())
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(c["args"][0] == "create" for c in self.calls()))
        self.fake.unlink()
        self.env["PATH"] = str(self.bin)
        result = self.invoke("--create", *self.path_args())
        self.assertIn("unavailable", result.stderr)
        self.assertNotEqual(result.returncode, 0)
        self.assert_clean_scratch()

    def test_failures_destroy_partial_vm_and_remove_receipt_and_scratch(self):
        cases = [{"fail": stage} for stage in ("create", "start", "shell")]
        cases += [{"missing_receipt": True}, {"bad_receipt": True}, {"extra_result": True}]
        for scenario in cases:
            with self.subTest(scenario=scenario):
                self.scenario_is(**scenario)
                result = self.invoke("--create", *self.path_args())
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls()[-1]["args"], ["delete", "--force", bootstrap.VM_NAME])
                self.assertIn("A retry starts from scratch", result.stderr)
                self.assertNotIn("Manual authentication", result.stdout)
                self.assertFalse((self.lima_state / bootstrap.VM_NAME).exists())
                self.assertFalse((self.paths[2] / "setup-receipt.txt").exists())
                self.assert_clean_scratch()
                # Unexpected files are reported, never silently deleted.
                (self.paths[2] / "unexpected.log").unlink(missing_ok=True)

    def test_interruptions_destroy_vm(self):
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig):
                self.scenario_is(signal=sig)
                result = self.invoke("--create", *self.path_args())
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls()[-1]["args"], ["delete", "--force", bootstrap.VM_NAME])
                self.assertFalse((self.lima_state / bootstrap.VM_NAME).exists())
                self.assert_clean_scratch()

    def test_cleanup_failure_is_explicit_and_never_claims_success(self):
        self.scenario_is(bad_receipt=True, fail="delete")
        result = self.invoke("--create", *self.path_args())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("VM deletion failed", result.stderr)
        self.assertIn("do not reuse the partial VM", result.stderr)
        self.assertNotIn("Created sonic-static-audit", result.stdout)
        self.assertFalse((self.paths[2] / "setup-receipt.txt").exists())
        self.assert_clean_scratch()

    def test_lima_state_and_scratch_cannot_write_into_mounts(self):
        for key in ("LIMA_HOME", "TMPDIR"):
            for path in self.paths:
                with self.subTest(variable=key, mount=path):
                    original = self.env[key]
                    self.env[key] = str(path)
                    result = self.invoke("--create", *self.path_args())
                    self.env[key] = original
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("outside", result.stderr)
                    self.assertEqual(list(path.iterdir()), [])
        self.assertFalse(any(c["args"][0] in ("create", "start", "delete") for c in self.calls()))
        self.assert_clean_scratch()

    def test_paths_and_argument_errors_fail_before_lima(self):
        invalid = [[], ["--create"], ["--create", "--dry-run"], ["--unknown"],
                   ["--dry-run", "--source", str(self.paths[0])]]
        for args in invalid:
            with self.subTest(args=args):
                self.assertNotEqual(self.invoke(*args).returncode, 0)
        source, reference, results = self.paths
        missing = self.base / "missing"
        file = self.base / "file"
        file.touch()
        link = self.base / "link"
        link.symlink_to(source, target_is_directory=True)
        hidden = self.base / ".ssh"
        hidden.mkdir()
        template = self.base / "{{.Home}}"
        template.mkdir()
        (template / "child").mkdir()
        template_alias = self.base / "template-alias"
        template_alias.symlink_to(template, target_is_directory=True)
        for bad_source in ("relative", missing, file, link, hidden, template, Path("/"),
                           Path.home(), ROOT.parent, reference, results, template_alias / "child"):
            with self.subTest(source=bad_source):
                result = self.invoke("--create", *self.path_args((bad_source, reference, results)))
                self.assertNotEqual(result.returncode, 0)
        (results / "old.txt").write_text("keep me")
        result = self.invoke("--create", *self.path_args())
        self.assertIn("must be empty", result.stderr)
        self.assertEqual((results / "old.txt").read_text(), "keep me")
        self.assertEqual(self.calls(), [])
        self.assert_clean_scratch()

    def test_paths_are_rendered_as_literals(self):
        unusual = self.base / 'source "quote" $dollar `backtick` café: [x]'
        unusual.mkdir()
        result = self.invoke("--dry-run", *self.path_args((unusual, self.paths[1], self.paths[2])))
        self.assertEqual(result.returncode, 0, result.stderr)
        locations = [json.loads(line.split("location: ", 1)[1]) for line in result.stdout.splitlines()
                     if line.startswith("  - location: ")]
        self.assertEqual(locations[1:], [str(unusual), str(self.paths[1]), str(self.paths[2])])
        self.assertEqual(self.calls(), [])

    def test_pins_are_validated_and_feed_guest_commands(self):
        for value in ("latest", "^1.2.3", "~1.2.3", "1.2", "1.2.3; touch bad", "01.2.3", 123):
            config = copy.deepcopy(self.config)
            config["guest_cli_versions"]["codex"] = value
            with self.subTest(value=value), self.assertRaises(AuditError):
                validate_config(config)
        config = copy.deepcopy(self.config)
        del config["guest_cli_versions"]
        validate_config(config)  # Existing static preparation profiles remain valid.
        versions = {"codex": "1.2.3", "claude_code": "4.5.6"}
        self.assertEqual(bootstrap.commands("unused.yaml", versions)[2][-6:-4], ["1.2.3", "4.5.6"])

    def test_guest_has_only_pinned_installs_and_version_probes(self):
        guest = (ROOT / "experiment/guest-setup.sh").read_text()
        self.assertIn('"@openai/codex@$codex_version" "@anthropic-ai/claude-code@$claude_version"', guest)
        self.assertIn("--ignore-scripts --no-audit --no-fund", guest)
        self.assertIn("Node archive SHA-256 mismatch", guest)
        self.assertIn('codex_observed=$(codex --version)', guest)
        self.assertIn('claude_observed=$(claude --version)', guest)
        self.assertIn('[[ $codex_observed == "codex-cli $codex_version" ]]', guest)
        self.assertIn('[[ $claude_observed == "$claude_version (Claude Code)" ]]', guest)
        for forbidden in (" login", "docker ", "make ", "npm test", "claude -p", "codex exec"):
            self.assertNotIn(forbidden, guest)

    def test_guest_checksum_and_receipt_code_offline(self):
        guest = (ROOT / "experiment/guest-setup.sh").read_text()
        blocks = [block.split("\nPY\n", 1)[0] for block in guest.split("<<'PY'\n")[1:]]
        self.assertEqual(len(blocks), 2)
        archive = self.base / "node.tar.gz"
        payload = b"synthetic runtime archive; never extracted or executed"
        checksum = hashlib.sha256(payload).hexdigest()
        for expected in (checksum, "0" * 64):
            with mock.patch.object(sys, "argv", ["-", bootstrap.NODE_VERSION, expected, str(archive)]), \
                    mock.patch.object(urllib.request, "urlopen", return_value=io.BytesIO(payload)) as download:
                if expected == checksum:
                    exec(compile(blocks[0], "guest-download", "exec"), {})
                    self.assertEqual(archive.read_bytes(), payload)
                else:
                    with self.assertRaisesRegex(SystemExit, "SHA-256 mismatch"):
                        exec(compile(blocks[0], "guest-download", "exec"), {})
                self.assertEqual(download.call_args.args[0],
                                 f"https://nodejs.org/download/release/v{bootstrap.NODE_VERSION}/"
                                 f"node-v{bootstrap.NODE_VERSION}-linux-arm64.tar.gz")
            archive.unlink()
        args = ["-", bootstrap.LIMA_VERSION, bootstrap.IMAGE_SHA256,
                bootstrap.NODE_VERSION, "v" + bootstrap.NODE_VERSION,
                self.versions["codex"], "codex-cli " + self.versions["codex"],
                self.versions["claude_code"], self.versions["claude_code"] + " (Claude Code)"]

        def guest_path(path):
            self.assertEqual(path, "/mnt/results")
            return self.paths[2]

        with mock.patch.object(sys, "argv", args), \
                mock.patch.dict(sys.modules, {"pathlib": SimpleNamespace(Path=guest_path)}):
            exec(compile(blocks[1], "guest-receipt", "exec"), {})
            bootstrap.verify_receipt(self.paths[2], self.versions)
            before = (self.paths[2] / "setup-receipt.txt").read_bytes()
            with self.assertRaisesRegex(SystemExit, "still be empty"):
                exec(compile(blocks[1], "guest-receipt", "exec"), {})
            self.assertEqual((self.paths[2] / "setup-receipt.txt").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
