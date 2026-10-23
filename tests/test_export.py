"""Allowlist exports exclude failed traces, scratch, and linked private files."""

import io
import json
from pathlib import Path
import tarfile
import unittest

import test_runner
from common import ReviewError, read_json


class ExportTests(unittest.TestCase):
    setUp = test_runner.RunnerTests.setUp
    scenario = test_runner.RunnerTests.scenario
    run_review = test_runner.RunnerTests.run_review

    def test_archive_contains_only_valid_successes_and_bounded_summaries(self):
        from export_results import export_run
        self.scenario({'codex': {'src/route0.cpp': {'first': 'partial'},
                                 'src/route1.cpp': {'always': 'exit'},
                                 'src/route2.cpp': {'candidates': [test_runner.finding('src/route2.cpp')]}}})
        self.assertEqual(self.run_review(), 1)
        out = self.base / 'exhaustive-run'
        (out / 'work' / 'private.log').write_text('RAW-DIAGNOSTIC-MARKER')
        (out / 'diagnostics').mkdir()
        (out / 'diagnostics/trace.jsonl').write_text('RAW-DIAGNOSTIC-MARKER')
        archive = self.base / 'publish.tar.gz'
        export_run(out, archive)
        with tarfile.open(archive) as tar:
            names = tar.getnames()
            data = b''.join(tar.extractfile(member).read() for member in tar.getmembers())
        self.assertIn('run.json', names)
        self.assertIn('notifications.jsonl', names)
        self.assertTrue(any(name.endswith('/trace.jsonl') for name in names))
        self.assertTrue(any(name.endswith('/report.md') for name in names))
        self.assertTrue(all(not name.startswith(('work/', 'diagnostics/')) for name in names))
        self.assertNotIn(b'INCOMPLETE-MARKER', data)
        self.assertNotIn(b'RAW-DIAGNOSTIC-MARKER', data)
        self.assertFalse(any(name.endswith('failure.json') for name in names))

    def test_export_refuses_changed_success_links_and_raw_failure_summaries(self):
        from export_results import export_run
        from common import json_bytes, sha256
        self.assertEqual(self.run_review(), 0)
        out = self.base / 'exhaustive-run'
        result = next((out / 'results/discovery').glob('*/result.json'))
        original = result.read_bytes()
        result.write_text('changed artifact')
        with self.assertRaises(ReviewError):
            export_run(out, self.base / 'changed.tar.gz')
        result.write_bytes(original)
        result.unlink()
        private = self.base / 'diagnostics/private.json'
        private.write_bytes(original)
        result.symlink_to(private)
        with self.assertRaises(ReviewError):
            export_run(out, self.base / 'linked.tar.gz')
        result.unlink()
        result.write_bytes(original)
        (out / 'notifications.jsonl').write_text(json.dumps({'lane': 'discovery', 'task_id': 'x',
                                                           'attempts': 3, 'error': 'RAW OUTPUT'}) + '\n')
        with self.assertRaises(ReviewError):
            export_run(out, self.base / 'raw.tar.gz')
        self.assertFalse(list(self.base.glob('*.tar.gz')))


if __name__ == '__main__':
    unittest.main()
