"""Offline bootstrap tests: only a fake limactl, never a VM or provider CLI."""

import ast
import contextlib
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
from common import ReviewError, validate_config


class BootstrapTests(unittest.TestCase):
    HOST = ("Linux", "u", "test-release", "test-version", "x86_64")
    CREATE_FLAGS = ()

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
        # Test the real Bash entry point with kernel identity mocked inside its
        # Python child. No platform override exists in the production CLI.
        launcher = self.bin / "python3"
        launcher.write_text(f"#!{sys.executable} -B\n" + "import json, os, runpy, sys\n"
                            + f"with open({str(self.base / 'scenario.json')!r}) as stream:\n"
                            + "    host = os.uname_result(json.load(stream)['host'])\n"
                            + "os.uname = lambda: host\n"
                            + "assert sys.argv[1] == '-B'\n"
                            + "sys.argv = sys.argv[2:]\n"
                            + "sys.path.insert(0, os.path.dirname(sys.argv[0]))\n"
                            + "runpy.run_path(sys.argv[0], run_name='__main__')\n")
        launcher.chmod(0o755)
        for name in ("qemu-system-x86_64", "qemu-img"):
            binary = self.bin / name
            binary.write_text("#!/bin/sh\nexit 99 # Must never be executed by bootstrap tests.\n")
            binary.chmod(0o755)
        # dirname is needed by the Bash entry point even in the missing-Lima test.
        (self.bin / "dirname").symlink_to(shutil.which("dirname"))
        self.config = json.loads((ROOT / "experiment/config.json").read_text())
        self.versions = self.config["guest_cli_versions"]
        with mock.patch.object(bootstrap.os, "uname", return_value=os.uname_result(self.HOST)):
            self.profile = bootstrap.host_profile()
        self.scenario = self.base / "scenario.json"
        self.scenario_is()
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
    print("limactl version " + scenario["version"])
elif command == "list":
    print(scenario.get("existing", "unrelated-vm"))
elif command == "create":
    config = pathlib.Path(args[-1])
    assert stat.S_IMODE(config.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    (base / "config-path.txt").write_text(str(config))
    (base / "captured.yaml").write_text(config.read_text())
    (base / "lima-state" / args[2].split('=', 1)[1]).mkdir()
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
    (base / "lima-state" / args[-1]).rmdir()
else:
    raise AssertionError(args)
''')
        self.fake.chmod(0o755)
        receipt = bootstrap.expected_receipt(self.versions, self.profile)
        (self.base / "expected-receipt.txt").write_text(
            "".join(f"{key}={value}\n" for key, value in receipt.items()))
        self.env = {"PATH": str(self.bin) + os.pathsep + os.defpath,
                    "TMPDIR": str(self.scratch), "LIMA_HOME": str(self.lima_state),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    # Synthetic placeholder values; real tokens are never read.
                    "OPENAI_API_KEY": "test-placeholder", "ANTHROPIC_API_KEY": "test-placeholder",
                    "SSH_AUTH_SOCK": "/test-placeholder", "HTTPS_PROXY": "test-placeholder"}

    def invoke(self, *args):
        return subprocess.run(["/bin/bash", str(ROOT / "experiment/bootstrap-lima.sh"), *args],
                              cwd=self.base, env=self.env, text=True, capture_output=True,
                              check=False, timeout=15)

    def create(self, *args):
        return self.invoke("--create", *self.CREATE_FLAGS, *args)

    def path_args(self, paths=None):
        return [part for label, path in zip(("source", "reference", "results"), paths or self.paths)
                for part in (f"--{label}", str(path))]

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def scenario_is(self, **kwargs):
        self.scenario.write_text(json.dumps({"host": self.HOST, "version": self.profile["lima"], **kwargs}))

    def assert_clean_scratch(self):
        self.assertEqual(list(self.scratch.iterdir()), [])
        if (self.base / "config-path.txt").exists():
            self.assertFalse(Path((self.base / "config-path.txt").read_text()).parent.exists())

    def test_dry_run_is_deterministic_without_lima_or_writes(self):
        before = {str(p.relative_to(self.base)): p.read_bytes() if p.is_file() else None
                  for p in self.base.rglob("*")}
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
        self.assertEqual(before, {str(p.relative_to(self.base)): p.read_bytes() if p.is_file() else None
                                  for p in self.base.rglob("*")})
        self.fake.unlink()
        for name in ("qemu-system-x86_64", "qemu-img"):
            (self.bin / name).unlink()
        self.assertEqual(self.invoke("--dry-run").stdout, first.stdout)

    def test_dry_run_selects_exact_native_profile(self):
        plan = self.invoke("--dry-run")
        self.assertEqual(plan.returncode, 0, plan.stderr)
        yaml = plan.stdout.split("--- BEGIN LIMA YAML ---\n")[1].split("--- END LIMA YAML ---")[0]
        if self.HOST[0] == "Linux":
            expected = ('minimumLimaVersion: "2.0.3"', "vmType: qemu", "arch: x86_64", "cpus: 8",
                        "memory: 32GiB", "disk: 200GiB", "mountType: 9p",
                        "ubuntu-24.04-server-cloudimg-amd64.img",
                        "04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94")
            self.assertIn("qemu-system-x86_64 and qemu-img", plan.stdout)
            node_hash = "9d942932535988091034dc94cc5f42b6dc8784d6366df3a36c4c9ccb3996f0c2"
        else:
            expected = ('minimumLimaVersion: "2.2.1"', "vmType: vz", "arch: aarch64", "cpus: 4",
                        "memory: 8GiB", "disk: 40GiB", "mountType: virtiofs",
                        "ubuntu-24.04-server-cloudimg-arm64.img",
                        "50e24e1fb79e89116e939d90aa7738f7af48c442fa1bc2510131e0b39c50348e")
            self.assertNotIn("qemu-system-x86_64 and qemu-img", plan.stdout)
            node_hash = "8cf30ff7250f9463b53c18f89c6c606dfda70378215b2c905d0a9a8b08bd45e0"
        for literal in expected:
            self.assertIn(literal, yaml)
        self.assertIn(node_hash, plan.stdout)
        self.assertIn(f"Host profile: {self.HOST[0]}/{self.HOST[4]} (uname -s/-m).", plan.stdout)
        self.assertIn("https://cloud-images.ubuntu.com/releases/noble/release-20261017/", yaml)
        self.assertIn(f"Host profile: {self.HOST[0]}/{self.HOST[4]}", plan.stdout)
    def test_linux_profile_does_not_depend_on_local_hostname(self):
        host = list(self.HOST)
        host[1] = "another-host"
        self.scenario_is(host=host)
        self.assertEqual(self.invoke("--dry-run", *self.path_args()).returncode, 0)
        result = self.invoke("--create", *self.path_args())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fully_qualified_linux_hostname_is_allowed(self):
        host = list(self.HOST)
        host[1] = "u.example.test"
        self.scenario_is(host=host)
        result = self.invoke("--create", *self.path_args())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unsupported_hosts_fail_before_lima_even_with_override(self):
        for system, machine in (("Linux", "aarch64"), ("Darwin", "x86_64"),
                                ("FreeBSD", "x86_64"), ("Darwin", "aarch64")):
            self.scenario_is(host=[system, "u", "test-release", "test-version", machine])
            for mode in ("--dry-run", "--create"):
                with self.subTest(host=(system, machine), mode=mode):
                    result = self.invoke(mode, "--allow-other-host", *self.path_args())
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(f"unsupported host {system}/{machine}", result.stderr)
                    self.assertEqual(result.stdout, "")
        self.assertEqual(self.calls(), [])
        self.assert_clean_scratch()

    def test_qemu_prerequisites_are_required_only_for_linux_and_outside_mounts(self):
        self.env["PATH"] = str(self.bin)  # Never discover a real host QEMU binary.
        for name in ("qemu-system-x86_64", "qemu-img"):
            binary = self.bin / name
            original = binary.read_bytes()
            binary.unlink()
            if self.HOST[0] == "Darwin":
                continue
            result = self.create(*self.path_args())
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(f"QEMU prerequisite {name} is unavailable", result.stderr)
            for mount in self.paths:
                with self.subTest(binary=name, mount=mount):
                    mounted_binary = mount / name
                    mounted_binary.write_bytes(original)
                    mounted_binary.chmod(0o755)
                    binary.symlink_to(mounted_binary)
                    result = self.create(*self.path_args())
                    self.assertNotEqual(result.returncode, 0)
                    # Results must be empty even before tool validation.
                    self.assertIn("must be empty" if mount == self.paths[2] else
                                  f"{name} must not be an executable", result.stderr)
                    binary.unlink()
                    mounted_binary.unlink()
            binary.write_bytes(original)
            binary.chmod(0o755)
        if self.HOST[0] == "Darwin":
            result = self.create(*self.path_args())
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertEqual(self.calls(), [])
        self.assert_clean_scratch()

    def test_other_profile_lima_version_is_refused(self):
        self.scenario_is(version="2.2.1" if self.HOST[0] == "Linux" else "2.0.3")
        result = self.create(*self.path_args())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"Lima version mismatch: require {self.profile['lima']}", result.stderr)
        self.assertEqual([call["args"][0] for call in self.calls()], ["--version"])
        self.assert_clean_scratch()

    def test_receipt_is_bound_to_profile_and_runtime_checksum(self):
        receipt = self.paths[2] / "setup-receipt.txt"
        for key in ("lima", "ubuntu_sha256", "guest_arch", "node_sha256"):
            with self.subTest(key=key):
                values = bootstrap.expected_receipt(self.versions, self.profile)
                values[key] = "wrong-profile"
                receipt.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
                with self.assertRaisesRegex(ReviewError, "does not match"):
                    bootstrap.verify_receipt(self.paths[2], self.versions, self.profile)

    def test_create_uses_exact_dry_run_config_and_script(self):
        plan = self.invoke("--dry-run", *self.path_args())
        self.assertEqual(plan.returncode, 0, plan.stderr)
        result = self.create(*self.path_args())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call["args"][0] for call in self.calls()],
                         ["--version", "list", "create", "start", "shell"])
        rendered = plan.stdout.split("--- BEGIN LIMA YAML ---\n")[1].split("--- END LIMA YAML ---")[0]
        self.assertEqual(rendered, (self.base / "captured.yaml").read_text())
        guest = plan.stdout.split("--- BEGIN GUEST SCRIPT ---\n")[1].split("--- END GUEST SCRIPT ---")[0]
        self.assertEqual(guest, (self.base / "guest.sh").read_text())
        config_path = (self.base / "config-path.txt").read_text()
        for actual, expected in zip(self.calls()[2:], bootstrap.commands(config_path, self.versions, self.profile)):
            self.assertEqual(actual["args"], expected[1:])
            self.assertIn(shlex.join(["limactl", *actual["args"]]).replace(config_path, "<scratch>/review-vm.yaml")
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
        bootstrap.verify_receipt(self.paths[2], self.versions, self.profile)
        self.assertIn("codex login --device-auth", result.stdout)
        self.assert_clean_scratch()

    def test_existing_vm_is_never_started_or_deleted(self):
        self.scenario_is(existing=bootstrap.VM_NAME)
        result = self.create(*self.path_args())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        self.assertEqual([c["args"][0] for c in self.calls()], ["--version", "list"])
        self.assert_clean_scratch()

    def test_named_vm_plan_receipt_and_existing_legacy_vm_are_independent(self):
        name = 'sonic-static-review-v2'
        legacy = self.lima_state / bootstrap.VM_NAME
        legacy.mkdir()
        (legacy / 'keep.txt').write_text('existing VM state')
        self.scenario_is(existing=bootstrap.VM_NAME)
        plan = self.invoke('--dry-run', '--name', name, *self.path_args())
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertIn('--name=' + name, plan.stdout)
        self.assertIn('limactl delete --force ' + name, plan.stdout)
        receipt = bootstrap.expected_receipt(self.versions, self.profile, name)
        (self.base / 'expected-receipt.txt').write_text(''.join(f'{k}={v}\n' for k, v in receipt.items()))
        result = self.create('--name', name, *self.path_args())
        if self.HOST[0] == 'Darwin':
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Linux/x86_64', result.stderr)
            self.assertEqual(self.calls(), [])
        else:
            self.assertEqual(result.returncode, 0, result.stderr)
            bootstrap.verify_receipt(self.paths[2], self.versions, self.profile, name)
            self.assertEqual(self.calls()[2]['args'][2], '--name=' + name)
            self.assertEqual(self.calls()[-1]['args'][-1], name)
            planned_guest = plan.stdout.split('--- BEGIN GUEST SCRIPT ---\n')[1].split(
                '--- END GUEST SCRIPT ---')[0]
            self.assertEqual((self.base / 'guest.sh').read_text(), planned_guest)
            self.assertIn(name + ' codex login', result.stdout)
        self.assertEqual((legacy / 'keep.txt').read_text(), 'existing VM state')
        self.assertFalse(any(c['args'][0] in ('start', 'shell', 'delete') and
                             bootstrap.VM_NAME in c['args'] for c in self.calls()))
        self.assert_clean_scratch()

    def test_named_vm_cleanup_and_host_policy(self):
        name = 'sonic-static-review-v2'
        host = ['Linux', 'u', 'test-release', 'test-version', 'x86_64']
        self.scenario_is(host=host, version='2.0.3', fail='shell')
        legacy = self.lima_state / bootstrap.VM_NAME
        legacy.mkdir()
        result = self.invoke('--create', '--name', name, *self.path_args())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls()[-1]['args'], ['delete', '--force', name])
        self.assertTrue(legacy.is_dir())
        self.assertFalse((self.lima_state / name).exists())
        host[1] = 'other-host'
        self.scenario_is(host=host, version='2.0.3')
        with mock.patch.object(bootstrap.os, 'uname', return_value=os.uname_result(host)):
            linux_profile = bootstrap.host_profile()
        receipt = bootstrap.expected_receipt(self.versions, linux_profile, name)
        (self.base / 'expected-receipt.txt').write_text(''.join(f'{k}={v}\n' for k, v in receipt.items()))
        result = self.invoke('--create', '--name', name, '--allow-other-host', *self.path_args())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(c['args'][:2] == ['create', '--tty=false'] for c in self.calls()))

    def test_partial_existing_state_and_global_overrides_are_refused(self):
        vm = self.lima_state / bootstrap.VM_NAME
        vm.mkdir()
        result = self.create(*self.path_args())
        self.assertIn("already exists", result.stderr)
        self.assertTrue(vm.is_dir())
        vm.rmdir()
        directory = self.lima_state / "_config"
        directory.mkdir()
        for name in ("default.yaml", "override.yaml"):
            path = directory / name
            path.write_text("must never be loaded")
            result = self.create(*self.path_args())
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing inherited Lima configuration", result.stderr)
            path.unlink()
        self.assertFalse(any(c["args"][0] in ("create", "start", "delete") for c in self.calls()))

    def test_bad_version_missing_binary_and_inventory_failure_are_nonmutating(self):
        self.scenario_is(version="0.0.0")
        result = self.create(*self.path_args())
        self.assertIn("version mismatch", result.stderr)
        self.assertEqual([c["args"][0] for c in self.calls()], ["--version"])
        self.scenario_is(fail="list")
        result = self.create(*self.path_args())
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(c["args"][0] == "create" for c in self.calls()))
        self.fake.unlink()
        self.env["PATH"] = str(self.bin)
        result = self.create(*self.path_args())
        self.assertIn("unavailable", result.stderr)
        self.assertNotEqual(result.returncode, 0)
        self.assert_clean_scratch()

    def test_failures_destroy_partial_vm_and_remove_receipt_and_scratch(self):
        cases = [{"fail": stage} for stage in ("create", "start", "shell")]
        cases += [{"missing_receipt": True}, {"bad_receipt": True}, {"extra_result": True}]
        for scenario in cases:
            with self.subTest(scenario=scenario):
                self.scenario_is(**scenario)
                result = self.create(*self.path_args())
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
                result = self.create(*self.path_args())
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls()[-1]["args"], ["delete", "--force", bootstrap.VM_NAME])
                self.assertFalse((self.lima_state / bootstrap.VM_NAME).exists())
                self.assert_clean_scratch()

    def test_cleanup_failure_is_explicit_and_never_claims_success(self):
        self.scenario_is(bad_receipt=True, fail="delete")
        result = self.create(*self.path_args())
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
                    result = self.create(*self.path_args())
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
                result = self.create(*self.path_args((bad_source, reference, results)))
                self.assertNotEqual(result.returncode, 0)
        (results / "old.txt").write_text("keep me")
        result = self.create(*self.path_args())
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
            with self.subTest(value=value), self.assertRaises(ReviewError):
                validate_config(config)
        config = copy.deepcopy(self.config)
        del config["guest_cli_versions"]
        validate_config(config)  # Existing static preparation profiles remain valid.
        versions = {"codex": "1.2.3", "claude_code": "4.5.6"}
        self.assertEqual(bootstrap.commands("unused.yaml", versions, self.profile)[2][-7:],
                         ["1.2.3", "4.5.6", bootstrap.NODE_VERSION, self.profile["node_sha256"],
                          self.profile["lima"], self.profile["image_sha256"], self.profile["arch"]])

    def test_guest_has_only_pinned_installs_and_version_probes(self):
        guest = (ROOT / "experiment/guest-setup.sh").read_text()
        self.assertIn('"@openai/codex@$codex_version"', guest)
        self.assertIn('"@anthropic-ai/claude-code@$claude_version"', guest)
        self.assertIn("Claude Code requires its official postinstall", guest)
        self.assertIn("--ignore-scripts --no-audit --no-fund", guest)
        self.assertIn("Node archive SHA-256 mismatch", guest)
        self.assertIn('codex_observed=$(codex --version)', guest)
        self.assertIn('claude_observed=$(claude --version)', guest)
        self.assertIn('[[ $codex_observed == "codex-cli $codex_version" ]]', guest)
        self.assertIn('[[ $claude_observed == "$claude_version (Claude Code)" ]]', guest)
        self.assertIn('[[ $(uname -s) == Linux && $(uname -m) == "$guest_arch" ]]', guest)
        self.assertIn("x86_64) node_arch=x64", guest)
        self.assertIn("aarch64) node_arch=arm64", guest)
        for forbidden in (" login", "docker ", "make ", "npm test", "claude -p", "codex exec"):
            self.assertNotIn(forbidden, guest)

    def test_guest_checksum_and_receipt_code_offline(self):
        guest = (ROOT / "experiment/guest-setup.sh").read_text()
        blocks = [block.split("\nPY\n", 1)[0] for block in guest.split("<<'PY'\n")[1:]]
        self.assertEqual(len(blocks), 2)
        archive = self.base / "node.tar.gz"
        payload = b"synthetic runtime archive; never extracted or executed"
        checksum = hashlib.sha256(payload).hexdigest()
        node_arch = "x64" if self.HOST[0] == "Linux" else "arm64"
        for expected in (checksum, "0" * 64):
            with mock.patch.object(sys, "argv", ["-", bootstrap.NODE_VERSION, expected, node_arch, str(archive)]), \
                    mock.patch.object(urllib.request, "urlopen", return_value=io.BytesIO(payload)) as download:
                if expected == checksum:
                    exec(compile(blocks[0], "guest-download", "exec"), {})
                    self.assertEqual(archive.read_bytes(), payload)
                else:
                    with self.assertRaisesRegex(SystemExit, "SHA-256 mismatch"):
                        exec(compile(blocks[0], "guest-download", "exec"), {})
                self.assertEqual(download.call_args.args[0],
                                 f"https://nodejs.org/download/release/v{bootstrap.NODE_VERSION}/"
                                 f"node-v{bootstrap.NODE_VERSION}-linux-{node_arch}.tar.gz")
            archive.unlink()
        args = ["-", self.profile["lima"], self.profile["image_sha256"],
                self.profile["arch"], self.profile["node_sha256"],
                bootstrap.NODE_VERSION, "v" + bootstrap.NODE_VERSION,
                self.versions["codex"], "codex-cli " + self.versions["codex"],
                self.versions["claude_code"], self.versions["claude_code"] + " (Claude Code)"]

        def guest_path(path):
            self.assertEqual(path, "/mnt/results")
            return self.paths[2]

        with mock.patch.object(sys, "argv", args), \
                mock.patch.dict(sys.modules, {"pathlib": SimpleNamespace(Path=guest_path)}):
            exec(compile(blocks[1], "guest-receipt", "exec"), {})
            bootstrap.verify_receipt(self.paths[2], self.versions, self.profile)
            before = (self.paths[2] / "setup-receipt.txt").read_bytes()
            with self.assertRaisesRegex(SystemExit, "still be empty"):
                exec(compile(blocks[1], "guest-receipt", "exec"), {})
            self.assertEqual((self.paths[2] / "setup-receipt.txt").read_bytes(), before)

    def test_guest_receipt_uses_the_selected_new_vm_name(self):
        guest = (ROOT / 'experiment/guest-setup.sh').read_text()
        receipt_code = guest.split("<<'PY'\n")[-1].split('\nPY\n')[0]
        name = 'sonic-static-review-v2'
        expected = bootstrap.expected_receipt(self.versions, self.profile, name)
        fields = ('lima', 'ubuntu_sha256', 'guest_arch', 'node_sha256', 'node_expected', 'node_observed',
                  'codex_expected', 'codex_observed', 'claude_code_expected', 'claude_code_observed', 'vm')
        args = ['-', *[expected[key] for key in fields]]
        with mock.patch.object(sys, 'argv', args), \
                mock.patch.dict(sys.modules, {'pathlib': SimpleNamespace(Path=lambda path: self.paths[2])}):
            exec(compile(receipt_code, 'guest-receipt', 'exec'), {})
        bootstrap.verify_receipt(self.paths[2], self.versions, self.profile, name)
        with self.assertRaises(ReviewError):
            bootstrap.verify_receipt(self.paths[2], self.versions, self.profile)


class MacOSBootstrapTests(BootstrapTests):
    HOST = ("Darwin", "test-mac", "test-release", "test-version", "arm64")
    CREATE_FLAGS = ("--allow-other-host",)


class GuestProvisioningTests(unittest.TestCase):
    """Run only the isolation setup fragment, with all guest commands replaced."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="guest-provisioning-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.bin = self.base / "bin"
        self.work = self.base / "work"
        for directory in (self.bin, self.work, self.base / "diagnostics"):
            directory.mkdir()
        self.log = self.base / "calls.jsonl"
        self.receipt = self.base / "ready"
        fake = self.bin / "guest-command"
        fake.write_text(f"#!{sys.executable} -I\n"
                        + "import json, os, pathlib, sys, tempfile\n"
                        + f"base = pathlib.Path({str(self.base)!r})\n" + r'''
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
scenario = json.loads((base / "scenario.json").read_text())
with (base / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps({"name": name, "args": args}) + "\n")
if name == "id":
    assert args == ["-u"], args
    print(scenario.get("uid", 1000))
elif name == "sudo":
    if args == ["apt-get", "-o", "APT::Update::Error-Mode=any", "update"]:
        stage = "update"
    elif args == ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install",
                  "--yes", "--no-install-recommends", "bubblewrap"]:
        stage = "install"
    elif args[:3] == ["install", "-m", "0644"]:
        assert args[-1] == "/etc/sysctl.d/99-sonic-static-review-userns.conf", args
        stage = "persist"
    elif args == ["sysctl", "-p", "/etc/sysctl.d/99-sonic-static-review-userns.conf"]:
        stage = "apply"
    else:
        raise AssertionError(args)  # Never fall through to real host commands.
    if scenario.get("fail") == stage:
        sys.exit("synthetic " + stage + " failure")
    if stage == "persist":
        (base / "sysctl.conf").write_text(pathlib.Path(args[-2]).read_text())
    if stage == "apply":
        assert "kernel.apparmor_restrict_unprivileged_userns=0" in (base / "sysctl.conf").read_text()
        (base / "sysctl-value").write_text("0")
elif name == "sysctl":
    assert args == ["-n", "kernel.apparmor_restrict_unprivileged_userns"], args
    if scenario.get("fail") == "readback":
        sys.exit("synthetic sysctl read failure")
    print(scenario.get("readback", (base / "sysctl-value").read_text()))
elif name == "python3":
    assert args[:3] == ["-I", "-B", "-"], args
    code = sys.stdin.read()
    sys.argv = args[2:]
    sys.platform = "linux"
    sys.dont_write_bytecode = True
    real_temporary_directory = tempfile.TemporaryDirectory
    def guest_temporary_directory(*positional, **kwargs):
        if kwargs.get("dir") == "/var/tmp":
            kwargs["dir"] = str(base / "diagnostics")
        return real_temporary_directory(*positional, **kwargs)
    tempfile.TemporaryDirectory = guest_temporary_directory
    if scenario.get("missing_bwrap"):
        import shutil
        real_which = shutil.which
        shutil.which = lambda command: None if command == "bwrap" else real_which(command)
    exec(compile(code, "guest-worker-probe", "exec"), {"__name__": "__main__"})
elif name == "bwrap":
    sentinel = pathlib.Path(args[-1])
    assert sentinel.read_text() == "private diagnostic isolation probe"
    assert sentinel.parent.parent.stat().st_mode & 0o777 == 0o700
    status = scenario.get("probe_exit", 0)
    if status:
        print("synthetic namespace/canary failure", file=sys.stderr)
    sys.exit(status)
else:
    raise AssertionError(name)
''')
        fake.chmod(0o755)
        for name in ("id", "sudo", "sysctl", "python3", "bwrap"):
            (self.bin / name).symlink_to(fake)

    def script(self, name="sonic-static-review-v2"):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(bootstrap.main(["--dry-run", "--name", name]), 0)
        return output.getvalue().split("--- BEGIN GUEST SCRIPT ---\n", 1)[1].split(
            "--- END GUEST SCRIPT ---", 1)[0]

    def provision(self, name="sonic-static-review-v2", **scenario):
        guest = self.script(name)
        marker = "# Worker isolation for the new VM only.\n"
        self.assertIn(marker, guest, "guest setup must provision and verify worker isolation")
        fragment = guest.split(marker, 1)[1].split("# Ubuntu cloud images", 1)[0]
        self.assertLess(guest.index(marker), guest.index('values = {"status": "ready"'))
        (self.base / "scenario.json").write_text(json.dumps(scenario))
        # This is an extracted fragment with fake privileged commands, never the
        # full guest script. All actual writes remain in the disposable fixture.
        harness = ("set -euo pipefail\numask 077\n"
                   + f"vm_name={shlex.quote(name)}\nguest_work={shlex.quote(str(self.work))}\n"
                   + fragment + f"\nprintf 'ready\\n' > {shlex.quote(str(self.receipt))}\n")
        return subprocess.run(["/bin/bash", "--noprofile", "--norc", "-s"], input=harness,
                              cwd=self.base, env={"PATH": str(self.bin) + os.pathsep + os.defpath},
                              text=True, capture_output=True, timeout=15, check=False)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_plan_carries_exact_local_probe_modules_without_source_mount_imports(self):
        guest = self.script()
        self.assertIn("<<'WORKER_PROBE'\n", guest)
        code = guest.split("<<'WORKER_PROBE'\n", 1)[1].split("\nWORKER_PROBE\n", 1)[0]
        assignment = next(node for node in ast.parse(code).body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == "probe_sources"
                                  for target in node.targets))
        self.assertEqual(ast.literal_eval(assignment.value), {
            name: (ROOT / "experiment/src" / name).read_text()
            for name in ("common.py", "diagnostics.py")})

    def test_fresh_guest_installs_persists_and_runs_exact_probe_before_ready(self):
        result = self.provision()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.receipt.read_text(), "ready\n")
        self.assertEqual((self.base / "sysctl.conf").read_text(),
                         "kernel.apparmor_restrict_unprivileged_userns=0\n")
        calls = self.calls()
        self.assertEqual([call["name"] for call in calls],
                         ["id", "sudo", "sudo", "sudo", "sudo", "sysctl", "python3", "bwrap"])
        from diagnostics import isolation_prefix
        probe_args = calls[-1]["args"]
        root = Path(probe_args[-1]).parent.parent
        with mock.patch("diagnostics.sys.platform", "linux"), \
                mock.patch("diagnostics.shutil.which", return_value=str(self.bin / "bwrap")):
            prefix = isolation_prefix(root)
        self.assertEqual(probe_args[:len(prefix) - 1], prefix[1:])
        self.assertEqual(probe_args[len(prefix) - 1:len(prefix) + 2], [sys.executable, "-I", "-c"])
        self.assertFalse(root.exists())
        self.assertEqual(list((self.base / "diagnostics").iterdir()), [])

    def test_provisioning_and_probe_failures_never_declare_ready(self):
        scenarios = [{"fail": stage} for stage in ("update", "install", "persist", "apply", "readback")]
        scenarios += [{"readback": "1"}, {"missing_bwrap": True}, {"probe_exit": 1}, {"probe_exit": 9}]
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                shutil.rmtree(self.work)
                self.work.mkdir()
                self.log.unlink(missing_ok=True)
                result = self.provision(**scenario)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(self.receipt.exists())
                if "fail" in scenario or "readback" in scenario:
                    self.assertFalse(any(call["name"] in ("python3", "bwrap") for call in self.calls()))
                else:
                    self.assertIn("guest worker isolation preflight failed", result.stderr)
                self.assertEqual(list((self.base / "diagnostics").iterdir()), [])

    def test_root_cannot_pass_the_unprivileged_probe(self):
        result = self.provision(uid=0)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unprivileged guest user", result.stderr)
        self.assertEqual([call["name"] for call in self.calls()], ["id"])
        self.assertFalse(self.receipt.exists())

    def test_legacy_guest_skips_new_provisioning_and_probe(self):
        result = self.provision(name=bootstrap.VM_NAME)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.receipt.read_text(), "ready\n")


if __name__ == "__main__":
    unittest.main()
