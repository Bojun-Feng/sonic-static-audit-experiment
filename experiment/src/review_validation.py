"""Hard-gate validation for discovery and review results."""
from pathlib import Path
import functools
from common import json_bytes, keys, read_json, require, source_lines, within

EXPERIMENT = Path(__file__).resolve().parents[1]


def runner_config():
    value = read_json(EXPERIMENT / 'runner-config.json')
    keys(value, ['gates'], 'gate config')
    # Hard-gate thresholds are the repository-specific part of the review family;
    # the defect-kind enum lives in the discovery schema, the wording in the prompts.
    gates = value["gates"]
    keys(gates, ["max_files_touched", "max_fix_logic_lines", "min_confidence"], "hard gates")
    require(all(type(gates[k]) is int for k in gates) and gates["max_files_touched"] >= 1
            and gates["max_fix_logic_lines"] >= 1 and 1 <= gates["min_confidence"] <= 5,
            "hard gate thresholds must be positive integers with confidence between 1 and 5")
    return value


# Hard gates, in reporting order. Discovery self-assesses them, the runner screens
# deterministically, and the blinded verifier re-scores them before `confirmed`.
GATES = ('wg_fit', 'defect_kind', 'intention_clear', 'impact_reachable', 'files_touched',
         'fix_logic_lines', 'fix_unique', 'defect_confidence', 'remedy_confidence')
GATE_FIELDS = ('defect_kind', 'wg_fit', 'intended_behavior', 'intention_clear', 'impact_reachable', 'fix_summary',
               'fix_logic_lines', 'files_touched', 'fix_unique', 'defect_confidence', 'remedy_confidence')
FINDING_FIELDS = ('title', 'path', 'start_line', 'end_line', 'root_cause', 'evidence', 'impact', *GATE_FIELDS)
VERDICT_FIELDS = ('status', 'rationale', 'failed_gates', 'wg_fit', 'kind_fit', 'intention_clear', 'impact_reachable',
                  'fix_patch', 'fix_logic_lines', 'files_touched', 'fix_unique', 'defect_confidence', 'remedy_confidence')
# The only candidate material a verifier session may read.
VALIDATION_FIELDS = ('id', 'path', 'start_line', 'end_line', 'defect_kind', 'claims')


def atomic_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    require(not path.is_symlink() and not temporary.is_symlink(), 'output file must not be a symlink')
    temporary.write_bytes(json_bytes(value))
    temporary.replace(path)


@functools.lru_cache(maxsize=1)
def defect_kinds():
    """The closed defect family, read from the discovery schema so there is one source of truth."""
    schema = read_json(EXPERIMENT / 'schemas/discovery.json')
    kinds = schema['properties']['candidates']['items']['properties']['defect_kind']['enum']
    require(isinstance(kinds, list) and kinds and all(isinstance(k, str) for k in kinds), 'invalid defect kinds')
    return kinds


def gate_failures(assessment, gates, kinds=None):
    """Names of the hard gates an assessment fails; empty means it passes them all.

    A discovery finding carries `defect_kind` (checked against `kinds`); a verifier
    verdict carries `kind_fit` instead.
    """
    kind = assessment['defect_kind'] in kinds if kinds is not None else assessment['kind_fit']
    # A verifier verdict that offers no fix (blank fix_patch) is not scored on fix size: a non-defect rejection
    # need not invent a patch. A confirmed verdict still has to supply the patch (checked in validate_answer).
    no_fix = kinds is None and not str(assessment.get('fix_patch', '')).strip()
    holds = {'wg_fit': assessment['wg_fit'] is True,
             'defect_kind': kind is True,
             'intention_clear': assessment['intention_clear'] is True,
             'impact_reachable': assessment['impact_reachable'] is True,
             'files_touched': no_fix or 1 <= assessment['files_touched'] <= gates['max_files_touched'],
             'fix_logic_lines': no_fix or 1 <= assessment['fix_logic_lines'] <= gates['max_fix_logic_lines'],
             'fix_unique': assessment['fix_unique'] is True,
             'defect_confidence': assessment['defect_confidence'] >= gates['min_confidence'],
             'remedy_confidence': assessment['remedy_confidence'] >= gates['min_confidence']}
    return [name for name in GATES if not holds[name]]


def patch_files(patch):
    """Files named by a unified diff's `+++` headers, without `a/`/`b/` prefixes."""
    files = []
    for line in patch.splitlines():
        if line.startswith('+++ '):
            name = line[4:].split('\t')[0].strip()
            files.append(name[2:] if name.startswith(('a/', 'b/')) else name)
    return files


def patch_logic_lines(patch):
    """Added or removed non-blank, non-comment lines of a unified diff."""
    count = 0
    for line in patch.splitlines():
        if line.startswith(('+++', '---')) or len(line) < 2 or line[0] not in '+-':
            continue
        body = line[1:].strip()
        if body and not body.startswith(('//', '/*', '*')):
            count += 1
    return count


def validate_answer(answer, repo, lane, task, config):
    gates = config['gates']
    if lane == 'discovery':
        keys(answer, ['candidates'], 'discovery result')
        require(isinstance(answer['candidates'], list), 'candidates must be an array')
        for finding in answer['candidates']:
            keys(finding, FINDING_FIELDS, 'finding')
            require(all(isinstance(finding[k], str) and finding[k].strip()
                        for k in ('title', 'path', 'root_cause', 'evidence', 'impact', 'intended_behavior', 'fix_summary')),
                    'empty finding field')
            require(finding['defect_kind'] in defect_kinds(), 'unknown defect kind')
            require(all(type(finding[k]) is bool for k in ('wg_fit', 'intention_clear', 'impact_reachable', 'fix_unique')),
                    'gate flags must be booleans')
            require(all(type(finding[k]) is int and finding[k] >= 1 for k in ('fix_logic_lines', 'files_touched')),
                    'invalid fix size')
            require(all(type(finding[k]) is int and 1 <= finding[k] <= 5
                        for k in ('defect_confidence', 'remedy_confidence')), 'invalid confidence')
            source = within(repo, finding['path'])
            require(source.is_file(), 'finding source is missing')
            require(type(finding['start_line']) is int and type(finding['end_line']) is int
                    and 1 <= finding['start_line'] <= finding['end_line'] <= len(source_lines(source.read_bytes())),
                    'invalid finding line range')
    else:
        keys(answer, VERDICT_FIELDS, 'verifier result')
        require(answer['status'] in ('confirmed', 'rejected', 'needs-validation')
                and isinstance(answer['rationale'], str) and answer['rationale'].strip(), 'invalid verdict')
        require(all(type(answer[k]) is bool for k in ('wg_fit', 'kind_fit', 'intention_clear', 'impact_reachable', 'fix_unique')),
                'gate flags must be booleans')
        require(all(type(answer[k]) is int and answer[k] >= 0 for k in ('fix_logic_lines', 'files_touched')),
                'invalid fix size')
        require(all(type(answer[k]) is int and 1 <= answer[k] <= 5 for k in ('defect_confidence', 'remedy_confidence')),
                'invalid confidence')
        require(isinstance(answer['fix_patch'], str), 'fix_patch must be text')
        failed = answer['failed_gates']
        require(isinstance(failed, list) and all(isinstance(g, str) and g in GATES for g in failed)
                and len(set(failed)) == len(failed), 'invalid failed_gates')
        # The verdict must agree with its own gate fields: confirmed iff every gate holds.
        derived = gate_failures(answer, gates)
        require(sorted(failed) == sorted(derived), 'failed_gates disagree with the gate fields')
        require((answer['status'] == 'confirmed') == (not derived), 'status disagrees with the hard gates')
        if answer['status'] == 'confirmed':
            files = patch_files(answer['fix_patch'])
            require(len(files) == 1 and (files[0] == task['path'] or files[0].endswith('/' + task['path'])),
                    'fix_patch must change exactly the candidate file')
            require(1 <= patch_logic_lines(answer['fix_patch']) <= gates['max_fix_logic_lines'],
                    'fix_patch logic lines are out of bounds')
