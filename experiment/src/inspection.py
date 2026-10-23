"""Source-backed native inspection evidence; never execute saved commands.

A retained command may chain simple commands (``A && B``, ``A; B``, one per
line) and pipe a read through range or numbering filters. The expected bytes
of every read are reconstructed from the source and must appear, in order, in
the aggregated output; searches are verified line by line against source
bytes. Commands whose output cannot be reconstructed (reads outside the
source, listings, echoes) are skipped without credit. Nothing is executed.
"""

import os
from pathlib import Path
import re
import shlex

from common import EXTENSIONS, ReviewError, require, within


KEYWORDS = set('auto bool break case char class const continue default delete else false float for '
               'include int namespace nullptr private public return sizeof static std struct switch '
               'template this true unsigned using virtual void while'.split())
SHELLS = ('sh', 'bash', 'zsh')
SEPARATORS = set(';&|\n')
REDIRECTIONS = set('<>&')
RANGE = re.compile(r'([1-9][0-9]*)(?:,([1-9][0-9]*|\$))?p')
COUNT = re.compile(r'\+?[0-9]+')
NUMBERED = '{:>6}\t{}'  # GNU `nl -ba` and `cat -n`
SEARCH_TOOLS = ('rg', 'grep')
SEARCH_VALUE_FLAGS = {'-e', '--regexp', '-g', '--glob', '--iglob', '--include', '--exclude', '--exclude-dir',
                      '-t', '--type', '-A', '-B', '-C', '--context', '--after-context', '--before-context',
                      '--max-count', '-m', '--color'}
SEARCH_PLAIN_FLAGS = {'--line-number', '--fixed-strings', '--with-filename', '--no-heading', '--hidden',
                      '--recursive', '--no-ignore', '--smart-case', '--ignore-case', '--word-regexp'}
SEARCH_SHORT_FLAGS = set('nFrRHEwiSs')
DIAGNOSTIC_PREFIXES = ('rg: ', 'grep: ')


def tokens(command):
    """Shell words of one retained command, unwrapping a single sh/bash/zsh -c/-lc wrapper."""
    if not isinstance(command, str):
        return None
    try:
        argv = shlex.split(command)
        if len(argv) == 3 and Path(argv[0]).name in SHELLS and argv[1] in ('-c', '-lc'):
            command = argv[2]
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|<>\n')
        lexer.whitespace = ' \t\r'
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return None


def simple_commands(command):
    """Pipelines of the command, split at ';', '&&', '||', '&' and newlines.

    Each pipeline is a list of stages (word lists) or None when a redirection
    other than a discarded stderr remains.
    """
    words = tokens(command)
    if words is None:
        return []
    commands, current = [], []
    for word in words + ['\n']:
        if word != '|' and set(word) <= SEPARATORS:
            if current:
                commands.append(pipeline(current))
            current = []
        else:
            current.append(word)
    return commands


def pipeline(words):
    stages, stage = [], []
    for word in words + ['|']:
        if word == '|':
            stages.append(stage)
            stage = []
        else:
            stage.append(word)
    result = []
    for stage in stages:
        kept, index = [], 0
        while index < len(stage):
            if stage[index] == '2' and stage[index + 1:index + 3] == ['>', '/dev/null']:
                index += 3
                continue
            kept.append(stage[index])
            index += 1
        if not kept or any(set(word) <= REDIRECTIONS for word in kept):
            return None
        result.append(kept)
    return result


def resolve(cwd, value):
    """Absolute path of a command argument, or None when the working directory is unknown."""
    if not value or (cwd is None and not value.startswith('/')):
        return None
    return Path(os.path.abspath((cwd or Path('/')) / value))


def source_path(repo, cwd, value):
    candidate = resolve(cwd, value)
    require(candidate is not None and candidate.is_relative_to(repo), 'inspection path is outside source')
    return repo if candidate == repo else within(repo, candidate.relative_to(repo).as_posix())


def source_file(repo, cwd, value, cache):
    """Decoded lines (with endings) of a regular source file, or None."""
    try:
        path = source_path(repo, cwd, value)
    except ReviewError:
        return None
    if path == repo or not path.is_file():
        return None
    return path, lines_of(path, cache)


def lines_of(path, cache):
    """Lines with their `\\n` endings; only newlines delimit lines, as the tools do."""
    if path not in cache:
        parts = path.read_bytes().decode('utf-8').split('\n')
        cache[path] = [part + '\n' for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
    return cache[path]


def sed_expression(args):
    """Return (ranges, remaining args) for `sed -n 'A,Bp[;C,Dp]'`, else (None, None)."""
    args, quiet, expression = list(args), False, None
    while args and args[0].startswith('-'):
        flag = args.pop(0)
        if flag in ('-n', '--quiet', '--silent'):
            quiet = True
        elif flag in ('-e', '-ne', '--expression'):
            quiet |= flag == '-ne'
            if not args or expression is not None:
                return None, None
            expression = args.pop(0)
        elif flag == '--':
            break
        else:
            return None, None
    if not quiet:
        return None, None
    if expression is None:
        if not args:
            return None, None
        expression = args.pop(0)
    ranges = []
    for part in expression.split(';'):
        match = RANGE.fullmatch(part.strip())
        if not match:
            return None, None
        ranges.append((int(match[1]), match[2]))
    return ranges, args


def count_spec(args):
    """Return ((count, from_start), remaining args) for head/tail forms `-n N`, `-nN`, `-N`, `-n +N`."""
    args, spec = list(args), None
    while args and args[0].startswith('-') and args[0] != '-':
        flag = args.pop(0)
        if flag == '-n':
            if not args:
                return None, None
            value = args.pop(0)
        elif flag.startswith('-n'):
            value = flag[2:]
        elif flag == '--':
            break
        else:
            value = flag[1:]
        if spec is not None or not COUNT.fullmatch(value):
            return None, None
        spec = (int(value.lstrip('+')), value.startswith('+'))
    return (spec, args) if spec is not None else (None, None)


def select(stream, ranges):
    """GNU sed `-n` with `A,Bp` commands: each matching command prints the line once."""
    total, result = len(stream), []
    for position, item in enumerate(stream, 1):
        for start, end in ranges:
            stop = total if end == '$' else (int(end) if end else start)
            if position == start or start <= position <= stop:
                result.append(item)
    return result


def cut(stream, tool, spec):
    count, from_start = spec
    if tool == 'head':
        return None if from_start else stream[:count]
    if from_start:
        return stream[max(count, 1) - 1:]
    return stream[len(stream) - count:] if count else []


def number(stream):
    return [(path, line, NUMBERED.format(position, text)) for position, (path, line, text) in enumerate(stream, 1)]


def filter_stage(stage, stream):
    tool, args = Path(stage[0]).name, [a for a in stage[1:] if a != '--']
    if tool == 'sed':
        ranges, rest = sed_expression(args)
        return select(stream, ranges) if ranges is not None and not rest else None
    if tool in ('head', 'tail'):
        spec, rest = count_spec(args)
        return cut(stream, tool, spec) if spec is not None and not rest else None
    if tool == 'nl':
        return number(stream) if args in (['-ba'], ['-b', 'a']) else None
    if tool == 'cat':
        if not args:
            return stream
        return number(stream) if args in (['-n'], ['--number']) else None
    return None


def read_plan(stages, repo, cwd, cache):
    """Expected output of a read pipeline as (parts, text); parts map paths to line sets."""
    producer, filters = stages[0], stages[1:]
    tool, args = Path(producer[0]).name, [a for a in producer[1:] if a != '--']
    numbered, ranges, spec = False, None, None
    if tool == 'cat':
        flags = [a for a in args if a.startswith('-')]
        if any(flag not in ('-n', '--number') for flag in flags):
            return None
        numbered, files = bool(flags), [a for a in args if not a.startswith('-')]
    elif tool == 'nl':
        if args[:1] == ['-ba']:
            files = args[1:]
        elif args[:2] == ['-b', 'a']:
            files = args[2:]
        else:
            return None
        numbered = True
    elif tool == 'sed':
        ranges, files = sed_expression(args)
        if ranges is None:
            return None
    elif tool in ('head', 'tail'):
        spec, files = count_spec(args)
        if spec is None:
            return None
    else:
        return None
    if not files or any(f.startswith('-') for f in files) or (tool != 'cat' and len(files) != 1):
        return None
    stream = []
    for index, name in enumerate(files):
        found = source_file(repo, cwd, name, cache)
        if found is None:
            return None
        path, lines = found
        # `cat` merges a final unterminated line with the next file's first line.
        if index < len(files) - 1 and lines and not lines[-1].endswith('\n'):
            return None
        stream.extend((path, line, text) for line, text in enumerate(lines, 1))
    if ranges is not None:
        stream = select(stream, ranges)
    if spec is not None:
        stream = cut(stream, tool, spec)
        if stream is None:
            return None
    if numbered:
        stream = number(stream)
    for stage in filters:
        stream = filter_stage(stage, stream)
        if stream is None:
            return None
    parts = {}
    for path, line, _ in stream:
        parts.setdefault(path, set()).add(line)
    return parts, ''.join(text for _, _, text in stream)


def count_plan(stage, repo, cwd, cache):
    """Expected output of `wc -l FILE`, which anchors a chain but earns no credit."""
    if len(stage) != 3 or Path(stage[0]).name != 'wc' or stage[1] != '-l':
        return None
    found = source_file(repo, cwd, stage[2], cache)
    if found is None:
        return None
    return f'{sum(text.endswith(chr(10)) for text in found[1])} {stage[2]}\n'


def search_plan(stages, repo, cwd):
    """Parsed `rg`/`grep -n` search, optionally bounded by `head`, or None."""
    for stage in stages[1:]:
        if Path(stage[0]).name != 'head':
            return None
        spec, rest = count_spec([a for a in stage[1:] if a != '--'])
        if spec is None or spec[1] or rest:
            return None
    return parse_search(stages[0], repo, cwd)


def parse_search(argv, repo, cwd):
    if not argv or Path(argv[0]).name not in SEARCH_TOOLS:
        return None
    args, positional, pattern = argv[1:], [], None
    numbered = literal = False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == '--':
            positional.extend(args[index + 1:])
            break
        if arg in ('-e', '--regexp'):
            index += 1
            if index == len(args):
                return None
            pattern = args[index]
        elif arg in SEARCH_VALUE_FLAGS:
            index += 1
            if index == len(args):
                return None
        elif arg.startswith('--'):
            if '=' in arg and arg.split('=', 1)[0] in SEARCH_VALUE_FLAGS:
                pass
            elif arg not in SEARCH_PLAIN_FLAGS:
                return None
            numbered |= arg == '--line-number'
            literal |= arg == '--fixed-strings'
        elif arg.startswith('-') and len(arg) > 1:
            if not set(arg[1:]) <= SEARCH_SHORT_FLAGS:
                return None
            numbered |= 'n' in arg
            literal |= 'F' in arg
        else:
            positional.append(arg)
        index += 1
    if pattern is None:
        if not positional:
            return None
        pattern = positional.pop(0)
    if not numbered or not positional:
        return None
    roots, foreign = [], []
    for value in positional:
        target = resolve(cwd, value)
        if target is None:
            return None
        if target.is_relative_to(repo):
            try:
                root = source_path(repo, cwd, value)
            except ReviewError:
                return None
            if not root.exists():
                return None
            roots.append(root)
        else:
            foreign.append(target)
    if not roots:
        return None
    return {'roots': roots, 'foreign': foreign, 'literal': literal, 'pattern': pattern, 'cwd': cwd}


def explain(line, searches, repo, cache):
    """Classify one search output line: ('hit'|'context', path, content), 'foreign', or None."""
    if line == '--' or line.startswith(DIAGNOSTIC_PREFIXES):
        return 'foreign'
    candidates = [(line[:match.start()], int(match[2]), line[match.end():], match[1])
                  for match in re.finditer(r'([:-])([1-9][0-9]*)\1', line)]
    bare = re.fullmatch(r'([1-9][0-9]*)([:-])(.*)', line, re.DOTALL)
    for search in searches:
        for value, number_, content, separator in candidates:
            target = resolve(search['cwd'], value)
            if target is None:
                continue
            if not target.is_relative_to(repo):
                if any(target.is_relative_to(root) for root in search['foreign']):
                    return 'foreign'
                continue
            if not any(target == root or target.is_relative_to(root) for root in search['roots']):
                continue
            found = source_file(repo, search['cwd'], value, cache)
            if found and verify(found, number_, content):
                return ('hit' if separator == ':' else 'context', found[0], content)
        if bare:
            for root in search['roots']:
                if root.is_file() and verify((root, lines_of(root, cache)), int(bare[1]), bare[3]):
                    return ('hit' if bare[2] == ':' else 'context', root, bare[3])
    return None


def verify(found, number_, content):
    lines = found[1]
    return number_ <= len(lines) and lines[number_ - 1].rstrip('\r\n') == content


def search_credit(searches, output, repo, identifiers, cache):
    """True when every output line is an explained search line and a verified
    C/C++ line mentions an identifier of the assigned file."""
    symbols = set()
    for search in searches:
        pattern = search['pattern']
        if not search['literal']:
            pattern = pattern.replace(r'\b', '').replace(r'\<', '').replace(r'\>', '')
        symbols |= identifiers & set(re.findall(r'[A-Za-z_][A-Za-z_0-9]{2,}', pattern))
    if not symbols:
        return False
    literals = [s['pattern'] for s in searches if s['literal']]
    matched = False
    for line in output.split('\n'):
        if not line:
            continue
        explained = explain(line, searches, repo, cache)
        if explained is None:
            return False
        if explained == 'foreign':
            continue
        kind, path, content = explained
        if kind == 'hit' and literals and not any(literal in content for literal in literals):
            return False
        matched |= path.suffix in EXTENSIONS and any(symbol in content for symbol in symbols)
    return matched


def anchor(rest, text, skipping, padded=False):
    """Span of the expected text: at the start, or after skipped output on a line boundary.

    `padded` allows leading blanks, as BSD `wc` prints before its count.
    """
    pattern = re.compile((r'[ \t]*' if padded else '') + re.escape(text))
    match = pattern.match(rest)
    if match:
        return match.span()
    for match in pattern.finditer(rest) if skipping else ():
        if match.start() and rest[match.start() - 1] == '\n':
            return match.span()
    return None


def attribute(commands, output, repo, identifiers, cache):
    """Credited evidence of parsed simple commands: ('read', path, lines) and ('search',) items."""
    credits, rest, cwd, pending = [], output, repo, []

    def settle(chunk):
        if pending and all(pending) and search_credit(pending, chunk, repo, identifiers, cache):
            credits.append(('search',))

    for stages in commands:
        if stages is None:
            pending.append(None)
            continue
        if len(stages) == 1 and len(stages[0]) == 2 and stages[0][0] == 'cd':
            try:
                target = source_path(repo, cwd, stages[0][1])
                cwd = target if target.is_dir() else None
            except ReviewError:
                cwd = None
            continue
        plan = read_plan(stages, repo, cwd, cache)
        count = count_plan(stages[0], repo, cwd, cache) if plan is None and len(stages) == 1 else None
        if plan is not None:
            parts, text = plan
        elif count is not None:
            parts, text = {}, count
        else:
            pending.append(search_plan(stages, repo, cwd))
            continue
        if not text:
            continue
        span = anchor(rest, text, bool(pending), padded=plan is None)
        if span is None:
            break
        settle(rest[:span[0]])
        pending = []
        rest = rest[span[1]:]
        credits.extend(('read', path, lines) for path, lines in parts.items())
    settle(rest)
    return credits


def validate_inspection(events, task, repo):
    repo = Path(os.path.abspath(repo))
    source = within(repo, task['path'])
    cache = {}
    text = ''.join(lines_of(source, cache))
    identifiers = set(re.findall(r'\b[A-Za-z_][A-Za-z_0-9]{2,}\b', text)) - KEYWORDS
    covered, read_ids, context_ids, ids = set(), set(), set(), set()
    final_index = max(i for i, event in enumerate(events) if event.get('type') == 'item.completed'
                      and isinstance(event.get('item'), dict) and event['item'].get('type') == 'agent_message')
    for event in events[:final_index]:
        item = event.get('item')
        if event.get('type') != 'item.completed' or not isinstance(item, dict) or item.get('type') != 'command_execution':
            continue
        identity = item.get('id')
        require(isinstance(identity, str) and identity and identity not in ids,
                'missing or duplicate inspection command ID')
        ids.add(identity)
        if item.get('status') not in ('completed', 'failed') or type(item.get('exit_code')) is not int:
            continue
        output = item.get('aggregated_output')
        if not isinstance(output, str) or not output.strip():
            continue
        # A single command must succeed; a chain's status reflects only its last
        # command, so its byte-exact reads and searches keep their credit.
        commands = simple_commands(item.get('command'))
        if len(commands) < 2 and (item['status'] != 'completed' or item['exit_code'] != 0):
            continue
        try:
            credits = attribute(commands, output, repo, identifiers, cache)
        except (ReviewError, OSError, UnicodeError):
            continue
        for index, credit in enumerate(credits):
            key = f'{identity}#{index}'
            if credit[0] == 'search':
                context_ids.add(key)
                continue
            _, path, lines = credit
            if path == source:
                covered.update(lines)
                read_ids.add(key)
            elif path.suffix in EXTENSIONS:
                content = ''.join(lines_of(path, cache)[line - 1] for line in sorted(lines))
                if identifiers & set(re.findall(r'\b\w+\b', content)):
                    context_ids.add(key)
    require(covered == set(range(1, len(lines_of(source, cache)) + 1)) and read_ids and context_ids
            and len(read_ids | context_ids) >= 2,
            'incomplete source inspection evidence: require assigned-file coverage and related source context')
