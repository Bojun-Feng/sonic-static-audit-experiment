"""Hard-gate validation for discovery findings."""
from pathlib import Path
import functools
from common import json_bytes, keys, read_json, require, source_lines, within

EXPERIMENT = Path(__file__).resolve().parents[1]


def runner_config():
    value = read_json(EXPERIMENT / 'runner-config.json')
    keys(value, ['gates'], 'gate config')
    # Hard-gate thresholds are the repository-specific part of the discovery contract; the
    # defect-kind enum lives in the discovery schema and the gate wording lives in the prompt.
    gates = value["gates"]
    keys(gates, ["max_files_touched", "max_fix_logic_lines", "min_confidence"], "hard gates")
    require(all(type(gates[k]) is int for k in gates) and gates["max_files_touched"] >= 1
            and gates["max_fix_logic_lines"] >= 1 and 1 <= gates["min_confidence"] <= 5,
            "hard gate thresholds must be positive integers with confidence between 1 and 5")
    return value


# Hard gates, in reporting order. Discovery self-assesses them and the runner screens them
# deterministically before admitting a finding.
GATES = ('wg_fit', 'defect_kind', 'intention_clear', 'impact_reachable', 'files_touched',
         'fix_logic_lines', 'fix_unique', 'defect_confidence', 'remedy_confidence')
GATE_FIELDS = ('defect_kind', 'wg_fit', 'intended_behavior', 'intention_clear', 'impact_reachable', 'fix_summary',
               'fix_logic_lines', 'files_touched', 'fix_unique', 'defect_confidence', 'remedy_confidence')
FINDING_FIELDS = ('title', 'path', 'start_line', 'end_line', 'root_cause', 'evidence', 'impact', *GATE_FIELDS)


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


def gate_failures(finding, gates, kinds):
    """Names of the hard gates a discovery finding fails; empty means it passes them all."""
    holds = {'wg_fit': finding['wg_fit'] is True,
             'defect_kind': finding['defect_kind'] in kinds,
             'intention_clear': finding['intention_clear'] is True,
             'impact_reachable': finding['impact_reachable'] is True,
             'files_touched': 1 <= finding['files_touched'] <= gates['max_files_touched'],
             'fix_logic_lines': 1 <= finding['fix_logic_lines'] <= gates['max_fix_logic_lines'],
             'fix_unique': finding['fix_unique'] is True,
             'defect_confidence': finding['defect_confidence'] >= gates['min_confidence'],
             'remedy_confidence': finding['remedy_confidence'] >= gates['min_confidence']}
    return [name for name in GATES if not holds[name]]


def validate_answer(answer, repo):
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
