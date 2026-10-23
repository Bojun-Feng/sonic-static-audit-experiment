"""Render and execute one fresh Lima setup; never run source or authenticate."""

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile

from common import AuditError, read_json, require, validate_config

EXPERIMENT = Path(__file__).resolve().parents[1]
ROOT = EXPERIMENT.parent
LIMA_VERSION = "2.2.1"
VM_NAME = "sonic-static-audit"
IMAGE = "https://cloud-images.ubuntu.com/releases/noble/release-20261017/ubuntu-24.04-server-cloudimg-arm64.img"
IMAGE_SHA256 = "50e24e1fb79e89116e939d90aa7738f7af48c442fa1bc2510131e0b39c50348e"
NODE_VERSION = "22.14.0"
NODE_SHA256 = "8cf30ff7250f9463b53c18f89c6c606dfda70378215b2c905d0a9a8b08bd45e0"
PLACEHOLDERS = ("/absolute/path/to/sonic-swss", "/absolute/path/to/reference-materials",
                "/absolute/path/to/audit-results")


def mount_paths(values):
    """Validate metadata only: never walk input trees or read their contents."""
    paths = []
    host_home = Path.home().resolve()
    for label, value in zip(("source", "reference", "results"), values):
        path = Path(value)
        require(path.is_absolute(), f"--{label} must be an absolute path")
        require(all(ord(c) >= 32 and ord(c) != 127 for c in value)
                and "{{" not in value and "}}" not in value,
                f"--{label}: control characters and Lima template expressions are forbidden")
        require(not path.is_symlink() and path.is_dir(),
                f"--{label} must be an existing directory, not a symlink")
        path = path.resolve(strict=True)
        require("{{" not in str(path) and "}}" not in str(path)
                and all(ord(c) >= 32 and ord(c) != 127 for c in str(path)),
                f"--{label}: resolved path contains control characters or a Lima template expression")
        require(path != Path(path.anchor) and not host_home.is_relative_to(path)
                and not ROOT.parent.is_relative_to(path),
                f"--{label}: refusing host home, repository parent, or their ancestors")
        require(not any(p.startswith(".") for p in path.parts),
                f"--{label}: hidden/credential directories cannot be mounted")
        require(os.access(path, os.R_OK | os.X_OK), f"--{label} is not readable/searchable")
        paths.append(path)
    for i, left in enumerate(paths):
        for right in paths[i + 1:]:
            require(not left.is_relative_to(right) and not right.is_relative_to(left),
                    "source, reference, and results directories must be disjoint")
    require(os.access(paths[2], os.W_OK), "--results must be writable")
    require(not any(paths[2].iterdir()), "--results must be empty; use a fresh directory")
    return tuple(paths)


def render_config(paths):
    # JSON strings are valid YAML scalars; user paths are never shell/YAML code.
    source, reference, results = (json.dumps(str(p), ensure_ascii=True) for p in paths)
    return f"""minimumLimaVersion: "{LIMA_VERSION}"
vmType: vz
arch: aarch64
cpus: 4
memory: 8GiB
disk: 40GiB
images:
  - location: "{IMAGE}"
    arch: aarch64
    digest: "sha256:{IMAGE_SHA256}"
user:
  name: audit
  home: /home/audit
  shell: /bin/bash
mountType: virtiofs
mounts:
  - location: {source}
    mountPoint: /mnt/source
    writable: false
  - location: {reference}
    mountPoint: /mnt/references
    writable: false
  - location: {results}
    mountPoint: /mnt/results
    writable: true
containerd:
  system: false
  user: false
ssh:
  forwardAgent: false
  loadDotSSHPubKeys: false
  forwardX11: false
  forwardX11Trusted: false
propagateProxyEnv: false
env:
  DISABLE_AUTOUPDATER: "1"
  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1"
portForwards:
  - guestPortRange: [1, 65535]
    ignore: true
"""


def commands(config_path, versions):
    return [
        ["limactl", "create", "--tty=false", f"--name={VM_NAME}", str(config_path)],
        ["limactl", "start", "--tty=false", VM_NAME],
        ["limactl", "shell", "--workdir=/", VM_NAME, "/bin/bash", "--noprofile", "--norc",
         "-s", "--", versions["codex"], versions["claude_code"], NODE_VERSION,
         NODE_SHA256, LIMA_VERSION, IMAGE_SHA256],
    ]


def authentication_commands():
    print("Manual authentication only (run later; never run by bootstrap):")
    print(f"limactl shell --workdir=/home/audit {VM_NAME} codex login --device-auth")
    print(f"limactl shell --workdir=/home/audit {VM_NAME} claude auth login")


def print_plan(paths, versions, guest_script):
    print("DRY RUN: no commands executed, downloads, files, or VM state changed.")
    print("Host: validate absolute existing, disjoint mount directories; results must be empty.")
    print("Host: pass only PATH, HOME, USER, LOGNAME, TMPDIR, LIMA_HOME, and LC_ALL=C to Lima.")
    print(f"Host: require `limactl --version` == 'limactl version {LIMA_VERSION}'.")
    print("Host: reject Lima _config/default.yaml and override.yaml (no inherited configuration).")
    print("Host: require Lima state and temporary scratch outside all mounts.")
    print(f"Host: limactl list --quiet; refuse existing {VM_NAME} (including stopped VMs).")
    print("Host: create a private mode-0700 temporary directory under the system temp directory/TMPDIR.")
    print("Host: write mode-0600 <scratch>/audit.yaml with exactly this content:")
    print("--- BEGIN LIMA YAML ---")
    print(render_config(paths), end="")
    print("--- END LIMA YAML ---")
    for command in commands("<scratch>/audit.yaml", versions):
        print("Host: " + shlex.join(command))
    print("Guest: the shell command receives exactly this script on stdin:")
    print("--- BEGIN GUEST SCRIPT ---")
    print(guest_script, end="")
    print("--- END GUEST SCRIPT ---")
    print("Host: verify only setup-receipt.txt exists and observed versions match the pins.")
    print("Host: remove private scratch on success and failure.")
    print(f"On create/start/setup/receipt failure or interruption: limactl delete --force {VM_NAME}")
    print("Remove any incomplete setup-receipt.txt. A retry starts from scratch, never repairs a VM.")
    print("Static-only: no source execution, builds/tests, Docker, virtual switches, or model agents.")
    authentication_commands()


def expected_receipt(versions):
    return {
        "status": "ready", "lima": LIMA_VERSION, "vm": VM_NAME,
        "ubuntu_sha256": IMAGE_SHA256,
        "node_expected": NODE_VERSION, "node_observed": f"v{NODE_VERSION}",
        "codex_expected": versions["codex"], "codex_observed": f"codex-cli {versions['codex']}",
        "claude_code_expected": versions["claude_code"],
        "claude_code_observed": f"{versions['claude_code']} (Claude Code)",
        "authentication": "not-run", "static_only": "true",
    }


def verify_receipt(results, versions):
    receipt = results / "setup-receipt.txt"
    require(set(results.iterdir()) == {receipt}, "results must contain only setup-receipt.txt")
    require(not receipt.is_symlink() and receipt.is_file() and receipt.stat().st_size < 4096,
            "setup receipt must be a small regular file")
    lines = receipt.read_text(encoding="utf-8").splitlines()
    pairs = [line.split("=", 1) for line in lines]
    require(all(len(pair) == 2 for pair in pairs), "invalid setup receipt")
    require(len(pairs) == len(dict(pairs)) and dict(pairs) == expected_receipt(versions),
            "setup receipt does not match pinned/observed versions")


def create_vm(paths, versions, guest_script):
    # Do not pass provider tokens, SSH_AUTH_SOCK, shell startup overrides, or
    # host proxy credentials to Lima. Do not inspect any credential files.
    env = {key: os.environ[key] for key in ("PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LIMA_HOME")
           if key in os.environ}
    env["LC_ALL"] = "C"
    lima = shutil.which("limactl", path=env.get("PATH", os.defpath))
    require(lima is not None, f"pinned Lima {LIMA_VERSION} is unavailable; install it manually")
    lima = str(Path(lima).resolve())

    def run(args, **kwargs):
        return subprocess.run([lima, *args], env=env, check=True, text=True, **kwargs)

    lima_home = Path(env.get("LIMA_HOME", str(Path.home() / ".lima")))
    require(lima_home.is_absolute(), "LIMA_HOME must be absolute when set")
    lima_home = lima_home.resolve()
    require(not any(lima_home.is_relative_to(p) or p.is_relative_to(lima_home) for p in paths),
            "Lima state must be outside and disjoint from all mounts")
    require(not any(Path(lima).is_relative_to(p) for p in paths),
            "limactl must not be an executable from a mounted input/results directory")
    observed = run(["--version"], stdout=subprocess.PIPE).stdout.strip()
    require(observed == f"limactl version {LIMA_VERSION}",
            f"Lima version mismatch: require {LIMA_VERSION}, got {observed}")
    for name in ("default.yaml", "override.yaml"):
        path = lima_home / "_config" / name
        require(not path.exists() and not path.is_symlink(),
                f"refusing inherited Lima configuration: {name}; use a clean LIMA_HOME")
    names = run(["list", "--quiet"], stdout=subprocess.PIPE).stdout.splitlines()
    require(VM_NAME not in names and not (lima_home / VM_NAME).exists()
            and not (lima_home / VM_NAME).is_symlink(),
            f"VM {VM_NAME} already exists; refusing to repair or replace it")

    attempted = False
    results = paths[2]
    # Check an explicit TMPDIR before tempfile probes it by writing a file.
    if "TMPDIR" in env:
        requested_scratch = Path(env["TMPDIR"]).resolve()
        require(requested_scratch.is_dir() and os.access(requested_scratch, os.W_OK | os.X_OK),
                "TMPDIR must be an existing writable/searchable directory")
        require(not any(requested_scratch.is_relative_to(p) for p in paths),
                "temporary scratch must be outside all mounts; set TMPDIR accordingly")
    scratch_parent = Path(tempfile.gettempdir()).resolve()
    require(not any(scratch_parent.is_relative_to(p) for p in paths),
            "temporary scratch must be outside all mounts; set TMPDIR accordingly")
    try:
        # The context also cleans up on failed creation, start, or guest setup.
        with tempfile.TemporaryDirectory(prefix="sonic-static-audit-", dir=scratch_parent) as directory:
            scratch = Path(directory)
            config_path = scratch / "audit.yaml"
            with config_path.open("x", encoding="utf-8") as stream:
                os.chmod(config_path, 0o600)
                stream.write(render_config(paths))
            create, start, setup = commands(config_path, versions)
            attempted = True  # Failed create can already have allocated VM state.
            run(create[1:])
            run(start[1:])
            run(setup[1:], input=guest_script)
            verify_receipt(results, versions)
        print(f"Created {VM_NAME}; verified {results / 'setup-receipt.txt'}.")
        authentication_commands()
    except BaseException:
        if attempted:
            print(f"Bootstrap failed; destroying {VM_NAME}. A retry starts from scratch.", file=sys.stderr)
            # Finish cleanup even if a second Ctrl-C arrives.
            old_handlers = {sig: signal.signal(sig, signal.SIG_IGN)
                            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
            try:
                try:
                    run(["delete", "--force", VM_NAME])
                except (OSError, subprocess.CalledProcessError):
                    print(f"ERROR: VM deletion failed. Run `limactl delete --force {VM_NAME}` "
                          "before retrying; do not reuse the partial VM.", file=sys.stderr)
                receipt = results / "setup-receipt.txt"
                if receipt.is_file() or receipt.is_symlink():
                    receipt.unlink()
            finally:
                for sig, handler in old_handlers.items():
                    signal.signal(sig, handler)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="print the complete plan without mutation")
    mode.add_argument("--create", action="store_true", help="create a fresh VM and install pinned guest CLIs")
    for name in ("source", "reference", "results"):
        parser.add_argument(f"--{name}", help="absolute existing directory (required for --create)")
    args = parser.parse_args(argv)
    values = (args.source, args.reference, args.results)
    if any(values) and not all(values) or args.create and not all(values):
        parser.error("--source, --reference, and --results must all be supplied")
    try:
        config = validate_config(read_json(EXPERIMENT / "config.json"))
        require("guest_cli_versions" in config, "config.json requires guest_cli_versions")
        versions = config["guest_cli_versions"]
        paths = mount_paths(values) if all(values) else tuple(map(Path, PLACEHOLDERS))
        guest_script = (EXPERIMENT / "guest-setup.sh").read_text(encoding="utf-8")
        if args.dry_run:
            print_plan(paths, versions, guest_script)
        else:
            create_vm(paths, versions, guest_script)
    except (AuditError, OSError, ValueError, subprocess.CalledProcessError, KeyboardInterrupt) as exc:
        print(f"ERROR: {exc or 'bootstrap interrupted'}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"bootstrap interrupted by signal {signum}")

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    sys.exit(main())
