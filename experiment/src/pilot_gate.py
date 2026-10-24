#!/usr/bin/env python3
"""Cheap pilot acceptance gate: candidate counts, evidence quality, and verifier independence.

Evaluate finished pilot arms before authorizing a full production run. Every run
is re-validated through the export inventory (hashes, exact children) first, so
the numbers below describe only validated artifacts.
"""

import argparse
import fcntl
import json
import os
from pathlib import Path
import sys

from common import ReviewError, json_bytes, within
from export_results import inventory
from review_queue import runner_config
from runner import GATE_FIELDS, VALIDATION_FIELDS, patch_files, patch_logic_lines

CHECKS = ('integrity', 'gate_fields', 'candidate_count', 'fix_shape', 'independence')
# Material a blinded verifier prompt or tool call must never mention.
LEAKS = ('prior_candidates', 'candidate.json', 'report.md', 'trace.jsonl', 'results/discovery',
         'results/verifier', 'results/candidates')
PROMPT_SPREAD_BYTES = 2048


def strings(value):
    """All string leaves of a JSON value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def sibling_reads(trace, own):
    """Verifier tool inputs that reach into results other than the candidate's own validation input."""
    found = []
    for line in trace.splitlines():
        event = json.loads(line)
        if event.get('type') != 'assistant':
            continue
        for block in event.get('message', {}).get('content', []):
            if isinstance(block, dict) and block.get('type') == 'tool_use':
                found.extend(text for text in strings(block.get('input'))
                             if ('results/' in text and own not in text) or 'trace.jsonl' in text)
    return found


def evaluate_run(root, max_candidates, max_admitted_ratio, gates):
    root = Path(root).resolve()
    fd = os.open(within(root, 'runner.lock'), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReviewError('cannot evaluate an active run') from exc
        files = inventory(root)
    finally:
        os.close(fd)
    status = json.loads(files['status.json'])
    checks = {}

    def check(name, passed, detail):
        checks[name] = {'pass': bool(passed), 'detail': detail}

    residue = [p.name for p in within(root, 'work').iterdir()]
    notices = len(files['notifications.jsonl'].decode().splitlines()) if 'notifications.jsonl' in files else 0
    check('integrity', status['state'] == 'complete' and not residue,
          f"state={status['state']} notices={notices} work_residue={len(residue)}")
    findings = [finding for name, data in files.items()
                if name.startswith('results/discovery/') and name.endswith('/result.json')
                for finding in json.loads(data)['candidates']]
    missing = [f for f in findings if not set(GATE_FIELDS) <= set(f)]
    screened = json.loads(files['results/screened.json']) if 'results/screened.json' in files else None
    check('gate_fields', not missing and screened is not None and all(s['failed_gates'] for s in screened),
          f'findings={len(findings)} missing_gate_fields={len(missing)} screened_file={screened is not None}')
    candidates = {name.split('/')[2]: json.loads(data) for name, data in files.items()
                  if name.startswith('results/candidates/') and name.endswith('/candidate.json')}
    validations = {name.split('/')[2]: json.loads(data) for name, data in files.items()
                   if name.startswith('results/validation/') and name.endswith('/validation-input.json')}
    verdicts = {name.split('/')[2]: json.loads(data) for name, data in files.items()
                if name.startswith('results/verifier/') and name.endswith('/result.json')}
    counts = {'discovery_succeeded': status['discovery']['succeeded'], 'discovery_failed': status['discovery']['failed'],
              'verifier_succeeded': status['verifier']['succeeded'], 'verifier_failed': status['verifier']['failed'],
              'raw_findings': len(findings), 'screened': len(screened or []), 'candidate_count': len(candidates),
              'confirmed': sum(v['status'] == 'confirmed' for v in verdicts.values()),
              'rejected': sum(v['status'] == 'rejected' for v in verdicts.values()),
              'needs_validation': sum(v['status'] == 'needs-validation' for v in verdicts.values())}
    ratio = len(candidates) / len(findings) if findings else 0.0
    check('candidate_count', len(candidates) <= max_candidates and ratio <= max_admitted_ratio,
          f'admitted={len(candidates)} raw={len(findings)} ratio={ratio:.2f} '
          f'max_candidates={max_candidates} max_ratio={max_admitted_ratio}')
    malformed = []
    for candidate_id, verdict in verdicts.items():
        if verdict['status'] != 'confirmed':
            continue
        patch = verdict['fix_patch']
        shape = (verdict['files_touched'] == 1 and 1 <= verdict['fix_logic_lines'] <= gates['max_fix_logic_lines']
                 and min(verdict['defect_confidence'], verdict['remedy_confidence']) >= gates['min_confidence']
                 and not verdict['failed_gates'] and len(patch_files(patch)) == 1
                 and 1 <= patch_logic_lines(patch) <= gates['max_fix_logic_lines'])
        if not shape:
            malformed.append(candidate_id)
    check('fix_shape', not malformed, f"confirmed={counts['confirmed']} malformed={len(malformed)}")
    leaks, reads, sizes = [], [], []
    for candidate_id in verdicts:
        prompt = files[f'results/verifier/{candidate_id}/prompt.txt'].decode()
        own = f'results/validation/{candidate_id}/validation-input.json'
        sizes.append(len(prompt.encode()))
        if any(leak in prompt for leak in LEAKS) or prompt.count('validation-input.json') != 1 or own not in prompt:
            leaks.append(candidate_id)
        if sibling_reads(files[f'results/verifier/{candidate_id}/trace.jsonl'].decode(), f'results/validation/{candidate_id}'):
            reads.append(candidate_id)
    bad_inputs = [cid for cid, value in validations.items()
                  if set(value) != set(VALIDATION_FIELDS)
                  or any(set(claim) != {'title', 'root_cause'} for claim in value['claims'])]
    spread = max(sizes) - min(sizes) if sizes else 0
    check('independence', not leaks and not reads and not bad_inputs and spread < PROMPT_SPREAD_BYTES
          and set(validations) == set(candidates),
          f'verdicts={len(verdicts)} prompt_leaks={len(leaks)} sibling_reads={len(reads)} '
          f'bad_inputs={len(bad_inputs)} prompt_spread_bytes={spread}')
    cost, durations = 0.0, []
    for candidate_id in verdicts:
        metadata = json.loads(files[f'results/verifier/{candidate_id}/metadata.json'])
        cost += float(metadata.get('total_cost_usd') or 0.0)
        if metadata.get('duration_ms') is not None:
            durations.append(metadata['duration_ms'])
    counts['cost_usd'] = round(cost, 4)
    counts['mean_duration_ms'] = round(sum(durations) / len(durations)) if durations else None
    confirmed_paths = sorted(candidates[cid]['path'] for cid, v in verdicts.items() if v['status'] == 'confirmed')
    return {'state': status['state'], 'counts': counts, 'checks': checks, 'confirmed_paths': confirmed_paths}


def evaluate(runs, positives, negatives, max_candidates, max_admitted_ratio, max_cost_usd=None):
    gates = runner_config()['gates']
    report = {'runs': {}, 'totals': {}, 'checks': {}, 'controls': {}}
    for run in runs:
        report['runs'][str(Path(run).resolve())] = evaluate_run(run, max_candidates, max_admitted_ratio, gates)
    results = list(report['runs'].values())
    for key in ('discovery_succeeded', 'discovery_failed', 'raw_findings', 'screened', 'candidate_count',
                'verifier_succeeded', 'verifier_failed', 'confirmed', 'rejected', 'needs_validation'):
        report['totals'][key] = sum(r['counts'][key] for r in results)
    report['totals']['cost_usd'] = round(sum(r['counts']['cost_usd'] for r in results), 4)
    for name in CHECKS:
        report['checks'][name] = {'pass': all(r['checks'][name]['pass'] for r in results),
                                  'detail': '; '.join(r['checks'][name]['detail'] for r in results)}
    confirmed = [path for r in results for path in r['confirmed_paths']]
    for path in positives:
        report['controls'][path] = {'kind': 'positive', 'confirmed': confirmed.count(path), 'pass': path in confirmed}
    for path in negatives:
        report['controls'][path] = {'kind': 'negative', 'confirmed': confirmed.count(path), 'pass': path not in confirmed}
    report['checks']['controls'] = {'pass': all(c['pass'] for c in report['controls'].values()),
                                    'detail': ' '.join(f"{c['kind']}:{p}={c['confirmed']}" for p, c in report['controls'].items())
                                    or 'no control paths'}
    report['checks']['cost'] = {'pass': max_cost_usd is None or report['totals']['cost_usd'] <= max_cost_usd,
                                'detail': f"cost_usd={report['totals']['cost_usd']} max={max_cost_usd}"}
    report['passed'] = all(check['pass'] for check in report['checks'].values())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='append', dest='runs', type=Path, required=True)
    parser.add_argument('--positive', action='append', dest='positives', default=[],
                        help='path expected to yield at least one confirmed verdict')
    parser.add_argument('--negative', action='append', dest='negatives', default=[],
                        help='path expected to yield no confirmed verdict')
    parser.add_argument('--max-candidates', type=int, default=12, help='per run')
    parser.add_argument('--max-admitted-ratio', type=float, default=0.5, help='admitted candidates over raw findings, per run')
    parser.add_argument('--max-cost-usd', type=float)
    parser.add_argument('--out', type=Path, help='JSON report path')
    args = parser.parse_args()
    try:
        report = evaluate(args.runs, args.positives, args.negatives, args.max_candidates, args.max_admitted_ratio,
                          args.max_cost_usd)
        if args.out:
            args.out.write_bytes(json_bytes(report))
    except (ReviewError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f'FAIL pilot gate: {exc}', file=sys.stderr)
        return 1
    for name, check in report['checks'].items():
        print(f"{'PASS' if check['pass'] else 'FAIL'} {name}: {check['detail']}")
    print(('PASS' if report['passed'] else 'FAIL') + ' pilot gate: ' + json.dumps(report['totals'], sort_keys=True))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
