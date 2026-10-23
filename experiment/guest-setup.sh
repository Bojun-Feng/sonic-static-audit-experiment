#!/bin/bash
# Sent on stdin to the fresh guest. Never execute this script on the host.
set -euo pipefail
umask 077
[[ $# -eq 7 ]] || { printf 'ERROR: expected seven pinned setup arguments\n' >&2; exit 2; }
codex_version=$1
claude_version=$2
node_version=$3
node_sha256=$4
lima_version=$5
image_sha256=$6
guest_arch=$7
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
guest_work=$(mktemp -d /tmp/sonic-static-audit.XXXXXXXX)
trap 'rm -rf -- "$guest_work"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
export DISABLE_AUTOUPDATER=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

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
sudo install -d -m 0755 /opt/audit-node
sudo tar -xzf "$guest_work/node.tar.gz" --strip-components=1 -C /opt/audit-node
sudo ln -s /opt/audit-node/bin/node /usr/local/bin/node
export PATH=/opt/audit-node/bin:/usr/local/bin:/usr/bin:/bin
[[ $(node --version) == "v$node_version" ]]
sudo install -d -m 0755 -o "$(id -u)" -g "$(id -g)" /opt/audit-clis
# Codex installs without lifecycle scripts. Claude Code requires its official postinstall
# to fetch the pinned platform binary, so install it separately with scripts enabled.
npm install --global --prefix /opt/audit-clis --cache "$guest_work/npm-cache" \
    --userconfig /dev/null --registry https://registry.npmjs.org \
    --ignore-scripts --no-audit --no-fund --update-notifier=false \
    "@openai/codex@$codex_version"
npm install --global --prefix /opt/audit-clis --cache "$guest_work/npm-cache" \
    --userconfig /dev/null --registry https://registry.npmjs.org \
    --no-audit --no-fund --update-notifier=false \
    "@anthropic-ai/claude-code@$claude_version"

# Persist update/telemetry restrictions for subsequent manual guest sessions.
cat > "$guest_work/codex" <<'SH'
#!/bin/sh
exec /opt/audit-clis/bin/codex -c check_for_update_on_startup=false "$@"
SH
cat > "$guest_work/claude" <<'SH'
#!/bin/sh
export DISABLE_AUTOUPDATER=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
exec /opt/audit-clis/bin/claude "$@"
SH
sudo install -m 0755 "$guest_work/codex" "$guest_work/claude" /usr/local/bin/
node_observed=$(node --version)
codex_observed=$(codex --version)
claude_observed=$(claude --version)
[[ $codex_observed == "codex-cli $codex_version" ]]
[[ $claude_observed == "$claude_version (Claude Code)" ]]

# Publish the only result after all installations and version checks succeed.
# Exclusive creation prevents overwriting existing results; host cleanup removes
# an incomplete receipt on any subsequent failure, including lost transport.
python3 - "$lima_version" "$image_sha256" "$guest_arch" "$node_sha256" "$node_version" "$node_observed" \
    "$codex_version" "$codex_observed" "$claude_version" "$claude_observed" <<'PY'
from pathlib import Path
import os
import sys
keys = ("lima", "ubuntu_sha256", "guest_arch", "node_sha256", "node_expected", "node_observed", "codex_expected",
        "codex_observed", "claude_code_expected", "claude_code_observed")
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
