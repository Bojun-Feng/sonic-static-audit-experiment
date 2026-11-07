#!/bin/bash
# Sent on stdin to the fresh guest. Never execute this script on the host.
set -euo pipefail
umask 077
[[ $# -eq 6 || $# -eq 7 ]] || { printf 'ERROR: expected six pinned setup arguments and optional VM name\n' >&2; exit 2; }
codex_version=$1
node_version=$2
node_sha256=$3
lima_version=$4
image_sha256=$5
guest_arch=$6
vm_name=${7:-sonic-static-audit}
[[ $vm_name == sonic-static-audit || $vm_name == sonic-static-review-v2 ]] || exit 2
case "$guest_arch" in
    x86_64) node_arch=x64 ;;
    aarch64) node_arch=arm64 ;;
    *) printf 'ERROR: unsupported guest architecture\n' >&2; exit 1 ;;
esac
[[ $(uname -s) == Linux && $(uname -m) == "$guest_arch" ]] || {
    printf 'ERROR: guest architecture does not match the host profile\n' >&2
    exit 1
}
[[ -d /mnt/source && -d /mnt/references && -d /mnt/results ]] || exit 1

# Use only guest-local scratch, never a source/reference working directory.
cd /
guest_work=$(mktemp -d /tmp/review-setup.XXXXXXXX)
trap 'rm -rf -- "$guest_work"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

# Worker isolation for the sonic-static-review-v2 profile.
if [[ $vm_name == sonic-static-review-v2 ]]; then
    [[ $(id -u) -ne 0 ]] || {
        printf 'ERROR: worker isolation must be checked as the unprivileged guest user\n' >&2
        exit 1
    }
    # Install bubblewrap and required dependencies from the image's Ubuntu repositories.
    sudo apt-get -o APT::Update::Error-Mode=any update
    sudo env DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends bubblewrap

    # Ubuntu's AppArmor restriction otherwise denies the runner's user namespace.
    # Persist and apply only this new guest's setting; do not reload other sysctls.
    cat > "$guest_work/worker-userns.conf" <<'SYSCTL'
kernel.apparmor_restrict_unprivileged_userns=0
SYSCTL
    sudo install -m 0644 "$guest_work/worker-userns.conf" /etc/sysctl.d/99-sonic-static-review-userns.conf
    sudo sysctl -p /etc/sysctl.d/99-sonic-static-review-userns.conf
    [[ $(sysctl -n kernel.apparmor_restrict_unprivileged_userns) == 0 ]] || {
        printf 'ERROR: guest unprivileged user namespaces remain restricted\n' >&2
        exit 1
    }

    # The planner embeds our own modules, not code from the reviewed source mount.
    # Run the same sentinel probe as the runner, without sudo, before any receipt.
    python3 -I -B - "$guest_work" <<'WORKER_PROBE'
from pathlib import Path
import sys
import tempfile

probe_sources = None  # BOOTSTRAP_WORKER_ISOLATION_SOURCES
if probe_sources is None:
    raise SystemExit("ERROR: worker probe must be supplied by the bootstrap planner")
modules = Path(sys.argv[1]) / "worker-probe"
modules.mkdir(mode=0o700)
for name, source in probe_sources.items():
    (modules / name).write_text(source, encoding="utf-8")
sys.path.insert(0, str(modules))
from common import AttemptError
from diagnostics import verified_worker_prefix

with tempfile.TemporaryDirectory(prefix="review-isolation-", dir="/var/tmp") as directory:
    try:
        verified_worker_prefix(Path(directory), tuple(map(Path, ("/mnt/source", "/mnt/references", "/mnt/results"))))
    except AttemptError as exc:
        raise SystemExit(f"ERROR: guest worker isolation preflight failed: {exc}") from exc
WORKER_PROBE
fi

# Ubuntu cloud images supply Python, CA certificates, tar, and gzip. Install a
# checksummed Node binary; no apt upgrade, source build, or unpinned installer.
python3 - "$node_version" "$node_sha256" "$node_arch" "$guest_work/node.tar.gz" <<'PY'
import hashlib
import sys
import urllib.request
version, expected, arch, target = sys.argv[1:]
url = f"https://nodejs.org/download/release/v{version}/node-v{version}-linux-{arch}.tar.gz"
digest = hashlib.sha256()
with urllib.request.urlopen(url, timeout=60) as response, open(target, "xb") as stream:
    while chunk := response.read(1024 * 1024):
        digest.update(chunk)
        stream.write(chunk)
if digest.hexdigest() != expected:
    raise SystemExit("Node archive SHA-256 mismatch")
PY
# Install tools in /opt/audit-node and /opt/audit-clis.
sudo install -d -m 0755 /opt/audit-node
sudo tar -xzf "$guest_work/node.tar.gz" --strip-components=1 -C /opt/audit-node
sudo ln -s /opt/audit-node/bin/node /usr/local/bin/node
export PATH=/opt/audit-node/bin:/usr/local/bin:/usr/bin:/bin
[[ $(node --version) == "v$node_version" ]]
sudo install -d -m 0755 -o "$(id -u)" -g "$(id -g)" /opt/audit-clis
# Install the pinned Codex CLI with npm lifecycle scripts disabled.
npm install --global --prefix /opt/audit-clis --cache "$guest_work/npm-cache" \
    --userconfig /dev/null --registry https://registry.npmjs.org \
    --ignore-scripts --no-audit --no-fund --update-notifier=false \
    "@openai/codex@$codex_version"

# Persist update restrictions for subsequent manual guest sessions.
cat > "$guest_work/codex" <<'SH'
#!/bin/sh
exec /opt/audit-clis/bin/codex -c check_for_update_on_startup=false "$@"
SH
sudo install -m 0755 "$guest_work/codex" /usr/local/bin/
node_observed=$(node --version)
codex_observed=$(codex --version)
[[ $codex_observed == "codex-cli $codex_version" ]]

# Publish the only result after all installations, version checks, and any
# required worker isolation probe succeed.
# Exclusive creation prevents overwriting existing results; host cleanup removes
# an incomplete receipt on any subsequent failure, including lost transport.
python3 - "$lima_version" "$image_sha256" "$guest_arch" "$node_sha256" "$node_version" "$node_observed" \
    "$codex_version" "$codex_observed" "$vm_name" <<'PY'
from pathlib import Path
import os
import sys
keys = ("lima", "ubuntu_sha256", "guest_arch", "node_sha256", "node_expected", "node_observed", "codex_expected",
        "codex_observed", "vm")
values = {"status": "ready", "vm": "sonic-static-audit", **dict(zip(keys, sys.argv[1:])),
          "authentication": "not-run", "static_only": "true"}
results = Path("/mnt/results")
if any(results.iterdir()):
    raise SystemExit("Results must still be empty")
with (results / "setup-receipt.txt").open("x", encoding="utf-8") as receipt:
    receipt.write("".join(f"{key}={value}\n" for key, value in values.items()))
    receipt.flush()
    os.fsync(receipt.fileno())
PY
