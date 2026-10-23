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


if __name__ == '__main__':
    unittest.main()
