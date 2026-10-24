"""Offline enforcement contracts; no VM, provider, or authentication calls."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'experiment/src'))
from common import AttemptError, ReviewError
from diagnostics import isolation_prefix, private_root, verified_worker_prefix


class DiagnosticsTests(unittest.TestCase):
    def test_parallel_runners_must_share_the_same_private_root(self):
        from scheduler import bind_diagnostics_root
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            bind_diagnostics_root(base / 'locks', base / 'diagnostics')
            bind_diagnostics_root(base / 'locks', base / 'diagnostics')
            with self.assertRaises(ReviewError):
                bind_diagnostics_root(base / 'locks', base / 'other-diagnostics')

    def test_root_rejects_overlap_alias_and_public_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            result = base / 'results'
            result.mkdir()
            for root in (ROOT / 'private', result / 'private', base):
                with self.assertRaises(ReviewError):
                    private_root(root, [result])
            alias = base / 'alias'
            alias.symlink_to(result, target_is_directory=True)
            with self.assertRaises(ReviewError):
                private_root(alias, [result])
            public = base / 'public'
            public.mkdir(mode=0o755)
            with self.assertRaises(ReviewError):
                private_root(public, [result])
            root = private_root(base / 'diagnostics', [result])
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)

    def test_linux_hides_private_root_and_parent_process_filesystem(self):
        root = Path('/var/tmp/review-diagnostics')
        with mock.patch('diagnostics.sys.platform', 'linux'), \
                mock.patch('diagnostics.shutil.which', return_value='/usr/bin/bwrap'):
            command = isolation_prefix(root)
        self.assertEqual(command[:2], ['/usr/bin/bwrap', '--die-with-parent'])
        self.assertIn('--unshare-user', command)
        self.assertIn('--unshare-pid', command)
        # A plain bind of / carries nodev, so the CLI's TTY-backed shell tool
        # cannot open /dev/null or allocate a PTY. Give the worker bubblewrap's
        # minimal private /dev right after the root bind; never the host's
        # full device tree.
        bind = command.index('--bind')
        self.assertEqual(command[bind:bind + 5], ['--bind', '/', '/', '--dev', '/dev'])
        self.assertNotIn('--dev-bind', command)
        self.assertLess(command.index('--dev'), command.index('--tmpfs'))
        self.assertEqual(command[command.index('--tmpfs') + 1], str(root))
        self.assertEqual(command[command.index('--remount-ro') + 1], str(root))
        self.assertEqual(command[command.index('--proc') + 1], '/proc')
        self.assertEqual(command[-3:], ['--cap-drop', 'ALL', '--'])

    def test_macos_denies_diagnostic_reads_writes_and_other_process_access(self):
        root = Path('/private/tmp/review "private"')
        with mock.patch('diagnostics.sys.platform', 'darwin'):
            command = isolation_prefix(root)
        self.assertEqual(command[:2], ['/usr/bin/sandbox-exec', '-p'])
        self.assertIn('deny file-read* file-write*', command[2])
        self.assertIn('deny process-info* (target others)', command[2])
        self.assertIn('deny mach-priv-task-port', command[2])
        self.assertIn('\\"private\\"', command[2])

    def test_hidden_trees_are_masked_and_inputs_re_exposed_read_only(self):
        root = Path('/var/tmp/review-diagnostics')
        hidden = [Path('/mnt/results')]
        exposed = [Path('/mnt/source'), Path('/mnt/results/production/prepared/exhaustive'),
                   Path('/mnt/results/production/exhaustive-run/results/validation/c-1')]
        with mock.patch('diagnostics.sys.platform', 'linux'), \
                mock.patch('diagnostics.shutil.which', return_value='/usr/bin/bwrap'):
            command = isolation_prefix(root, hidden, exposed)
        positions = [i for i, arg in enumerate(command) if arg == '/mnt/results']
        # Mask the hidden tree, re-expose the inputs read-only inside it, then seal the mask.
        self.assertEqual([command[i - 1] for i in positions], ['--tmpfs', '--remount-ro'])
        binds = [i for i, arg in enumerate(command) if arg == '--ro-bind']
        self.assertEqual([command[i + 1] for i in binds], [str(p) for p in exposed])
        self.assertTrue(all(command[i + 1] == command[i + 2] for i in binds))
        self.assertLess(command.index(str(root)), positions[0])
        self.assertLess(positions[0], binds[0])
        self.assertLess(binds[-1], positions[1])
        self.assertEqual(command[-3:], ['--cap-drop', 'ALL', '--'])
        with mock.patch('diagnostics.sys.platform', 'darwin'):
            command = isolation_prefix(root, hidden, exposed)
        profile = command[2]
        deny = profile.index('(deny file-read* file-write* (subpath "/mnt/results"))')
        allow = profile.index('(allow file-read* (subpath "/mnt/results/production/prepared/exhaustive"))')
        self.assertLess(deny, allow)
        self.assertIn('(allow file-read* (subpath "/mnt/source"))', profile)
        self.assertLess(profile.index(str(root)), deny)

    @unittest.skipUnless(sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').exists(), 'real sandbox-exec probe')
    def test_real_probe_denies_hidden_trees_and_allows_exposed_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / 'diagnostics'
            root.mkdir(mode=0o700)
            hidden = base / 'runs'
            exposed = hidden / 'prepared'
            exposed.mkdir(parents=True)
            (exposed / 'manifest.json').write_text('{}')
            probe_dir = hidden / 'exhaustive-run/work'
            probe_dir.mkdir(parents=True)
            try:
                prefix = verified_worker_prefix(root, [], [hidden], [exposed], probe_dir)
            except AttemptError as exc:
                if 'Operation not permitted' in str(exc):
                    self.skipTest('nested sandbox unavailable here')
                raise
            self.assertEqual(prefix[0], '/usr/bin/sandbox-exec')
            self.assertEqual(list(probe_dir.iterdir()), [])
            self.assertEqual(list(root.iterdir()), [])
            real = isolation_prefix
            # Without the exposure the probe refuses: an input the worker needs is unreadable.
            with mock.patch('diagnostics.isolation_prefix', side_effect=lambda r, h=(), e=(): real(r, h, ())):
                with self.assertRaises(AttemptError) as caught:
                    verified_worker_prefix(root, [], [hidden], [exposed], probe_dir)
            self.assertIn('exposed', str(caught.exception))
            # Without the mask the hidden sentinel is readable: refused as well.
            with mock.patch('diagnostics.isolation_prefix', side_effect=lambda r, h=(), e=(): real(r, (), e)):
                with self.assertRaises(AttemptError) as caught:
                    verified_worker_prefix(root, [], [hidden], [exposed], probe_dir)
            self.assertIn('hidden', str(caught.exception))
            self.assertEqual(list(probe_dir.iterdir()), [])

    def test_missing_enforcement_never_falls_back_to_unprotected_execution(self):
        with mock.patch('diagnostics.sys.platform', 'linux'), \
                mock.patch('diagnostics.shutil.which', return_value=None), \
                mock.patch('diagnostics.subprocess.run') as run:
            with self.assertRaises(AttemptError) as caught:
                verified_worker_prefix(Path('/var/tmp/not-created'))
            self.assertFalse(caught.exception.retryable)
            self.assertEqual(caught.exception.reason, 'isolation_unavailable')
            run.assert_not_called()

    def test_isolation_binary_from_source_is_refused_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / 'source'
            with mock.patch('diagnostics.isolation_prefix', return_value=[str(source / 'bwrap')]), \
                    mock.patch('diagnostics.subprocess.run') as run:
                with self.assertRaises(AttemptError):
                    verified_worker_prefix(base, [source])
                run.assert_not_called()

    def test_probe_rejects_unavailable_sandbox_or_readable_canary_and_cleans_scratch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for returncode in (1, 9):
                with mock.patch('diagnostics.isolation_prefix', return_value=['sandbox']), \
                        mock.patch('diagnostics.subprocess.run', return_value=subprocess.CompletedProcess(
                            [], returncode, b'', b'private exact failure')):
                    with self.assertRaises(AttemptError) as caught:
                        verified_worker_prefix(root)
                    self.assertEqual(caught.exception.stage, 'worker_isolation')
                    self.assertNotIn('private exact failure', str(caught.exception.summary()))
                self.assertEqual(list(root.iterdir()), [])
            # Running the real local probe WITHOUT a sandbox must detect that
            # another same-user process can read mode-0700 diagnostics.
            with mock.patch('diagnostics.isolation_prefix', return_value=[]):
                with self.assertRaises(AttemptError):
                    verified_worker_prefix(root)
            self.assertEqual(list(root.iterdir()), [])

    def shim_prefix(self, directory, patches):
        """A stand-in isolation prefix: runs the real probe in-process after
        patching os primitives, so device failures can be simulated offline."""
        shim = Path(directory) / 'shim.py'
        shim.write_text('import os, sys\n'
                        'sentinel = sys.argv[-1]\n'
                        'real_open = os.open\n'
                        'def masked_open(path, flags, *args, **kwargs):\n'
                        '    if os.fspath(path) == sentinel or os.fspath(path) in DENIED:\n'
                        '        raise PermissionError(13, "denied by test shim")\n'
                        '    return real_open(path, flags, *args, **kwargs)\n'
                        'def no_pty():\n'
                        '    raise PermissionError(13, "pty denied by test shim")\n'
                        + patches +
                        'os.open = masked_open\n'
                        'argv = sys.argv[1:]\n'
                        'assert argv[1:3] == ["-I", "-c"], argv\n'
                        'sys.argv = ["-c", *argv[4:]]\n'
                        'exec(compile(argv[3], "probe", "exec"), {"__name__": "__main__"})\n')
        return [sys.executable, '-I', str(shim)]

    def test_probe_requires_usable_device_nodes_and_pty_inside_the_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'diagnostics'
            root.mkdir(mode=0o700)
            # Masked canary with working devices: the probe passes and returns the prefix.
            prefix = self.shim_prefix(directory, 'DENIED = set()\n')
            with mock.patch('diagnostics.isolation_prefix', return_value=prefix):
                self.assertEqual(verified_worker_prefix(root), prefix)
            self.assertEqual(list(root.iterdir()), [])
            # The same masked canary, but no PTY or no /dev/null: readiness must fail
            # before any provider call, privately naming the unusable device.
            for patches, marker in (('DENIED = set()\nos.openpty = no_pty\n', 'pty'),
                                    ('DENIED = {"/dev/null"}\n', '/dev/null')):
                prefix = self.shim_prefix(directory, patches)
                with mock.patch('diagnostics.isolation_prefix', return_value=prefix):
                    with self.assertRaises(AttemptError) as caught:
                        verified_worker_prefix(root)
                self.assertEqual(caught.exception.summary(),
                                 {'category': 'configuration', 'stage': 'worker_isolation',
                                  'reason': 'isolation_unavailable', 'retryable': False})
                self.assertIn(marker, str(caught.exception))
                self.assertNotIn(marker, str(caught.exception.summary()))
                self.assertEqual(list(root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
